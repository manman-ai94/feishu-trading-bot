"""How heavy the positions are, measured against how much the market moves. Rules only, no AI.

What is measured, all from what OKX reports:

  * Effective leverage: what the positions are worth, divided by the account's
    equity. The leverage set in OKX only decides how much margin a position
    ties up. This figure says how hard a move hits the account: at 20 times,
    a 1% move against the position is 20% of the equity.
  * The distance to the liquidation price, counted in days of normal range
    (the daily ATR). "4% away" says little until one knows that the market
    moves 2.4% on a normal day.
  * What the stop-loss would cost if it triggered now, as a share of the
    equity. Without a stop: what is gone by the time the liquidation price
    is reached.
  * What holding the position costs in funding at the current rate.

The thresholds (ALERT_LIQ_ATR, ALERT_LEVERAGE, ALERT_STOP_RISK_PCT) are the
owner's to set. Their defaults come from the project's research on stops and
position size; they are starting points and were never tested on this
contract. The gauge measures and reports. What to do about a reading is the
owner's decision, and no message here names a price or a size to trade.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .indicators import atr
from .models import AccountState, Candle, Position
from .okx_client import OkxError, OkxNetworkError

log = logging.getLogger(__name__)

DAY_BAR = "1Dutc"  # days that begin at 00:00 UTC, so that Saturday and Sunday are whole candles
DAY_CANDLES = 60
ATR_PERIOD = 14
RANGE_TTL_SECONDS = 3600.0
FUNDING_TTL_SECONDS = 600.0
LEVEL_WORDS = {1: "提醒", 2: "警告", 3: "紧急"}
# A reading reported at one of the owner's lines is only told again after it has been this much clear
# of that line (20% further from liquidation, or 20% less leverage), so a price wobbling on a line does
# not send the same card every half hour.
RELEASE_FACTOR = 1.2
# Markets whose underlying closes for the weekend while the perpetual keeps trading, thinly.
WEEKEND_CLOSED = ("XAU", "XAG", "XPT", "XPD")


def day_range(candles: list[Candle], period: int = ATR_PERIOD) -> float | None:
    """A normal day's range: the larger of the ATR over all days and over working days only.

    A contract whose underlying is closed at the weekend has two quiet candles
    a week, and they pull the average down. The working-day figure is the one
    a stop has to survive, so the larger of the two is used.
    """
    closed = [candle for candle in candles if candle.confirmed]
    weekdays = [c for c in closed if datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc).weekday() < 5]
    values = [value for value in (atr(closed, period), atr(weekdays, period)) if value]
    return max(values) if values else None


@dataclass(frozen=True)
class PositionRisk:
    position: Position
    notional: float | None = None  # what the position is worth, in its settlement currency
    leverage: float | None = None  # this position alone: notional / the account's equity
    atr_day: float | None = None  # a normal day's range of the price
    liq_gap: float | None = None  # from the mark price to the liquidation price
    liq_cost: float | None = None  # what is lost on the way there
    liq_cost_pct: float | None = None  # as a share of the equity
    stop_gap: float | None = None  # from the mark price to the stop-loss; None without one
    stop_cost: float | None = None  # what the stop would cost from here; 0 when it already locks in more than the mark
    stop_cost_pct: float | None = None
    funding_rate: float | None = None  # of the current period
    funding_hours: float | None = None  # how long a period is
    funding_cost: float | None = None  # per period, in the settlement currency; negative when the position is paid

    @property
    def liq_atr(self) -> float | None:
        """The distance to liquidation in days of normal range."""
        if self.liq_gap is None or not self.atr_day:
            return None
        return self.liq_gap / self.atr_day

    @property
    def stop_atr(self) -> float | None:
        if self.stop_gap is None or not self.atr_day:
            return None
        return self.stop_gap / self.atr_day


@dataclass(frozen=True)
class AccountRisk:
    equity: float | None
    positions: tuple[PositionRisk, ...] = ()

    @property
    def notional(self) -> float | None:
        values = [item.notional for item in self.positions]
        return None if not values or any(value is None for value in values) else sum(values)

    @property
    def leverage(self) -> float | None:
        """Every position together: what they are worth, divided by the equity."""
        if self.notional is None or not self.equity or self.equity <= 0:
            return None
        return self.notional / self.equity

    def of(self, position: Position) -> PositionRisk | None:
        for item in self.positions:
            if item.position.key == position.key:
                return item
        return None


@dataclass(frozen=True)
class Reading:
    """One measurement set against the owner's thresholds."""

    key: str  # what was measured, e.g. "leverage" or "liq:XAU-USDT-SWAP|cross|long"
    level: int  # 0 inside every threshold, then 1 notice, 2 warning, 3 urgent
    line: str  # the measurement in words
    release: int | None = None  # the level it counts as once reported: the lines eased by RELEASE_FACTOR
    subject: str = ""  # a few words for the card's title
    inst_ids: tuple[str, ...] = ()  # the positions it is about; empty for the whole account

    @property
    def held(self) -> int:
        return self.level if self.release is None else max(self.level, self.release)


