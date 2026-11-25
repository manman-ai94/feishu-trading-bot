"""US data releases that move gold, and a word to the owner half an hour before each.

CPI, the jobs report (non-farm payrolls) and the Federal Reserve's rate
decision can move gold several ATRs in minutes, and a stop can fill well past
its price. Half an hour before one, when there is a position or an order
waiting, the owner gets one card: when it comes out, each position with how
far its stop is in hours of normal movement (1-hour ATR), and the orders
waiting. It only tells; it never touches an order.

The dates are the official schedules, in US Eastern time:
  CPI       https://www.bls.gov/schedule/news_release/cpi.htm        08:30
  Jobs      https://www.bls.gov/schedule/news_release/empsit.htm     08:30
  FOMC      https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm  14:00 on the second day
A month before the last date the bot puts "update the calendar" into the
requests list, which the developer reads every day.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Callable
from zoneinfo import ZoneInfo

from .indicators import atr
from .models import AccountState, OrderInfo, Position, size_text

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
LEAD_SECONDS = 30 * 60
CHECK_EVERY_SECONDS = 60
TOLD_SETTING = "econ_told_{key}"
STALE_SETTING = "econ_calendar_stale"
STALE_DAYS = 30  # this long before the last date, the calendar is asked to be updated
MAX_ORDERS_SHOWN = 6

CALENDAR: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "cpi", "美国 CPI（通胀）", "08:30",
        ("2026-12-10", "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13", "2027-05-12", "2027-06-10",
         "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10"),
    ),
    (
        "nfp", "美国非农就业", "08:30",
        ("2026-12-04", "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02", "2027-05-07", "2027-06-04",
         "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08", "2027-11-05", "2027-12-03"),
    ),
    (
        "fomc", "美联储利率决议", "14:00",
        ("2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15",
         "2027-10-27", "2027-11-17"),
    ),
)  # fmt: skip


@dataclass(frozen=True)
class Release:
    key: str  # "cpi-2026-12-10": one per release, for remembering it was told
    name: str
    at: float  # when it comes out, epoch seconds

    def et(self) -> str:
        return datetime.fromtimestamp(self.at, tz=ET).strftime("%H:%M")


def releases() -> list[Release]:
    found = []
    for kind, name, clock, days in CALENDAR:
        hour, minute = (int(part) for part in clock.split(":"))
        for day in days:
            when = datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=ET)
            found.append(Release(f"{kind}-{day}", name, when.timestamp()))
    return sorted(found, key=lambda release: release.at)


def due(now: float, lead: float = LEAD_SECONDS) -> list[Release]:
    """The releases coming out within `lead` seconds from now."""
    return [release for release in releases() if 0 < release.at - now <= lead]


def last_day() -> date:
    return max(date.fromisoformat(day) for _, _, _, days in CALENDAR for day in days)


def _num(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:,.2f}".rstrip("0").rstrip(".") if abs(value) < 100_000 else f"{value:,.0f}"


def _side(direction: str) -> str:
    return "多" if direction == "long" else "空"


def position_line(position: Position, hour_atr: float | None) -> str:
    size = size_text(position)
    head = f"{position.inst_id} {_side(position.direction)}单 {size}，标记价 {_num(position.mark_px)}"
    stops = [s.sl_trigger_px for s in position.stops if s.sl_trigger_px is not None]
    if not stops:
        return f"{head}，**没有止损**"
    long = position.direction == "long"
    stop = max(stops) if long else min(stops)  # the one that would fire first
    if position.mark_px is None:
        return f"{head}，止损 {_num(stop)}"
    gap = position.mark_px - stop if long else stop - position.mark_px
    far = f"，约 {gap / hour_atr:.1f} 个 1 小时 ATR" if hour_atr and gap > 0 else ""
    return f"{head}，止损 {_num(stop)}（离标记价 {_num(gap)}{far}）"


def order_line(order: OrderInfo) -> str:
    side = "买" if order.side == "buy" else "卖"
    left = order.sz - order.filled_sz
    kind = "只减仓" if order.reduce_only else ("限价" if order.px is not None else order.ord_type or "")
    return f"{order.inst_id} {kind}{side} {_num(order.px)}（{left:,.10g} 张）"


def release_text(release: Release, now: float, tz: tzinfo, state: AccountState, orders: list[OrderInfo], atrs: dict[str, float | None]) -> str:
    local = datetime.fromtimestamp(release.at, tz=tz).strftime("%m-%d %H:%M")
    minutes = max(1, round((release.at - now) / 60))
    lines = [f"{release.name}在 {local} 公布（美东 {release.et()}），还有约 {minutes} 分钟。公布前后几分钟金价常大幅波动，止损可能滑点成交。"]
    if state.positions:
        lines.append("")
        lines.append("**持仓**")
        lines.extend(f"- {position_line(p, atrs.get(p.inst_id))}" for p in state.positions)
    if orders:
        lines.append("")
        lines.append("**等成交的挂单**")
        lines.extend(f"- {order_line(o)}" for o in orders[:MAX_ORDERS_SHOWN])
        if len(orders) > MAX_ORDERS_SHOWN:
            lines.append(f"- 还有 {len(orders) - MAX_ORDERS_SHOWN} 张，发“挂单”看全部")
    lines.append("")
    lines.append("只是提醒，我没有动任何单。")
    return "\n".join(lines)


class ReleaseWatcher:
    """Half an hour before a release, one card to the owner (and the group when it shares the account),
    when there is something on the account it could move."""

    def __init__(
        self,
        journal,
        okx,
        state: Callable[[], AccountState | None],
        send: Callable[[str, str], None],
        tz: tzinfo,
        clock: Callable[[], float] = time.time,
        note_wish: Callable[[str], None] | None = None,
    ):
        self._journal = journal
        self._okx = okx
        self._state = state
        self._send = send
        self._tz = tz
        self._clock = clock
        self._note_wish = note_wish

    def check_once(self) -> int:
        """Sends what is due; returns how many cards went out."""
        now = self._clock()
        self._calendar_check(now)
        sent = 0
        for release in due(now):
            setting = TOLD_SETTING.format(key=release.key)
            if self._journal.get_setting(setting, ""):
                continue
            state = self._state()
            if state is None:
                continue  # the account is not read yet: try again next minute
            orders = self._orders()
            if not state.positions and not orders:
                continue  # nothing it could move; looked at again until it comes out, in case he opens one
            atrs = {p.inst_id: self._hour_atr(p.inst_id) for p in state.positions}
            title = f"数据公布前提醒：{release.name}"
            self._send(title, release_text(release, now, self._tz, state, orders, atrs))
            self._journal.set_setting(setting, str(int(now)))
            log.info("release %s: the owner was told", release.key)
            sent += 1
        return sent

    def _orders(self) -> list[OrderInfo]:
        try:
            return [o for o in self._okx.pending_orders() if o.state in ("live", "partially_filled", "")]
        except Exception as exc:
            log.info("release alert: open orders not read (%s)", type(exc).__name__)
            return []

    def _hour_atr(self, inst_id: str) -> float | None:
        try:
            return atr(self._okx.candles(inst_id, "1H", 50))
        except Exception as exc:
            log.info("release alert: no candles for %s (%s)", inst_id, type(exc).__name__)
            return None

    def _calendar_check(self, now: float) -> None:
        last = last_day()
        if datetime.fromtimestamp(now, tz=ET).date() < last - timedelta(days=STALE_DAYS):
            return
        if self._journal.get_setting(STALE_SETTING, "") == last.isoformat():
            return
        self._journal.set_setting(STALE_SETTING, last.isoformat())
        log.warning("the data release calendar ends on %s: it needs the next year's dates", last)
        if self._note_wish is not None:
            self._note_wish(f"数据公布日历只排到 {last.isoformat()}，要补上之后的 CPI、非农和议息日期")

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.check_once()
            except Exception:
                log.exception("release alerts failed")
            stop.wait(CHECK_EVERY_SECONDS)
