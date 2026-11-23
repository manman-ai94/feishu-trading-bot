"""Trading discipline: the day's plan, the checklist a trade idea must pass, and when the day stops.

Rules in code. Only the plan itself is written by the AI, once a day:

  * The trading day follows gold's own session. It ends at 17:00 New York time (14:00 Los Angeles,
    06:00 Beijing in winter, 05:00 in summer) and the next one begins.
  * Each trading day needs a plan. At 17:05 New York, Sunday to Thursday, the AI writes one for the day
    that is beginning: which way (只做多, 只做空, both ways in a range, or 观望), the key levels and how to
    meet each way the market may go. It can be asked for at any time ("出今天的计划") when there is none,
    and made again once a day ("重做计划").
  * With PLAN_REQUIRED, no new position is opened on a day without a plan, or against its direction:
    the order is refused, not merely warned about.
  * At most PLAN_MAX_TRADES new positions a day (additions to a position do not count). After
    LOSS_STREAK_STOP losing trades in a row, or once the day's realised loss reaches PLAN_MAX_LOSS_PCT of
    the equity the day began with, no new position until the next trading day. Nothing lifts it early.
  * A trade idea the AI gives has to pass the checklist (`check_setup`): the plan's direction, the
    4-hour trend with it and the daily trend not against it, the entry near a key level, at least
    MIN_REWARD_RISK reward to risk, no major US data within half an hour, and the day not stopped.
    Otherwise the AI answers "观望" and says what is missing.

Only new positions are held back. Closing, reducing, cancelling and moving stops always work.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Callable
from zoneinfo import ZoneInfo

from . import econ
from .closes import FULL_CLOSE_TYPES, Closed, read_closed
from .indicators import atr, ema
from .okx_client import OkxError, OkxNetworkError
from .times import both

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
DAY_ENDS_NY = 17  # gold's daily session ends at 17:00 New York time, all year round
PLAN_AT_NY = (17, 5)  # when the plan for the day that is beginning is written
PLAN_WEEKDAYS_NY = (6, 0, 1, 2, 3)  # Sunday to Thursday evenings: the plans for Monday to Friday
PLAN_REVISIONS = 1  # "重做计划" this many times a day
MIN_REWARD_RISK = 2.0
NEWS_AHEAD_SECONDS = 30 * 60
NEWS_AFTER_SECONDS = 15 * 60  # the first quarter of an hour after a release is as wild as the run-up
KEY_LEVEL_ATR = 0.25  # "near a key level": within this share of a normal day's range
MAX_LEVELS = 4
STATUS_TTL_SECONDS = 60.0
STOP_TOLD_SETTING = "day_stop_told"  # the trading day whose stop was announced
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

BIASES = {"long": "只做多", "short": "只做空", "both": "两边都可以（区间里高抛低吸）", "none": "观望，不开新仓"}
NO_PLAN_WORDS = "发“出今天的计划”，我现在就写一份"

_PLAN = re.compile(r"\s*<plan>(.*?)</plan>[ \t]*", re.DOTALL | re.IGNORECASE)
_PLAN_DANGLING = re.compile(r"\s*<plan>.*\Z", re.DOTALL | re.IGNORECASE)
_INSTRUMENT = re.compile(r"^[A-Z0-9]{1,12}(-[A-Z0-9]{1,12}){1,2}$")

PLAN_NOTE = """

给程序的记录：
在全文的最后另起一行，附一段给程序读的计划，交易员看不到这一段。程序靠它拦下和计划不符的开仓，所以必须和正文一致。格式严格如下，写在一行里，不要放进代码块：
<plan>{"items":[{"inst":"XAU-USDT-SWAP","bias":"long","support":[支撑位],"resistance":[阻力位]}]}</plan>
inst 写 OKX 的品种代码。bias 只能是 long（只做多）、short（只做空）、both（区间里两边都可以）或 none（观望，不开新仓）。support 和 resistance 各写一到四个最关键的价位，普通数字，不带逗号和引号，和正文里的数字完全一致。每个要做计划的品种写一项。"""


# ------------------------------------------------------------------ the trading day


def trading_day(ts: float) -> date:
    """The trading day a moment belongs to: from 17:00 New York the next day's session has begun."""
    ny = datetime.fromtimestamp(ts, NEW_YORK)
    return (ny + timedelta(days=1)).date() if ny.hour >= DAY_ENDS_NY else ny.date()