def _notional(position: Position) -> float | None:
    if position.coins is not None and position.mark_px:
        return abs(position.coins) * position.mark_px
    return position.notional_usd


def measure(position: Position, equity: float | None, atr_day: float | None, funding: tuple[float, float] | None) -> PositionRisk:
    """The figures of one position. Pure arithmetic on what OKX reported."""
    notional = _notional(position)
    coins = position.coins if position.coins is not None else None
    mark = position.mark_px
    long = position.direction == "long"
    usable_equity = equity if equity and equity > 0 else None

    def share(amount: float | None) -> float | None:
        return None if amount is None or usable_equity is None else amount / usable_equity * 100.0

    liq_gap = liq_cost = None
    if mark and position.liq_px:
        liq_gap = abs(mark - position.liq_px)
        liq_cost = liq_gap * coins if coins is not None else None
    stop_gap = stop_cost = None
    stop = position.stop_loss
    if mark and stop is not None:
        stop_gap = max(0.0, mark - stop if long else stop - mark)
        stop_cost = stop_gap * coins if coins is not None else None
    rate = hours = cost = None
    if funding is not None:
        rate, hours = funding
        if notional is not None:
            cost = rate * notional * (1 if long else -1)  # a positive rate is paid by the longs
    return PositionRisk(
        position=position,
        notional=notional,
        leverage=None if notional is None or usable_equity is None else notional / usable_equity,
        atr_day=atr_day,
        liq_gap=liq_gap,
        liq_cost=liq_cost,
        liq_cost_pct=share(liq_cost),
        stop_gap=stop_gap,
        stop_cost=stop_cost,
        stop_cost_pct=share(stop_cost),
        funding_rate=rate,
        funding_hours=hours,
        funding_cost=cost,
    )


class RiskGauge:
    """Reads what the measurements need beyond the account itself: the daily range and the funding rate."""

    def __init__(self, okx, clock: Callable[[], float] = time.time):
        self._okx = okx
        self._clock = clock
        self._lock = threading.Lock()
        self._ranges: dict[str, tuple[float, float | None]] = {}
        self._funding: dict[str, tuple[float, tuple[float, float] | None]] = {}

    def day_range(self, inst_id: str) -> float | None:
        """A normal day's range of this instrument. None when OKX cannot be read: never an error."""
        now = self._clock()
        with self._lock:
            known = self._ranges.get(inst_id)
        if known and now - known[0] < RANGE_TTL_SECONDS:
            return known[1]
        try:
            value = day_range(self._okx.candles(inst_id, DAY_BAR, DAY_CANDLES))
        except (OkxError, OkxNetworkError) as exc:
            log.info("daily range of %s not read: %s", inst_id, exc)
            return known[1] if known else None  # an hour-old figure is better than none
        with self._lock:
            self._ranges[inst_id] = (now, value)
        return value

    def funding(self, inst_id: str) -> tuple[float, float] | None:
        """(rate of the current period, hours in a period). None for anything without funding."""
        if not inst_id.endswith("-SWAP"):
            return None
        now = self._clock()
        with self._lock:
            known = self._funding.get(inst_id)
        if known and now - known[0] < FUNDING_TTL_SECONDS:
            return known[1]
        try:
            value = self._okx.funding(inst_id)
        except (OkxError, OkxNetworkError) as exc:
            log.info("funding rate of %s not read: %s", inst_id, exc)
            return known[1] if known else None
        with self._lock:
            self._funding[inst_id] = (now, value)
        return value

    def assess(self, state: AccountState) -> AccountRisk:
        equity = state.snapshot.total_eq
        return AccountRisk(
            equity=equity,
            positions=tuple(
                measure(position, equity, self.day_range(position.inst_id), self.funding(position.inst_id))
                for position in state.positions
            ),
        )


