"""Post-trade review: each closed position is checked against what was planned, and one lesson is kept.

When the monitor sees a position closed in full, the program works out from OKX's own records, the
bot's snapshots and OKX's candles:

  * how it closed, the open and close price, how long it was held, the realised P&L;
  * the first stop seen on it and how often the stop moved;
  * the R-multiple: the move from the open price to the close price, in units of the distance from
    the open price to that first stop (one R is what the stop would have lost);
  * what the stop would have cost against the equity at the open, next to the trader's own line;
  * the largest move against and in favour of the position while it was open (MAE and MFE),
    from candles, also in R;
  * the last analysis with levels before the open, and the bot's own opening order if it placed one.

The AI then writes three lines (against the plan, one lesson, the trader's rules). Figures come
from the program, never from the AI. The lesson is kept in the journal; later answers and briefs
read the most recent ones first, so the next analysis starts from what the last trades taught.

Reads only. Nothing here places, changes or cancels an order.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .closes import FULL_CLOSE_TYPES, Closed, how_it_closed, read_closed, read_triggers
from . import sources
from .models import Candle, coin_name

log = logging.getLogger(__name__)

RECORD_WAIT_SECONDS = 30  # OKX writes the position history a little after the position is gone
RECORD_MARGIN_SECONDS = 15 * 60  # a record this much older than the close the monitor saw is not this close
ANALYSIS_LOOKBACK_SECONDS = 48 * 3600
ORDER_WINDOW_SECONDS = (15 * 60, 2 * 60)  # an opening order this long before / after the position appeared
LESSONS_SHOWN = 5
LESSONS_DAYS = 90
BARS = 300  # one request
# Candle size by how long the position was held: 300 candles must cover the whole holding.
BAR_CHOICES = ((70 * 3600, "15m", 15 * 60), (12 * 86400, "1H", 3600), (48 * 86400, "4H", 4 * 3600))
SESSIONS = ((0, 7, "亚洲盘"), (7, 13, "欧洲盘"), (13, 21, "美国盘"), (21, 24, "美盘收盘后"))  # UTC hours
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_LESSON = re.compile(r"^[\s>*\-•]*(?:\*\*)?教训(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*(.+)$", re.MULTILINE)


# ------------------------------------------------------------------ figures


def stop_of(position: dict) -> float | None:
    """The stop that would trigger first, for a position as the journal stored it."""
    prices = [s.get("sl_trigger_px") for s in position.get("stops") or () if isinstance(s, dict) and s.get("sl_trigger_px")]
    if not prices:
        return None
    return max(prices) if position.get("direction") == "long" else min(prices)


@dataclass(frozen=True)
class StopTrail:
    first: float | None  # the first stop seen on the position
    last: float | None
    moves: int  # times the stop changed (set, moved or removed) after the first sighting
    path: tuple[float | None, ...]  # each distinct stop in turn
    seen_from: float | None  # the first snapshot that holds the position
    equity_at_open: float | None


def stop_trail(snapshots: list[dict], inst_id: str, direction: str) -> StopTrail:
    """How the stop on one position went, from the journal's snapshots (oldest first)."""
    path: list[float | None] = []
    seen_from = equity = None
    for snapshot in snapshots:
        match = next(
            (p for p in snapshot["positions"] if p.get("inst_id") == inst_id and p.get("direction") == direction), None
        )
        if match is None:
            continue
        if seen_from is None:
            seen_from, equity = snapshot["ts"], snapshot.get("total_eq")
        stop = stop_of(match)
        if not path or path[-1] != stop:
            path.append(stop)
    first = next((stop for stop in path if stop is not None), None)
    return StopTrail(first, path[-1] if path else None, max(len(path) - 1, 0), tuple(path), seen_from, equity)


def r_multiple(direction: str, open_px: float | None, close_px: float | None, stop: float | None) -> float | None:
    """The move from open to close in units of the open-to-stop distance. None without a stop that risked something."""
    risk = _risk(direction, open_px, stop)
    if risk is None or close_px is None:
        return None
    move = close_px - open_px if direction == "long" else open_px - close_px
    return move / risk


def _risk(direction: str, open_px: float | None, stop: float | None) -> float | None:
    """Price distance from the open to a stop on the losing side; None when the stop risked nothing."""
    if open_px is None or stop is None:
        return None
    risk = open_px - stop if direction == "long" else stop - open_px
    return risk if risk > 0 else None