def day_bounds(day: date) -> tuple[float, float]:
    """(start, end) of a trading day, epoch seconds: 17:00 New York the evening before, to 17:00 that day."""
    start = datetime.combine(day - timedelta(days=1), clock_time(DAY_ENDS_NY), NEW_YORK)
    end = datetime.combine(day, clock_time(DAY_ENDS_NY), NEW_YORK)
    return start.timestamp(), end.timestamp()


def day_label(day: date) -> str:
    return f"{day:%m-%d} {WEEKDAYS[day.weekday()]}"


def plan_due(now: float) -> date | None:
    """The day whose plan is written now: just after the session ends on Sunday to Thursday. None otherwise."""
    ny = datetime.fromtimestamp(now, NEW_YORK)
    if ny.weekday() not in PLAN_WEEKDAYS_NY:
        return None
    return trading_day(now)


def plan_time(now: float) -> datetime:
    """Today's moment for writing the plan, in New York time."""
    ny = datetime.fromtimestamp(now, NEW_YORK)
    return ny.replace(hour=PLAN_AT_NY[0], minute=PLAN_AT_NY[1], second=0, microsecond=0)


# ------------------------------------------------------------------------ the plan


@dataclass(frozen=True)
class InstrumentPlan:
    inst_id: str
    bias: str  # long | short | both | none
    support: tuple[float, ...] = ()
    resistance: tuple[float, ...] = ()

    def allows(self, direction: str) -> bool:
        return self.bias == "both" or self.bias == direction

    def as_row(self) -> dict:
        return {"inst": self.inst_id, "bias": self.bias, "support": list(self.support), "resistance": list(self.resistance)}

    @property
    def base(self) -> str:
        return self.inst_id.split("-")[0]


@dataclass(frozen=True)
class DayPlan:
    id: int
    day: str  # ISO date of the trading day
    ts: float
    items: tuple[InstrumentPlan, ...]
    text: str
    revision: int = 0

    def item(self, inst_id: str) -> InstrumentPlan | None:
        return next((item for item in self.items if item.inst_id == inst_id), None)


def _levels(raw) -> tuple[float, ...]:
    values = []
    for value in raw if isinstance(raw, list) else ():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in values:
            values.append(number)
    return tuple(values[:MAX_LEVELS])


def parse_plan(raw: str) -> tuple[InstrumentPlan, ...] | None:
    try:
        data = json.loads(raw.strip())
    except ValueError:
        return None
    rows = data.get("items") if isinstance(data, dict) and "items" in data else [data]
    items: list[InstrumentPlan] = []
    for row in rows if isinstance(rows, list) else ():
        item = _item(row)
        if item is not None and all(known.inst_id != item.inst_id for known in items):
            items.append(item)
    return tuple(items) or None


def _item(row) -> InstrumentPlan | None:
    if not isinstance(row, dict):
        return None
    inst_id = str(row.get("inst", "")).strip().upper()
    bias = str(row.get("bias", "")).strip().lower()
    if not _INSTRUMENT.match(inst_id) or bias not in BIASES:
        return None
    return InstrumentPlan(inst_id, bias, _levels(row.get("support")), _levels(row.get("resistance")))


def extract_plan(text: str) -> tuple[str, tuple[InstrumentPlan, ...] | None]:
    """Cuts the <plan> block out of the AI's plan. Returns the text to show and what the block said."""
    found = _PLAN.search(text)
    if found is None:
        return _tidy(_PLAN_DANGLING.sub("", text)), None
    return _tidy(_PLAN.sub("\n\n", text)), parse_plan(found.group(1))