# ------------------------------------------------------------------------- words


def _num(value: float) -> str:
    return f"{value:,.2f}"


def leverage_line(account: AccountRisk) -> str:
    """ "" when it cannot be worked out."""
    if account.leverage is None:
        return ""
    return f"实际杠杆 {account.leverage:.1f} 倍（全部仓位的名义价值 {_num(account.notional)} ÷ 账户权益 {_num(account.equity)}）"


def liq_line(risk: PositionRisk) -> str:
    position = risk.position
    if not position.liq_px or risk.liq_gap is None:
        return ""
    text = f"预估强平价 {_num(position.liq_px)}，离标记价 {_num(risk.liq_gap)}"
    if position.liq_distance_pct is not None:
        text += f"（{position.liq_distance_pct:.2f}%）"
    if risk.liq_atr is not None:
        text += f"，相当于 {risk.liq_atr:.1f} 天的正常波幅（日线 ATR {_num(risk.atr_day)}）"
    return text


def stop_line(risk: PositionRisk) -> str:
    """What the stop would cost; without a stop, what the way to liquidation costs."""
    ccy = risk.position.pnl_ccy
    if risk.position.stop_loss is None:
        if risk.liq_cost is None:
            return "没有止损"
        share = f"，占权益 {risk.liq_cost_pct:.0f}%" if risk.liq_cost_pct is not None else ""
        return f"没有止损：价格走到强平价，这个仓位亏约 {_num(risk.liq_cost)} {ccy}{share}"
    if risk.stop_cost is None:
        return ""
    if risk.stop_cost == 0:
        return f"止损 {_num(risk.position.stop_loss)} 已经在标记价的有利一侧"
    share = f"，占权益 {risk.stop_cost_pct:.1f}%" if risk.stop_cost_pct is not None else ""
    text = f"止损 {_num(risk.position.stop_loss)} 触发时比现在少约 {_num(risk.stop_cost)} {ccy}{share}"
    if risk.stop_atr is not None:
        text += f"；止损离标记价 {risk.stop_atr:.1f} 天的正常波幅"
    return text


def funding_line(risk: PositionRisk) -> str:
    """ "" also when the funding comes to nothing worth a line (a rate of 0)."""
    if risk.funding_rate is None or risk.funding_cost is None or not risk.funding_hours:
        return ""
    if abs(risk.funding_cost) < 0.005:
        return ""
    ccy = risk.position.pnl_ccy
    per_day = risk.funding_cost * 24.0 / risk.funding_hours
    verb = "付" if risk.funding_cost > 0 else "收"
    return (
        f"资金费 (funding) 本期 {risk.funding_rate * 100:+.4f}%，按现在的仓位每 {risk.funding_hours:g} 小时{verb}约 "
        f"{_num(abs(risk.funding_cost))} {ccy}，照这个费率一天约 {_num(abs(per_day))} {ccy}"
    )


def position_risk_lines(risk: PositionRisk) -> list[str]:
    """The lines a position card carries below the position itself. Amounts, so for the owner's eyes."""
    return [line for line in (liq_line(risk), stop_line(risk), funding_line(risk)) if line]


# -------------------------------------------------------------------- thresholds


def _below(value: float | None, thresholds: tuple[float, ...]) -> int:
    """How many of the thresholds the value is under: the level of something that is worse when smaller."""
    return 0 if value is None else sum(1 for threshold in thresholds if value < threshold)