def bar_for(seconds: float) -> tuple[str, int] | None:
    for longest, bar, size in BAR_CHOICES:
        if seconds <= longest:
            return bar, size
    return None


def excursions(direction: str, open_px: float, candles: list[Candle], start: float, end: float, bar_seconds: int) -> tuple[float, float] | None:
    """The largest move against (MAE) and in favour of (MFE) the position while it was open, in price.

    Candles that overlap the holding are counted whole, so a few minutes either side may be included.
    None when no candle covers the holding."""
    inside = [c for c in candles if c.ts / 1000 < end and c.ts / 1000 + bar_seconds > start]
    if not inside:
        return None
    low, high = min(c.low for c in inside), max(c.high for c in inside)
    if direction == "long":
        return max(open_px - low, 0.0), max(high - open_px, 0.0)
    return max(high - open_px, 0.0), max(open_px - low, 0.0)


def session_of(ts: float) -> str:
    hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
    return next(name for start, stop, name in SESSIONS if start <= hour < stop)


# -------------------------------------------------------------------- facts


@dataclass(frozen=True)
class TradeFacts:
    closed: Closed
    how: str
    trail: StopTrail
    coins: float | None
    coin_ccy: str
    planned_loss: float | None  # what the first stop would have lost, in the settlement currency
    risk_pct: float | None  # the same against the equity at the open
    r: float | None
    mae_px: float | None
    mfe_px: float | None
    bar: str
    analysis: str
    order: str
    source: str = ""  # where the opening order came from (sources.SOURCE_NAMES); "manual" when the bot did not place it
    unit: str = "张"  # what the closed size counts: 张 on OKX, 手 (lots) on MT5

    def per_r(self, distance: float | None) -> float | None:
        risk = _risk(self.closed.direction, self.closed.open_px, self.trail.first)
        return None if risk is None or distance is None else distance / risk

    def as_row(self, text: str, lesson: str) -> dict:
        c = self.closed
        mae, mfe = self.per_r(self.mae_px), self.per_r(self.mfe_px)
        return dict(
            inst_id=c.inst_id, direction=c.direction, opened_at=int(c.opened_at) if c.opened_at else None,
            closed_at=int(c.closed_at), open_px=c.open_px, close_px=c.close_px, realized=c.realized, ccy=c.ccy,
            how=self.how, first_stop=self.trail.first, stop_moves=self.trail.moves, risk_pct=self.risk_pct,
            r_multiple=self.r, mae_r=mae, mfe_r=mfe, facts=facts_text(self, None, None), text=text, lesson=lesson,
            source=self.source,
        )  # fmt: skip


def _px(value: float | None) -> str:
    return "?" if value is None else f"{value:,.2f}"