def _tidy(text: str) -> str:
    text = re.sub(r"```[a-zA-Z]*\s*```", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _num(value: float | None) -> str:
    """A price as it is typed in a command: no thousands separator, no trailing zeros."""
    if value is None:
        return "未知"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def levels_text(item: InstrumentPlan) -> str:
    parts = []
    if item.support:
        parts.append("支撑 " + "、".join(_num(v) for v in item.support))
    if item.resistance:
        parts.append("阻力 " + "、".join(_num(v) for v in item.resistance))
    return "；".join(parts)


# ----------------------------------------------------------------- how the day goes


@dataclass(frozen=True)
class DayStatus:
    day: date
    opened: int  # new positions opened this trading day
    streak: int  # losing trades in a row, up to the latest
    finished: int  # trades closed in full this day
    realized: float  # what the closes of this day made, fees and funding included
    ccy: str
    start_equity: float | None
    loss_cap: float | None  # the day stops at a realised loss of this much
    stopped: str  # "" while new positions may be opened; else why not, in words


def trades(closed: list[Closed]) -> dict[tuple, list[Closed]]:
    """OKX's close records grouped by position: one entry per trade, from its opening to its last close."""
    groups: dict[tuple, list[Closed]] = {}
    for row in closed:
        opened = row.opened_at if row.opened_at is not None else row.closed_at
        groups.setdefault((row.inst_id, row.direction, round(opened, 3)), []).append(row)
    return groups


def _on_price(rows: list[Closed]) -> float | None:
    values = [row.pnl if row.pnl is not None else row.realized for row in rows]
    values = [value for value in values if value is not None]
    return sum(values) if values else None


def day_figures(closed: list[Closed], positions, start: float, end: float) -> tuple[int, int, int, float, str]:
    """(new positions opened, losing trades in a row at the end, trades finished, realised P&L, its currency)
    for the trading day [start, end). Pure arithmetic on OKX's records and the positions held now."""
    opened: set[tuple] = set()
    finished: list[tuple[float, float]] = []  # (when it was closed in full, what it made on the price)
    for key, rows in trades(closed).items():
        if start <= key[2] < end:
            opened.add(key)
        last = [row for row in rows if row.close_type in FULL_CLOSE_TYPES and start <= row.closed_at < end]
        result = _on_price(rows)
        if last and result is not None:
            finished.append((max(row.closed_at for row in last), result))
    for position in positions:
        when = position.c_time / 1000 if position.c_time else None
        if when is not None and start <= when < end:
            opened.add((position.inst_id, position.direction, round(when, 3)))
    streak = 0
    for _, result in sorted(finished, reverse=True):
        if result >= 0:
            break
        streak += 1
    today = [row for row in closed if start <= row.closed_at < end and row.realized is not None]
    ccy = next((row.ccy for row in today if row.ccy), "")
    return len(opened), streak, len(finished), sum(row.realized for row in today), ccy


# ---------------------------------------------------------------------- the checks


def trend_of(candles) -> tuple[str, str]:
    """("up" | "down" | "range" | "", the figures in words) from closed candles: price over EMA20 over EMA50 is up."""
    closed = [candle for candle in candles if candle.confirmed] or list(candles)
    closes = [candle.close for candle in closed]
    fast, slow = ema(closes, 20), ema(closes, 50)
    if not closes or fast is None or slow is None:
        return "", "数据不足"
    close = closes[-1]
    figures = f"收盘 {_num(close)}，EMA20 {_num(fast)}，EMA50 {_num(slow)}"
    if close > fast > slow:
        return "up", figures
    if close < fast < slow:
        return "down", figures
    return "range", figures


TREND_WORDS = {"up": "偏多", "down": "偏空", "range": "震荡", "": "看不出"}


class Discipline:
    def __init__(self, cfg, journal, okx, clock: Callable[[], float] = time.time, breaker: Callable[[], str] | None = None):
        """`okx` reads the account and the close records (the same account orders go to). `breaker()` says why
        the circuit breaker holds new positions back, "" when it does not."""
        self._cfg = cfg
        self._journal = journal
        self._okx = okx
        self._clock = clock
        self.breaker = breaker
        self._lock = threading.Lock()
        self._status: tuple[float, DayStatus] | None = None

    # -------------------------------------------------------------- the plan

    def today(self) -> date:
        return trading_day(self._clock())

    def plan(self, day: date | None = None) -> DayPlan | None:
        day = day or self.today()
        rows = self._journal.plans_for(day.isoformat())
        if not rows:
            return None
        row = rows[-1]
        items = tuple(item for item in (_item(raw) for raw in row["items"]) if item is not None)
        return DayPlan(int(row["id"]), row["day"], float(row["ts"]), items, row["text"], int(row["revision"]))

    def revisions_left(self, day: date | None = None) -> int:
        rows = self._journal.plans_for((day or self.today()).isoformat())
        return max(0, PLAN_REVISIONS - (len(rows) - 1)) if rows else PLAN_REVISIONS

    def keep(self, day: date, items: tuple[InstrumentPlan, ...], text: str, reason: str = "") -> DayPlan:
        revision = len(self._journal.plans_for(day.isoformat()))
        self._journal.add_plan(day.isoformat(), [item.as_row() for item in items], text, revision, reason, ts=self._clock())
        log.info("plan for %s kept (revision %d): %s", day, revision, ", ".join(f"{i.inst_id} {i.bias}" for i in items))
        return self.plan(day)

    def rules_text(self, status: DayStatus | None = None, amounts: bool = True) -> str:
        """The day's limits in words, for the plan card and the AI that writes the plan."""
        cfg = self._cfg
        parts = []
        if cfg.plan_max_trades:
            parts.append(f"今天最多开 {cfg.plan_max_trades} 笔新仓（加仓不算）")
        stops = []
        if cfg.loss_streak_stop:
            stops.append(f"连亏 {cfg.loss_streak_stop} 笔")
        if cfg.plan_max_loss_pct:
            cap = ""
            if amounts and status is not None and status.loss_cap is not None:
                cap = f" {status.loss_cap:,.2f} {status.ccy or 'USDT'}，"
            stops.append(f"当天已实现亏损到{cap}开盘时权益的 {cfg.plan_max_loss_pct:g}%")
        if stops:
            parts.append("或".join(stops) + "，当天就停手")
        return "；".join(parts)

    def card(self, plan: DayPlan, amounts: bool) -> str:
        """The plan as the owners and the group see it: the program's lines first, then the AI's."""
        day = date.fromisoformat(plan.day)
        _, end = day_bounds(day)
        lines = []
        for item in plan.items:
            levels = levels_text(item)
            lines.append(f"- **{item.base}：{BIASES[item.bias]}**" + (f"，{levels}" if levels else ""))
        status = None
        try:
            status = self.status()
        except (OkxError, OkxNetworkError) as exc:
            log.info("no day status for the plan card: %s", exc)
        rules = self.rules_text(status, amounts)
        if rules:
            lines.append(f"- {rules}")
        if self._cfg.plan_required:
            lines.append("- 和计划方向不一样的开仓、超过笔数的开仓，程序直接拦下；平仓、撤单、改止损不受影响")
        lines.append(f"- 有效到 {both(end, self._cfg.tz, '%m-%d %H:%M')}")
        head = "\n".join(lines)
        return f"{head}\n\n{plan.text}" if plan.text else head

    def title(self, plan: DayPlan) -> str:
        again = f"（第 {plan.revision + 1} 版）" if plan.revision else ""
        return f"交易计划 · {day_label(date.fromisoformat(plan.day))}{again}"

    # ----------------------------------------------------------- how the day goes

    def status(self, fresh: bool = False) -> DayStatus:
        """The day so far, from OKX's own records. Raises OkxError / OkxNetworkError when they cannot be read."""
        now = self._clock()
        with self._lock:
            known = self._status
        if known is not None and not fresh and now - known[0] < STATUS_TTL_SECONDS and known[1].day == trading_day(now):
            return known[1]
        day = trading_day(now)
        start, end = day_bounds(day)
        state = self._okx.account_state()
        opened, streak, finished, realized, ccy = day_figures(read_closed(self._okx), state.positions, start, end)
        ccy = ccy or (state.positions[0].pnl_ccy if state.positions else "USDT")
        start_equity = self._journal.first_equity_since(start)
        if start_equity is None and state.snapshot.total_eq is not None:
            start_equity = state.snapshot.total_eq - realized
        cfg = self._cfg
        cap = start_equity * cfg.plan_max_loss_pct / 100 if cfg.plan_max_loss_pct and start_equity else None
        stopped = ""
        if cfg.loss_streak_stop and streak >= cfg.loss_streak_stop:
            stopped = f"今天已经连亏 {streak} 笔，到了你定的 {cfg.loss_streak_stop} 笔"
        elif cap is not None and realized <= -cap:
            stopped = f"今天已实现亏损 {abs(realized):,.2f} {ccy}，到了开盘时权益的 {cfg.plan_max_loss_pct:g}%（{cap:,.2f}）"
        status = DayStatus(day, opened, streak, finished, realized, ccy, start_equity, cap, stopped)
        with self._lock:
            self._status = (now, status)
        return status

    def next_day_text(self) -> str:
        _, end = day_bounds(self.today())
        return both(end, self._cfg.tz, "%m-%d %H:%M")

    def refusal(self, inst_id: str, direction: str, adding: bool = False) -> str:
        """Why a new position (or an addition) may not be opened now; "" when it may.
        Reading OKX fails closed: no figures, no new position."""
        cfg = self._cfg
        base = inst_id.split("-")[0]
        word = "多" if direction == "long" else "空"
        tail = "平仓、撤单、改止损不受影响。"
        try:
            status = self.status(fresh=True)
        except (OkxError, OkxNetworkError) as exc:
            log.warning("discipline: OKX's records could not be read (%s)", type(exc).__name__)
            return f"读不到 {cfg.broker_name} 的平仓记录，今天开了几笔、连亏几笔没法核对，这次不开仓。请稍后再试。" + tail
        if status.stopped:
            return f"今天停手了：{status.stopped}。新的交易日 {self.next_day_text()}开始，到时再开仓。" + tail
        if cfg.plan_required:
            plan = self.plan(status.day)
            if plan is None:
                return f"今天（交易日 {day_label(status.day)}）还没有交易计划，不开新仓。{NO_PLAN_WORDS}。" + tail
            item = plan.item(inst_id)
            if item is None:
                return f"今天的交易计划里没有 {base}，不开它的新仓。" + tail
            if item.bias == "none":
                return f"今天的交易计划是观望（{base}），不开新仓。" + tail
            if not item.allows(direction):
                return f"今天的交易计划是{BIASES[item.bias]}（{base}），这张是做{word}，和计划相反，没有执行。" + tail
        if not adding and cfg.plan_max_trades and status.opened >= cfg.plan_max_trades:
            return f"今天已经开了 {status.opened} 笔新仓，到了每天 {cfg.plan_max_trades} 笔的上限。" + tail
        return ""

    def status_lines(self, amounts: bool = True) -> list[str]:
        """For the trading settings card and the AI: the plan in force and how the day stands."""
        cfg = self._cfg
        lines = []
        day = self.today()
        plan = self.plan(day)
        if plan is None:
            lines.append(f"交易计划：今天（{day_label(day)}）还没有" + ("，不能开新仓" if cfg.plan_required else ""))
        else:
            parts = [f"{item.base} {BIASES[item.bias]}" for item in plan.items]
            lines.append(f"交易计划（{day_label(day)}）：{'；'.join(parts)}")
        try:
            status = self.status()
        except (OkxError, OkxNetworkError):
            lines.append(f"今天开了几笔、连亏几笔：这次没读到 {cfg.broker_name}")
            return lines
        count = f"今天开了 {status.opened} 笔新仓" + (f"（上限 {cfg.plan_max_trades}）" if cfg.plan_max_trades else "")
        count += f"，平掉 {status.finished} 笔，最近连亏 {status.streak} 笔"
        if amounts and status.finished:
            count += f"，已实现 {status.realized:+,.2f} {status.ccy}"
        lines.append(count)
        if status.stopped:
            lines.append(f"今天停手：{status.stopped}，到 {self.next_day_text()}恢复")
        return lines

    def notes_for_ai(self, with_status: bool = True) -> str:
        """What the AI is told before every answer: today's plan and whether a new position may be opened.
        `with_status`: how the day stands (trades, losses), only where the account may be talked about."""
        day = self.today()
        plan = self.plan(day)
        if plan is None and self._cfg.plan_required:
            head = (
                f"今天（交易日 {day_label(day)}）还没有交易计划：不要给任何进场价、止损和目标位，"
                "告诉他先发“出今天的计划”；行情本身照常分析。"
            )
        elif plan is None:
            head = f"今天（交易日 {day_label(day)}）还没有交易计划。"
        else:
            parts = []
            for item in plan.items:
                levels = levels_text(item)
                parts.append(f"{item.inst_id} {BIASES[item.bias]}" + (f"（{levels}）" if levels else ""))
            head = f"今天的交易计划（交易日 {day_label(day)}，程序存的）：{'；'.join(parts)}。"
        if not with_status:
            return head
        try:
            status = self.status()
        except (OkxError, OkxNetworkError):
            return head
        if status.stopped:
            return head + f"今天已经停手（{status.stopped}），不要给任何进场的想法。"
        left = ""
        if self._cfg.plan_max_trades:
            left = f"今天还能开 {max(0, self._cfg.plan_max_trades - status.opened)} 笔新仓，"
        return head + f"{left}最近连亏 {status.streak} 笔。"

    def stop_alert(self) -> tuple[str, str]:
        """After a close: the notice that the day has just stopped, (with amounts, without); ("", "") when there
        is nothing new to say."""
        try:
            status = self.status(fresh=True)
        except (OkxError, OkxNetworkError):
            return "", ""
        if not status.stopped:
            return "", ""
        day = status.day.isoformat()
        if self._journal.get_setting(STOP_TOLD_SETTING, "") == day:
            return "", ""
        self._journal.set_setting(STOP_TOLD_SETTING, day)
        log.info("discipline: the day stopped")
        rule = f"按规矩今天停手：到 {self.next_day_text()}新的交易日开始之前，不接受开仓和加仓。"
        rest = "\n平仓、撤单、改止损照常可用。已经挂着的开仓单还在，不想让它成交就发“撤单 XAU”。"
        cfg = self._cfg
        public = f"今天已经连亏 {status.streak} 笔" if cfg.loss_streak_stop and status.streak >= cfg.loss_streak_stop else "今天的亏损到了上限"
        return f"{status.stopped}。{rule}{rest}", f"{public}。{rule}{rest}"

    # ------------------------------------------------------------- the checklist

    def check_setup(self, inst_id: str, side: str, entry: float, stop: float, target: float, amounts: bool = True) -> str:
        """The checklist for one trade idea, item by item, with the verdict. For the AI's check_setup tool."""
        now = self._clock()
        long = side == "long"
        word = "做多" if long else "做空"
        results: list[tuple[bool, str, str]] = []  # (passed, name, words)

        plan = self.plan()
        item = plan.item(inst_id) if plan else None
        if plan is None:
            results.append((not self._cfg.plan_required, "今天的计划", "今天还没有交易计划"))
        elif item is None:
            results.append((False, "今天的计划", f"今天的计划里没有 {inst_id}"))
        else:
            ok = item.allows(side)
            results.append((ok, "今天的计划", f"{BIASES[item.bias]}，这个想法是{word}，{'一致' if ok else '不一致'}"))

        day_candles = self._okx.candles(inst_id, "1D", 120)
        four, four_words = trend_of(self._okx.candles(inst_id, "4H", 120))
        daily, daily_words = trend_of(day_candles)
        want, against = ("up", "down") if long else ("down", "up")
        ok = four == want and daily != against
        results.append((
            ok, "大周期",
            f"4 小时{TREND_WORDS[four]}（{four_words}），日线{TREND_WORDS[daily]}（{daily_words}）；"
            f"要 4 小时{TREND_WORDS[want]}、日线不{TREND_WORDS[against]}",
        ))  # fmt: skip

        closed_days = [candle for candle in day_candles if candle.confirmed]
        day_atr = atr(closed_days, 14)
        levels = list(item.support + item.resistance) if item else []
        if closed_days:
            levels += [closed_days[-1].high, closed_days[-1].low]
        tolerance = KEY_LEVEL_ATR * day_atr if day_atr else entry * 0.003
        if levels:
            nearest = min(levels, key=lambda level: abs(level - entry))
            gap = abs(nearest - entry)
            ok = gap <= tolerance
            yardstick = f"日线 ATR {_num(day_atr)} 的 {KEY_LEVEL_ATR:g} 倍是 {_num(tolerance)}" if day_atr else f"容差 {_num(tolerance)}"
            results.append((ok, "关键位", f"入场 {_num(entry)} 离最近的关键位 {_num(nearest)} 差 {_num(gap)}（{yardstick}）"))
        else:
            results.append((False, "关键位", "没有可对照的关键位"))

        risk = (entry - stop) if long else (stop - entry)
        reward = (target - entry) if long else (entry - target)
        if risk <= 0 or reward <= 0:
            results.append((False, "盈亏比", f"{word}时止损要在入场{'下' if long else '上'}方、目标在{'上' if long else '下'}方"))
        else:
            ratio = reward / risk
            results.append((
                ratio >= MIN_REWARD_RISK, "盈亏比",
                f"{ratio:.1f}:1（风险 {_num(risk)}，空间 {_num(reward)}），要至少 {MIN_REWARD_RISK:g}:1",
            ))  # fmt: skip

        near = [r for r in econ.releases() if now - NEWS_AFTER_SECONDS <= r.at <= now + NEWS_AHEAD_SECONDS]
        if near:
            release = near[0]
            results.append((False, "重要数据", f"{release.name} {both(release.at, self._cfg.tz, '%H:%M')} 公布，在前后半小时里"))
        else:
            results.append((True, "重要数据", "前后半小时没有美国 CPI、非农、议息"))

        try:
            status = self.status()
            breaker = self.breaker() if self.breaker else ""
        except (OkxError, OkxNetworkError):
            results.append((False, "今天的状态", f"读不到 {self._cfg.broker_name} 的记录，没法核对"))
        else:
            if status.stopped or breaker:
                said = status.stopped if amounts else "今天已经停手（连亏或亏损到了上限）"
                results.append((False, "今天的状态", (said if status.stopped else "") or breaker))
            elif self._cfg.plan_max_trades and status.opened >= self._cfg.plan_max_trades:
                results.append((False, "今天的状态", f"今天已经开了 {status.opened} 笔，到了上限"))
            else:
                left = f"还能开 {self._cfg.plan_max_trades - status.opened} 笔，" if self._cfg.plan_max_trades else ""
                results.append((True, "今天的状态", f"{left}最近连亏 {status.streak} 笔"))

        head = f"清单（{inst_id} {word}，入场 {_num(entry)}，止损 {_num(stop)}，目标 {_num(target)}）："
        lines = [head, *(f"{'✓' if ok else '✗'} {name}：{words}" for ok, name, words in results)]
        missing = [name for ok, name, _ in results if not ok]
        if missing:
            lines.append(
                f"结论：不达标，差 {'、'.join(missing)}。按规矩回答“观望”，一句话说清楚差哪一条；"
                "不要写这个想法的进场价、止损和目标位。"
            )
        else:
            lines.append("结论：全部达标，可以把这个想法写出来。")
        return "\n".join(lines)
