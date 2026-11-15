"""Collects market data and turns it into the fact sheet the AI analyses.

All numbers come from OKX market data and from indicators.py. The AI is told
to use only these numbers.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .indicators import atr, ema, recent_range, rsi
from .models import Candle, OpenInterest, Ticker
from .okx_client import OkxClient, OkxError, OkxNetworkError

log = logging.getLogger(__name__)

# (bar, candles to fetch, label, lookback for the recent high/low)
TIMEFRAMES = (
    ("15m", 200, "15 分钟", 48),
    ("1H", 250, "1 小时", 48),
    ("4H", 250, "4 小时", 42),
    ("1D", 250, "日线", 30),
)


@dataclass
class TimeframeView:
    label: str
    close: float
    ema20: float | None
    ema50: float | None
    ema200: float | None
    rsi14: float | None
    atr14: float | None
    high: float | None
    low: float | None
    lookback: int

    @property
    def trend(self) -> str:
        if self.ema20 is None or self.ema50 is None:
            return "数据不足"
        if self.close > self.ema20 > self.ema50:
            return "偏多（价格在 EMA20 和 EMA50 上方，EMA20 在 EMA50 上方）"
        if self.close < self.ema20 < self.ema50:
            return "偏空（价格在 EMA20 和 EMA50 下方，EMA20 在 EMA50 下方）"
        return "震荡（价格和均线互相缠绕）"


@dataclass
class MarketContext:
    inst_id: str
    ticker: Ticker | None = None
    funding_rate: float | None = None
    open_interest: OpenInterest | None = None
    views: list[TimeframeView] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.ticker is not None and bool(self.views)

    def to_text(self) -> str:
        lines = [f"品种：{self.inst_id}"]
        if self.ticker:
            ticker = self.ticker
            lines.append(f"最新价：{_px(ticker.last)}")
            if ticker.high_24h and ticker.low_24h:
                lines.append(f"24 小时区间：{_px(ticker.low_24h)} 到 {_px(ticker.high_24h)}")
            if ticker.change_24h_pct is not None:
                lines.append(f"24 小时涨跌：{ticker.change_24h_pct:+.2f}%")
        if self.funding_rate is not None:
            lines.append(f"当前资金费率 (funding rate)：{self.funding_rate * 100:+.4f}%")
        if self.open_interest is not None:
            interest = _open_interest_text(self.open_interest)
            if interest:
                lines.append(f"持仓量 (open interest)：{interest}")
        for view in self.views:
            parts = [f"收盘 {_px(view.close)}", f"趋势 {view.trend}"]
            if view.ema20 is not None:
                parts.append(f"EMA20 {_px(view.ema20)}")
            if view.ema50 is not None:
                parts.append(f"EMA50 {_px(view.ema50)}")
            if view.ema200 is not None:
                parts.append(f"EMA200 {_px(view.ema200)}")
            if view.rsi14 is not None:
                parts.append(f"RSI14 {view.rsi14:.1f}")
            if view.atr14 is not None:
                parts.append(f"ATR14 {_px(view.atr14)}")
            if view.high is not None and view.low is not None:
                parts.append(f"近 {view.lookback} 根 K 线高点 {_px(view.high)}、低点 {_px(view.low)}")
            lines.append(f"[{view.label}] " + "，".join(parts))
        for error in self.errors:
            lines.append(f"（缺失数据：{error}）")
        return "\n".join(lines)


def fmt_price(value: float) -> str:
    """Two decimals for gold or BTC, more for coins that cost cents or less."""
    size = abs(value)
    if size >= 100 or size == 0:
        return f"{value:,.2f}"
    decimals = min(12, 3 - math.floor(math.log10(size)))
    return f"{value:,.{decimals}f}"


_px = fmt_price


def _open_interest_text(interest: OpenInterest) -> str:
    parts = []
    if interest.coins:
        parts.append(f"{interest.coins:,.0f} 单位标的")
    if interest.usd:
        parts.append(f"约 {interest.usd / 1e8:,.2f} 亿美元")
    return "，".join(parts)


def quote_line(okx: OkxClient, inst_id: str) -> str:
    """One line with the latest price, straight from OKX, no AI involved."""
    ticker = okx.ticker(inst_id)
    parts = []
    if ticker.change_24h_pct is not None:
        parts.append(f"24 小时 {ticker.change_24h_pct:+.2f}%")
    if ticker.high_24h and ticker.low_24h:
        parts.append(f"区间 {fmt_price(ticker.low_24h)} 到 {fmt_price(ticker.high_24h)}")
    try:
        funding = okx.funding_rate(inst_id)
    except (OkxError, OkxNetworkError):
        funding = None
    if funding is not None:
        parts.append(f"资金费率 {funding * 100:+.4f}%")
    detail = f"（{'，'.join(parts)}）" if parts else ""
    return f"**{inst_id}** {fmt_price(ticker.last)}{detail}"


def candles_text(inst_id: str, bar: str, candles: list[Candle], tz: ZoneInfo) -> str:
    """Raw candles as text, oldest first, for the AI to read exact highs and lows."""
    if not candles:
        return f"{inst_id} 没有 {bar} K 线数据。"
    stamp = "%Y-%m-%d" if bar.endswith(("D", "W", "M")) else "%m-%d %H:%M"
    lines = [f"{inst_id} 的 {bar} K 线，共 {len(candles)} 根，从旧到新，时间为 {tz.key}。标 * 的一根还没收盘。"]
    for candle in candles:
        moment = datetime.fromtimestamp(candle.ts / 1000, tz=tz).strftime(stamp)
        mark = "" if candle.confirmed else " *"
        lines.append(
            f"{moment} 开 {fmt_price(candle.open)} 高 {fmt_price(candle.high)} "
            f"低 {fmt_price(candle.low)} 收 {fmt_price(candle.close)}{mark}"
        )
    return "\n".join(lines)


def build_market_context(okx: OkxClient, inst_id: str) -> MarketContext:
    context = MarketContext(inst_id=inst_id)
    try:
        context.ticker = okx.ticker(inst_id)
    except (OkxError, OkxNetworkError) as exc:
        context.errors.append(f"最新价（{exc}）")
    try:
        context.funding_rate = okx.funding_rate(inst_id)
    except (OkxError, OkxNetworkError) as exc:
        log.info("no funding rate for %s: %s", inst_id, exc)
    try:
        context.open_interest = okx.open_interest(inst_id)
    except (OkxError, OkxNetworkError) as exc:
        log.info("no open interest for %s: %s", inst_id, exc)
    for bar, limit, label, lookback in TIMEFRAMES:
        try:
            candles = okx.candles(inst_id, bar, limit)
        except (OkxError, OkxNetworkError) as exc:
            context.errors.append(f"{label} K 线（{exc}）")
            continue
        # Indicators use closed candles only, so they do not jump around mid-bar.
        closed = [candle for candle in candles if candle.confirmed] or candles
        if len(closed) < 2:
            context.errors.append(f"{label} K 线太少")
            continue
        closes = [candle.close for candle in closed]
        high_low = recent_range(closed, lookback)
        context.views.append(
            TimeframeView(
                label=label,
                close=closes[-1],
                ema20=ema(closes, 20),
                ema50=ema(closes, 50),
                ema200=ema(closes, 200),
                rsi14=rsi(closes, 14),
                atr14=atr(closed, 14),
                high=high_low[0] if high_low else None,
                low=high_low[1] if high_low else None,
                lookback=min(lookback, len(closed)),
            )
        )
    return context