def _held(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    return f"{hours / 24:.1f} 天"


def facts_text(facts: TradeFacts, tz, rule_pct: float | None) -> str:
    """The program's figures for one closed position, in plain lines. The AI reads them; the card shows them."""
    c = facts.closed
    side = "多单" if c.direction == "long" else "空单"
    when = (lambda ts: datetime.fromtimestamp(ts, tz=tz or timezone.utc).strftime("%m-%d %H:%M"))
    if facts.unit == "手":  # MT5: the lots first, as the terminal shows them
        size = f"{c.closed_size:,.10g} 手" if c.closed_size else "数量不明"
        if facts.coins and c.closed_size:
            size += f"（{facts.coins:,.8g} {coin_name(facts.coin_ccy, facts.unit)}）"
    else:
        size = f"{facts.coins:,.8g} {facts.coin_ccy}（{c.closed_size:,.10g} 张）" if facts.coins and c.closed_size else (
            f"{c.closed_size:,.10g} 张" if c.closed_size else "数量不明"
        )
    lines = [f"{c.inst_id} {side} {size}，{facts.how}"]
    held = ""
    if c.opened_at:
        held = f"，{when(c.opened_at)} 开仓，{when(c.closed_at)} 平仓，持仓 {_held(c.closed_at - c.opened_at)}"
        held += f"（开在{session_of(c.opened_at)}）"
    lines.append(f"开仓均价 {_px(c.open_px)}，平仓均价 {_px(c.close_px)}{held}")
    if c.realized is not None:
        parts = [f"{label} {value:+,.2f}" for label, value in (("价差", c.pnl), ("手续费", c.fee), ("资金费", c.funding)) if value]
        lines.append(f"已实现盈亏 {c.realized:+,.2f} {c.ccy}" + (f"（{'，'.join(parts)}）" if parts else ""))
    trail = facts.trail
    if trail.first is None:
        lines.append("止损：机器人的记录里这个仓位一直没有止损" if trail.seen_from else "止损：机器人没有这个仓位的记录（开仓时它可能没在运行）")
    else:
        path = " → ".join("无" if stop is None else _px(stop) for stop in trail.path[:8])
        moved = f"，动过 {trail.moves} 次：{path}" if trail.moves else "，一直没动"
        late = ""
        if c.opened_at and trail.seen_from and trail.seen_from - c.opened_at > 20 * 60:
            late = f"（机器人从 {when(trail.seen_from)} 起才有这个仓位的记录）"
        lines.append(f"第一次看到的止损 {_px(trail.first)}{moved}{late}")
    if facts.planned_loss is not None:
        share = f"，占开仓时权益的 {facts.risk_pct:.1f}%" if facts.risk_pct is not None else ""
        rule = f"（交易规则：单笔止损最多亏权益的 {rule_pct:g}%）" if rule_pct and facts.risk_pct is not None else ""
        lines.append(f"按第一次的止损，这笔最多亏约 {facts.planned_loss:,.2f} {c.ccy}{share}{rule}")
    if facts.r is not None:
        lines.append(f"结果 {facts.r:+.2f}R（1R = 开仓价到第一次止损的距离，按价格算，不含手续费）")
    elif trail.first is not None:
        lines.append("R 倍数算不出：第一次看到止损时，它已经不在亏损的一边")
    if facts.mae_px is not None and facts.mfe_px is not None:
        mae_r, mfe_r = facts.per_r(facts.mae_px), facts.per_r(facts.mfe_px)
        in_r = lambda value: f"（{value:.2f}R）" if value is not None else ""  # noqa: E731
        lines.append(
            f"持仓期间最多逆向 {_px(facts.mae_px)}{in_r(mae_r)}，最多顺向 {_px(facts.mfe_px)}{in_r(mfe_r)}"
            f"（按 {facts.bar} K 线估算，首尾两根整根计入）"
        )
    lines.append(facts.analysis)
    lines.append(facts.order)
    return "\n".join(line for line in lines if line)


def lesson_of(text: str) -> str:
    found = _LESSON.search(text)
    return found.group(1).strip().strip("*").strip() if found else ""


def lessons_text(lessons: list[dict], tz) -> str:
    """The latest lessons, for the AI to read before it answers."""
    if not lessons:
        return ""
    lines = ["最近几笔平仓复盘记下的教训（新的在前，程序记的，不是交易员说的）："]
    for row in lessons:
        when = datetime.fromtimestamp(row["closed_at"], tz=tz).strftime("%m-%d")
        side = "多" if row["direction"] == "long" else "空"
        r = f" {row['r_multiple']:+.1f}R" if row["r_multiple"] is not None else ""
        lines.append(f"- {when} {row['inst_id']} {side}{r}：{row['lesson']}")
    return "\n".join(lines)


def week_text(reviews: list[dict]) -> str:
    """The week's closed positions in figures, for the weekly review. Computed here, not by the AI."""
    if not reviews:
        return "本周没有复盘过的平仓。"
    realized = [r["realized"] for r in reviews if r["realized"] is not None]
    wins = sum(1 for value in realized if value > 0)
    ccy = next((r["ccy"] for r in reviews if r["ccy"]), "")
    lines = [f"本周复盘过的平仓 {len(reviews)} 笔：赚 {wins} 笔，亏 {len(realized) - wins} 笔，已实现合计 {sum(realized):+,.2f} {ccy}"]
    rs = [r["r_multiple"] for r in reviews if r["r_multiple"] is not None]
    if rs:
        lines.append(
            f"有 R 的 {len(rs)} 笔：平均 {sum(rs) / len(rs):+.2f}R（这就是期望值 expectancy），"
            f"最好 {max(rs):+.2f}R，最差 {min(rs):+.2f}R"
        )
    maes = [r["mae_r"] for r in reviews if r["mae_r"] is not None]
    mfes = [r["mfe_r"] for r in reviews if r["mfe_r"] is not None]
    if maes and mfes:
        lines.append(f"持仓期间平均最多逆向 {sum(maes) / len(maes):.2f}R，平均最多顺向 {sum(mfes) / len(mfes):.2f}R")
    moved = sum(1 for r in reviews if r["stop_moves"])
    bare = sum(1 for r in reviews if r["first_stop"] is None)
    if moved or bare:
        lines.append(f"止损动过的 {moved} 笔，记录里没有止损的 {bare} 笔")
    for label, key in (("按开仓时段", lambda r: session_of(r["opened_at"])), ("按开仓的星期", lambda r: WEEKDAYS[datetime.fromtimestamp(r["opened_at"], tz=timezone.utc).weekday()])):
        groups: dict[str, list[dict]] = {}
        for row in reviews:
            if row["opened_at"]:
                groups.setdefault(key(row), []).append(row)
        if groups:
            parts = []
            for name, rows in groups.items():
                total = sum(r["realized"] or 0 for r in rows)
                parts.append(f"{name} {len(rows)} 笔 {total:+,.2f}")
            lines.append(f"{label}（UTC）：" + "；".join(parts))
    by_source = sources.week_line(reviews)
    if by_source:
        lines.append(by_source)
    return "\n".join(lines)


# ------------------------------------------------------------------ reviewer


class Reviewer:
    """Reviews a position once it is closed in full. Runs on its own thread; never touches an order."""

    def __init__(
        self,
        okx,
        journal,
        analyst,
        cfg,
        reporter,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._okx = okx
        self._journal = journal
        self._analyst = analyst
        self._cfg = cfg
        self._reporter = reporter  # sends to the owner, and to the group when it shares the account
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()

    def on_close(self, inst_id: str) -> None:
        seen_at = self._clock()
        threading.Thread(target=self._run, args=(inst_id, seen_at), name="review", daemon=True).start()

    def _run(self, inst_id: str, seen_at: float) -> None:
        try:
            self.review(inst_id, seen_at)
        except Exception:
            log.exception("could not review the close of %s", inst_id)

    def review(self, inst_id: str, seen_at: float) -> bool:
        """Returns whether a review was written and sent."""
        with self._lock:  # two closes at once are reviewed one after the other
            closed, triggers = self._record(inst_id, seen_at)
            if closed is None:
                log.info("review of %s: no full close in OKX's position history; skipped", inst_id)
                return False
            if self._journal.has_review(inst_id, closed.closed_at):
                return False
            facts = self.facts(closed, triggers)
            figures = facts_text(facts, self._cfg.tz, self._cfg.risk_per_trade_pct)
            lessons = lessons_text(self._journal.recent_lessons(3, self._clock() - LESSONS_DAYS * 86400), self._cfg.tz)
            text = ""
            shared = self._reporter.group_shares_account
            try:
                text = self._analyst.review_trade(figures, lessons, shared_with_group=shared)
            except Exception as exc:  # the figures still go out, and are kept
                log.warning("review of %s: the AI could not write it (%s)", inst_id, exc)
            lesson = lesson_of(text)
            self._journal.record_review(ts=self._clock(), **facts.as_row(text, lesson))
            side = "多单" if closed.direction == "long" else "空单"
            body = figures + ("\n\n" + text if text else "\n\n（这次 AI 没写出复盘，上面是程序算的数字。）")
            self._reporter.send_review(f"复盘：{inst_id} {side}", body)
            return True

    def _record(self, inst_id: str, seen_at: float):
        for attempt in range(2):
            try:
                history, triggers = read_closed(self._okx), read_triggers(self._okx)
            except Exception as exc:
                log.info("review of %s: OKX's records could not be read (%s)", inst_id, type(exc).__name__)
                history, triggers = [], []
            recent = [
                c for c in history
                if c.inst_id == inst_id and c.close_type in FULL_CLOSE_TYPES and c.closed_at >= seen_at - RECORD_MARGIN_SECONDS
            ]  # fmt: skip
            if recent:
                return recent[-1], triggers
            if attempt == 0:
                self._sleep(RECORD_WAIT_SECONDS)
        return None, []

    def facts(self, closed: Closed, triggers) -> TradeFacts:
        opening = self._opening_order(closed)
        start = closed.opened_at or closed.closed_at
        trail = stop_trail(self._journal.snapshots_between(start - 60, closed.closed_at + 60), closed.inst_id, closed.direction)
        coins, coin_ccy, unit = self._coins(closed)
        risk = _risk(closed.direction, closed.open_px, trail.first)
        planned = risk * coins if risk is not None and coins else None
        risk_pct = planned / trail.equity_at_open * 100 if planned is not None and trail.equity_at_open else None
        mae = mfe = None
        bar = ""
        if closed.opened_at and closed.open_px is not None:
            choice = bar_for(closed.closed_at - closed.opened_at)
            if choice is not None:
                bar, size = choice
                try:
                    candles = self._okx.candles(closed.inst_id, bar, BARS)
                    found = excursions(closed.direction, closed.open_px, candles, closed.opened_at, closed.closed_at, size)
                    if found is not None:
                        mae, mfe = found
                except Exception as exc:
                    log.info("review: no candles for %s (%s)", closed.inst_id, type(exc).__name__)
        return TradeFacts(
            closed=closed,
            how=how_it_closed(closed, triggers),
            trail=trail,
            coins=coins,
            coin_ccy=coin_ccy,
            unit=unit,
            planned_loss=planned,
            risk_pct=risk_pct,
            r=r_multiple(closed.direction, closed.open_px, closed.close_px, trail.first),
            mae_px=mae,
            mfe_px=mfe,
            bar=bar,
            analysis=self._analysis_line(closed),
            order=self._order_line(opening),
            source=self._source(closed, opening),
        )

    def _coins(self, closed: Closed) -> tuple[float | None, str, str]:
        """(coins, their currency, what the closed size counts in)."""
        if not closed.closed_size:
            return None, "", "张"
        try:
            spec = self._okx.instrument_spec(closed.inst_id)
        except Exception as exc:
            log.info("review: no contract size for %s (%s)", closed.inst_id, type(exc).__name__)
            return None, "", "张"
        return closed.closed_size * float(spec.coins_per_contract), spec.ct_val_ccy, spec.unit

    def _analysis_line(self, closed: Closed) -> str:
        if not closed.opened_at:
            return ""
        start = closed.opened_at - ANALYSIS_LOOKBACK_SECONDS
        analyses = self._journal.analyses_between(start, closed.opened_at + 60, closed.inst_id)
        if not analyses:
            return "开仓前 48 小时里，AI 没有给过这个品种带点位的分析"
        last = analyses[-1]
        when = datetime.fromtimestamp(last["ts"], tz=self._cfg.tz).strftime("%m-%d %H:%M")
        bias = {"long": "偏多", "short": "偏空", "range": "震荡"}.get(last["bias"], "没写方向")
        calls = [c for c in self._journal.calls_between(last["ts"], last["ts"] + 1, closed.inst_id) if c["analysis_id"] == last["id"]]
        scenarios = "；".join(
            f"{'多头' if c['side'] == 'long' else '空头'} 触发 {_px(c['trigger_px'])} 目标 {_px(c['target_px'])} 失效 {_px(c['invalid_px'])}"
            + (f"，把握 {c['confidence']}%" if c.get("confidence") is not None else "")
            for c in calls
        )
        question = f"，问题“{last['question']}”" if last["question"] else ""
        return f"开仓前最后一次带点位的分析：{when}{question}，{bias}" + (f"；情景：{scenarios}" if scenarios else "")

    def _opening_order(self, closed: Closed) -> dict | None:
        """The bot's order that opened this position: an opening order sent just before it opened. None when
        the open time is not known, and {} when the bot sent none (the owner opened it in OKX)."""
        if not closed.opened_at:
            return None
        before, after = ORDER_WINDOW_SECONDS
        orders = [
            o for o in self._journal.orders_between(closed.opened_at - before, closed.opened_at + after)
            if o["inst_id"] == closed.inst_id and o["action"] == "open" and o["result"] == "ok"
        ]  # fmt: skip
        return orders[-1] if orders else {}

    @staticmethod
    def _order_line(opening: dict | None) -> str:
        if opening is None:
            return ""
        if not opening:
            return "开仓单不是机器人下的（在 OKX 里手动下的）"
        how = f"（来源：{sources.name(opening['source'])}）" if opening.get("source") else ""
        return f"机器人下的开仓单：{opening['summary']}{how}"

    @staticmethod
    def _source(closed: Closed, opening: dict | None) -> str:
        if opening is None:
            return ""
        return (opening.get("source") or "") if opening else "manual"