def _above(value: float | None, thresholds: tuple[float, ...]) -> int:
    return 0 if value is None else sum(1 for threshold in thresholds if value > threshold)


def _list(thresholds: tuple[float, ...]) -> str:
    return "、".join(f"{value:g}" for value in thresholds)


def stop_protects(risk: PositionRisk) -> bool:
    """Whether the stop-loss goes off before the price can reach the liquidation price. Then how far
    liquidation is does not matter: the position is out first."""
    position = risk.position
    stop = position.stop_loss
    if stop is None:
        return False
    if not position.liq_px:
        return True
    return stop > position.liq_px if position.direction == "long" else stop < position.liq_px


def readings(account: AccountRisk, cfg) -> list[Reading]:
    """Every measurement that has a threshold, with its level. Levels of 0 are included.

    What a stop-loss would cost is not among them: the owner chooses that when he sets the stop, and
    it is told with the stop's change (monitor.py), not again each time the price drifts."""
    found: list[Reading] = []
    many = len(account.positions) > 1
    if cfg.alert_leverage and account.leverage is not None:
        level = _above(account.leverage, cfg.alert_leverage)
        release = _above(account.leverage, tuple(line / RELEASE_FACTOR for line in cfg.alert_leverage))
        move = f"价格每逆向走 1%，权益变动约 {account.leverage:.0f}%" if account.leverage >= 1 else ""
        line = "。".join(part for part in (leverage_line(account), move, f"你设的线：超过 {_list(cfg.alert_leverage)} 倍") if part)
        subject = f"实际杠杆 {account.leverage:.0f} 倍"
        found.append(Reading("leverage", level, line, release, subject))
    for risk in account.positions:
        position = risk.position
        side = f"{'多' if position.direction == 'long' else '空'}单"
        name = f"{position.inst_id} {side}：" if many else ""
        tag = "|".join(position.key)
        if cfg.alert_liq_atr and risk.liq_atr is not None and not stop_protects(risk):
            level = _below(risk.liq_atr, cfg.alert_liq_atr)
            release = _below(risk.liq_atr, tuple(line * RELEASE_FACTOR for line in cfg.alert_liq_atr))
            if position.stop_loss is None:
                why = "没有止损"
            else:
                why = f"止损 {_num(position.stop_loss)} 在强平价外面，价格先到强平价"
            line = f"{name}{why}，{liq_line(risk)}。你设的线：不到 {_list(cfg.alert_liq_atr)} 天"
            subject = f"{position.inst_id.split('-')[0]} {side}{'没有止损' if position.stop_loss is None else '止损在强平价外'}"
            found.append(Reading(f"liq:{tag}", level, line, release, subject, (position.inst_id,)))
    return found


def weekend_lines(account: AccountRisk, cfg) -> list[str]:
    """What stands out before a weekend, for markets whose underlying closes. Empty when nothing does."""
    lines: list[str] = []
    held = [risk for risk in account.positions if risk.position.inst_id.split("-")[0].upper() in WEEKEND_CLOSED]
    if not held:
        return lines
    if cfg.weekend_leverage and account.leverage is not None and account.leverage > cfg.weekend_leverage:
        lines.append(f"{leverage_line(account)}，高于你设的周末线 {cfg.weekend_leverage:g} 倍")
    for risk in held:
        position = risk.position
        name = f"{position.inst_id} {'多' if position.direction == 'long' else '空'}单"
        if position.stop_loss is None:
            lines.append(f"{name}：{stop_line(risk)}")
        elif risk.stop_atr is not None and risk.stop_atr < cfg.weekend_stop_atr:
            lines.append(
                f"{name}：止损 {_num(position.stop_loss)} 离标记价只有 {risk.stop_atr:.1f} 天的正常波幅"
                f"（你设的周末线是 {cfg.weekend_stop_atr:g} 天），周末成交稀薄时容易被一次插针打到"
            )
        if risk.liq_atr is not None and cfg.alert_liq_atr and risk.liq_atr < max(cfg.alert_liq_atr):
            lines.append(f"{name}：{liq_line(risk)}")
    return lines
