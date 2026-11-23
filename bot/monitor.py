"""Watches the account: turns changes between two polls into events, and
checks the risk thresholds. No AI involved, only rules."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from .closes import FULL_CLOSE_TYPES, close_lines, how_it_closed, read_closed, read_triggers
from .config import Config
from .journal import Journal
from .models import AccountState, Position
from .okx_client import OkxClient, OkxError, OkxNetworkError
from .risk import LEVEL_WORDS, AccountRisk, PositionRisk, liq_line, readings, stop_line

log = logging.getLogger(__name__)

QTY_TOLERANCE = 1e-9
FAILURES_BEFORE_ALERT = 3
NO_STOP_REPEAT_SECONDS = 24 * 3600  # a position left without a stop is pointed out again this often
# A close record counts for the position just gone only if OKX wrote it after the last read that still
# saw the position, less this much for the two clocks disagreeing.
CLOSE_RECORD_MARGIN_SECONDS = 10
CLOSE_RECORD_WAIT_SECONDS = 2.0  # OKX may write the record a moment after the position goes
NO_STOP_SETTING = "no_stop_alert:"  # in the journal: when each position was last pointed out, so a restart does not repeat it
RISK_LEVELS_SETTING = "risk_levels"  # in the journal: the level each risk reading was last reported at
NEAR_SETTING = "near_alerts"  # in the journal: the stops and take-profits the price was last reported close to
NEAR_ATR = 0.25  # "close to the stop / take-profit": within this share of a normal day's range
NEAR_RELEASE_ATR = 0.5  # told again only after the price has been this far away in between


@dataclass(frozen=True)
class Event:
    kind: str
    title: str
    inst_id: str = ""
    # Shown to the owner in the private chat (may contain amounts).
    private_lines: tuple[str, ...] = ()
    # Shown in the group: instrument, direction and price levels only.
    public_lines: tuple[str, ...] = ()
    to_group: bool = False
    warning: bool = False


def _px(value: float | None) -> str:
    return "未知" if value is None else f"{value:,.2f}"


def _qty(value: float) -> str:
    return f"{value:g}"


def _side(position: Position) -> str:
    return "多" if position.direction == "long" else "空"


def _money(value: float | None, ccy: str = "USD") -> str:
    return "未知" if value is None else f"{value:+,.2f} {ccy}"


def _size(position: Position) -> str:
    text = f"{_qty(position.qty)} 张"
    if position.coins is not None and position.coin_ccy:
        text += f"（合 {position.coins:,.8g} {position.coin_ccy}）"
    return text


def diff_positions(previous: dict[tuple, Position], current: dict[tuple, Position]) -> list[Event]:
    """Compare two polls and describe what changed."""
    events: list[Event] = []

    for key, position in current.items():
        before = previous.get(key)
        name = f"{position.inst_id} {_side(position)}单"
        if before is None:
            events.append(
                Event(
                    kind="opened",
                    title=f"开仓：{name}",
                    inst_id=position.inst_id,
                    private_lines=(
                        f"数量 {_size(position)}，均价 {_px(position.avg_px)}",
                        _stops_line(position),
                    ),
                    public_lines=(f"均价 {_px(position.avg_px)}", _stops_line(position)),
                    to_group=True,
                )
            )
            continue

        change = position.qty - before.qty
        if abs(change) > QTY_TOLERANCE:
            increased = change > 0
            events.append(
                Event(
                    kind="increased" if increased else "reduced",
                    title=f"{'加仓' if increased else '减仓'}：{name}",
                    inst_id=position.inst_id,
                    private_lines=(
                        f"{_qty(before.qty)} 张变为 {_size(position)}",
                        f"均价 {_px(before.avg_px)} 变为 {_px(position.avg_px)}",
                    ),
                    public_lines=(f"现均价 {_px(position.avg_px)}，标记价 {_px(position.mark_px)}",),
                    to_group=True,
                )
            )

        if (before.stop_loss, before.take_profit) != (position.stop_loss, position.take_profit):
            lines = []
            if before.stop_loss != position.stop_loss:
                lines.append(f"止损 {_px_or_none(before.stop_loss)} 变为 {_px_or_none(position.stop_loss)}")
            if before.take_profit != position.take_profit:
                lines.append(f"止盈 {_px_or_none(before.take_profit)} 变为 {_px_or_none(position.take_profit)}")
            events.append(
                Event(
                    kind="stop_changed",
                    title=f"止盈止损变动：{name}",
                    inst_id=position.inst_id,
                    private_lines=tuple(lines),
                    public_lines=tuple(lines),
                    to_group=True,
                )
            )

    for key, before in previous.items():
        if key in current:
            continue
        name = f"{before.inst_id} {_side(before)}单"
        events.append(
            Event(
                kind="closed",
                title=f"平仓：{name}",
                inst_id=before.inst_id,
                private_lines=(
                    f"原数量 {_size(before)}，开仓均价 {_px(before.avg_px)}",
                    f"平仓前标记价 {_px(before.mark_px)}，平仓前浮动盈亏 {_money(before.upl, before.pnl_ccy)}",
                ),
                public_lines=(f"开仓均价 {_px(before.avg_px)}，平仓前标记价 {_px(before.mark_px)}",),
                to_group=True,
            )
        )
    return events


def _px_or_none(value: float | None) -> str:
    return "无" if value is None else f"{value:,.2f}"


def _stops_line(position: Position) -> str:
    return f"止损 {_px_or_none(position.stop_loss)}，止盈 {_px_or_none(position.take_profit)}"


def position_brief(position: Position) -> str:
    """The position in one line, to open an alert with: what it is and how it stands right now."""
    size = f"{position.coins:,.8g} {position.coin_ccy}" if position.coins is not None and position.coin_ccy else f"{_qty(position.qty)} 张"
    return (
        f"{position.inst_id} {_side(position)}单 {size}，均价 {_px(position.avg_px)}，标记价 {_px(position.mark_px)}，"
        f"浮动盈亏 {_money(position.upl, position.pnl_ccy)}，{_stops_line(position)}"
    )


def pnl_at(position: Position, price: float) -> float | None:
    """What the whole position makes or loses against its average price if it closes at `price`."""
    if position.coins is None or not position.avg_px:
        return None
    move = price - position.avg_px if position.direction == "long" else position.avg_px - price
    return move * abs(position.coins)


def check_risk(state: AccountState, cfg: Config, day_start_equity: float | None) -> list[Event]:
    """Rule-based risk warnings. They only ever go to the owner."""
    events: list[Event] = []
    snapshot = state.snapshot

    threshold = cfg.alert_margin_ratio
    if threshold is not None:
        if snapshot.mgn_ratio is not None and snapshot.mgn_ratio <= threshold:
            events.append(
                Event(
                    kind="margin_warning",
                    title="保证金率偏低",
                    private_lines=(
                        f"账户保证金率 {snapshot.mgn_ratio * 100:.0f}%，你设的提醒线是 {threshold * 100:.0f}%",
                        "保证金率到 100% 会开始强平。",
                    ),
                    warning=True,
                )
            )
        for position in state.positions:
            if position.mgn_mode == "isolated" and position.mgn_ratio is not None and position.mgn_ratio <= threshold:
                events.append(
                    Event(
                        kind="margin_warning",
                        title=f"逐仓保证金率偏低：{position.inst_id} {_side(position)}单",
                        inst_id=position.inst_id,
                        private_lines=(f"保证金率 {position.mgn_ratio * 100:.0f}%，提醒线 {threshold * 100:.0f}%",),
                        warning=True,
                    )
                )

    if cfg.alert_liq_distance_pct is not None:
        for position in state.positions:
            distance = position.liq_distance_pct
            if distance is not None and distance <= cfg.alert_liq_distance_pct:
                events.append(
                    Event(
                        kind="liq_warning",
                        title=f"离强平价很近：{position.inst_id} {_side(position)}单",
                        inst_id=position.inst_id,
                        private_lines=(
                            f"标记价 {_px(position.mark_px)}，预估强平价 {_px(position.liq_px)}，相距 {distance:.2f}%",
                            f"你设的提醒线是 {cfg.alert_liq_distance_pct:g}%",
                        ),
                        warning=True,
                    )
                )

    if cfg.alert_daily_loss_usd is not None and day_start_equity is not None and snapshot.total_eq is not None:
        change = snapshot.total_eq - day_start_equity
        if change <= -abs(cfg.alert_daily_loss_usd):
            events.append(
                Event(
                    kind="daily_loss",
                    title="今日亏损到达提醒线",
                    private_lines=(
                        f"今日权益变化 {_money(change)}，你设的提醒线是 -{abs(cfg.alert_daily_loss_usd):,.2f} USD",
                        "这个数字包含出入金，仅供提醒。",
                    ),
                    warning=True,
                )
            )
    return events


class Monitor:
    def __init__(
        self,
        okx: OkxClient,
        cfg: Config,
        journal: Journal,
        notify: Callable[[Event], None],
        clock: Callable[[], float] = time.time,
        gauge=None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._okx = okx
        self._sleep = sleep
        self._cfg = cfg
        self._journal = journal
        self._notify = notify
        self._clock = clock
        self._gauge = gauge  # risk.RiskGauge, or None: then positions are not measured against the market's range
        self._risk_levels: dict[str, int] | None = None  # read from the journal when first needed
        self._near: set[str] | None = None  # likewise: see _near_events
        self._lock = threading.Lock()
        self._state: AccountState | None = None
        self._positions: dict[tuple, Position] | None = None
        self._read_at: float | None = None  # when self._positions was read
        self._last_snapshot_at = 0.0
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._wake = threading.Event()  # set by the OKX stream: read the account now, not at the next tick
        self.cycles = 0  # rounds of run_forever: the health check reads it to tell a stuck monitor
        self._bare_since: dict[tuple, float] = {}  # positions without a stop-loss, and since when
        self._failures = 0
        self._down_reported = False
        self.last_error: str = ""

    @property
    def state(self) -> AccountState | None:
        """The most recent successful read of the account."""
        with self._lock:
            return self._state

    def day_start_equity(self) -> float | None:
        now = datetime.fromtimestamp(self._clock(), tz=self._cfg.tz)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self._journal.first_equity_since(midnight.timestamp())

    def poll_once(self) -> AccountState | None:
        """Read the account once, emit events for anything that changed."""
        now = self._clock()
        try:
            state = self._okx.account_state()
        except (OkxError, OkxNetworkError) as exc:
            self._on_failure(exc)
            return None
        self._on_success()

        if not state.stops_known and self._positions:
            # The stop orders could not be read this time. That is not "the stops are gone":
            # each position keeps the stops it was last seen with.
            state = replace(
                state,
                positions=[
                    replace(position, stops=self._positions[position.key].stops) if position.key in self._positions else position
                    for position in state.positions
                ],
            )
        current = {position.key: position for position in state.positions}
        events: list[Event] = []
        if self._positions is None:
            log.info("baseline: %d open position(s)", len(current))
        else:
            events.extend(self._as_recorded(event) for event in diff_positions(self._positions, current))
        self._positions = current
        self._read_at = now
        with self._lock:
            self._state = state

        if now - self._last_snapshot_at >= self._cfg.snapshot_minutes * 60 or events:
            self._journal.record_snapshot(state, now)
            self._last_snapshot_at = now

        for event in check_risk(state, self._cfg, self.day_start_equity()):
            if self._cooled_down(event, now):
                events.append(event)
        risk = self._measure(state)
        events = [self._with_stop_cost(event, risk) for event in events]
        events.extend(self._without_stops(state, now, risk))
        events.extend(self._risk_events(risk, now))
        events.extend(self._near_events(risk, state))

        for event in events:
            self._journal.record_event(event.kind, event.title + "；" + "；".join(event.private_lines), event.inst_id, ts=now)
            self._safe_notify(event)
        return state

    def poke(self, reason: str = "") -> None:
        """Read the account as soon as possible: OKX pushed a change (see okx_stream.py)."""
        log.info("monitor woken by the OKX stream (%s)", reason or "push")
        self._wake.set()

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.cycles += 1
            try:
                self.poll_once()
            except Exception:  # keep the loop alive whatever happens
                log.exception("monitor poll crashed")
            self._wake.wait(self._cfg.poll_seconds)
            self._wake.clear()

    # ------------------------------------------------------------- internals

    def _as_recorded(self, event: Event) -> Event:
        """A close as OKX recorded it: the realised P&L with fees and funding, and whether a stop, a
        take-profit or a liquidation did it. Keeps the last poll's numbers when there is no record."""
        if event.kind != "closed":
            return event
        try:
            record = self._close_record(event.inst_id)
            if record is None:
                return event
            triggers = read_triggers(self._okx)
        except Exception as exc:  # the alert goes out either way
            log.info("close of %s: OKX's record could not be read (%s)", event.inst_id, type(exc).__name__)
            return event
        lines = close_lines(record, triggers)
        size = tuple(line.split("，开仓均价")[0] for line in event.private_lines[:1])  # "原数量 ..." from the last poll
        how = how_it_closed(record, triggers)
        title = event.title
        for start, label in (("止损", "止损触发"), ("止盈", "止盈触发"), ("强平", "强平")):
            if how.startswith(start):
                title = event.title.replace("平仓：", f"{label}：", 1)
        return replace(
            event,
            title=title,
            private_lines=(*lines, *size),
            public_lines=tuple(line for line in lines if not line.startswith("已实现盈亏")),
            warning=event.warning or record.close_type in ("3", "4"),
        )

    def _close_record(self, inst_id: str):
        """OKX's record of the close just seen: a full close written after the last read that still saw
        the position. An older record, such as a partial close earlier on, is not this close."""
        since = (self._read_at or 0.0) - CLOSE_RECORD_MARGIN_SECONDS
        for attempt in range(2):
            history = read_closed(self._okx)
            recent = [
                c for c in history if c.inst_id == inst_id and c.close_type in FULL_CLOSE_TYPES and c.closed_at >= since
            ]
            if recent:
                return recent[-1]
            if not history or attempt:
                return None  # no records at all: nothing to wait for
            self._sleep(CLOSE_RECORD_WAIT_SECONDS)
        return None

    def _measure(self, state: AccountState) -> AccountRisk | None:
        if self._gauge is None:
            return None
        try:
            return self._gauge.assess(state)
        except Exception:  # a measurement must never stop the watching of positions
            log.exception("could not measure the positions' risk")
            return None

    def _risk_events(self, risk: AccountRisk | None, now: float) -> list[Event]:
        """Readings that crossed one of the owner's thresholds since they were last reported.

        One card with everything that stands out, sent when something gets worse.
        A reading that stays where it is, or gets better, is not repeated here:
        it is on every status card and in every brief.
        """
        if risk is None:
            return []
        if self._risk_levels is None:
            try:
                stored = json.loads(self._journal.get_setting(RISK_LEVELS_SETTING) or "{}")
                self._risk_levels = {str(key): int(value) for key, value in stored.items()}
            except (ValueError, TypeError, AttributeError):
                self._risk_levels = {}
        known = self._risk_levels
        before = dict(known)
        found = readings(risk, self._cfg)
        held = {reading.key: reading.held for reading in found}
        for key in list(known):
            if held.get(key, 0) < known[key]:  # well clear of the line, closed, or no longer measurable
                known[key] = held.get(key, 0)
        raised = [reading for reading in found if reading.level > known.get(reading.key, 0)]
        events: list[Event] = []
        if raised:
            standing = [reading for reading in found if reading.level > 0]
            worst = max(reading.level for reading in standing)
            about = {inst_id for reading in standing for inst_id in reading.inst_ids}
            if any(not reading.inst_ids for reading in standing):  # an account-wide reading: every position
                about = {measured.position.inst_id for measured in risk.positions}
            briefs = [position_brief(measured.position) for measured in risk.positions if measured.position.inst_id in about]
            subjects = "，".join(dict.fromkeys(reading.subject for reading in standing if reading.subject))
            event = Event(
                kind="risk_level",
                title=f"仓位风险{LEVEL_WORDS[worst]}" + (f"：{subjects}" if subjects else ""),
                private_lines=tuple(
                    line
                    for line in (
                        *briefs,
                        *(reading.line for reading in standing),
                        self._how_to_act(sorted(about)),
                    )
                    if line
                ),
                warning=True,
            )
            if self._cooled_down(event, now):
                for reading in raised:
                    known[reading.key] = reading.level
                events.append(event)
        for key in [key for key, level in known.items() if level == 0]:
            del known[key]
        if known != before:
            self._journal.set_setting(RISK_LEVELS_SETTING, json.dumps(known, ensure_ascii=False))
        return events

    def _with_stop_cost(self, event: Event, risk: AccountRisk | None) -> Event:
        """A stop placed or moved, or a position opened or added to: what the stop would cost from here,
        and whether that is more than the owner's rule. Told once, with the change, rather than every
        time the price drifts."""
        if event.kind not in ("opened", "increased", "stop_changed") or risk is None:
            return event
        measured = next((item for item in risk.positions if item.position.inst_id == event.inst_id), None)
        if measured is None or measured.position.stop_loss is None:
            return event
        line = stop_line(measured)
        if not line:
            return event
        rule = self._cfg.alert_stop_risk_pct
        if rule is not None and measured.stop_cost_pct is not None and measured.stop_cost_pct > rule:
            line += f"。超过了你定的单笔风险：权益的 {rule:g}%"
        loss = pnl_at(measured.position, measured.position.stop_loss)
        if loss is not None:
            line += f"。按开仓均价算，这一单在止损处{'赚' if loss >= 0 else '亏'}约 {abs(loss):,.2f} {measured.position.pnl_ccy}"
        return replace(event, private_lines=(*event.private_lines, line))

    def _near_events(self, risk: AccountRisk | None, state: AccountState) -> list[Event]:
        """The price has come close to a stop-loss or a take-profit: what is happening to the position.
        Each is told once; again only after the price has moved well away in between, or the order moved."""
        if risk is None:
            return []
        known: set[str] = self._near_known()
        before = set(known)
        current: dict[str, float] = {}  # key -> distance in days of normal range
        events: list[Event] = []
        for measured in risk.positions:
            if not measured.atr_day or not state.stops_known:
                continue
            for which, price, gap in self._near_targets(measured):
                key = f"{which}:{'|'.join(measured.position.key)}:{price:g}"
                current[key] = gap / measured.atr_day
                if current[key] < NEAR_ATR and key not in known:
                    known.add(key)
                    events.append(self._near_event(measured, which, price, gap, risk.equity))
        for key in list(known):
            if key not in current or current[key] > NEAR_RELEASE_ATR:
                known.discard(key)
        if known != before:
            self._journal.set_setting(NEAR_SETTING, json.dumps(sorted(known)))
        return events

    def _near_known(self) -> set[str]:
        if self._near is None:
            try:
                self._near = set(json.loads(self._journal.get_setting(NEAR_SETTING) or "[]"))
            except (ValueError, TypeError):
                self._near = set()
        return self._near

    @staticmethod
    def _near_targets(measured: PositionRisk):
        position = measured.position
        mark = position.mark_px
        if not mark:
            return
        long = position.direction == "long"
        if position.stop_loss is not None and measured.stop_gap:  # 0: the stop is already past the price
            yield "sl", position.stop_loss, measured.stop_gap
        tp = position.take_profit
        if tp is not None:
            gap = tp - mark if long else mark - tp
            if gap > 0:
                yield "tp", tp, gap

    def _near_event(self, measured: PositionRisk, which: str, price: float, gap: float, equity: float | None) -> Event:
        position = measured.position
        name = f"{position.inst_id} {_side(position)}单"
        ccy = position.pnl_ccy
        days = gap / measured.atr_day
        made = pnl_at(position, price)
        share = f"（占权益 {abs(made) / equity * 100:.1f}%）" if made is not None and equity and equity > 0 else ""
        if which == "sl":
            where = f"标记价 {_px(position.mark_px)}，离止损 {_px(price)} 只剩 {gap:,.2f}（{days:.2f} 天的正常波幅）"
            lines = [where]
            if made is not None:
                lines.append(
                    f"打到止损的话，按开仓均价 {_px(position.avg_px)} 算这一单{'赚' if made >= 0 else '亏'}约 {abs(made):,.2f} {ccy}{share}，"
                    f"现在的浮动盈亏是 {_money(position.upl, ccy)}"
                )
            how = self._how_to_act([position.inst_id])
            title = f"离止损很近：{name}"
        else:
            where = f"标记价 {_px(position.mark_px)}，离止盈 {_px(price)} 还有 {gap:,.2f}（{days:.2f} 天的正常波幅）"
            lines = [where]
            if made is not None:
                lines.append(f"到止盈的话，按开仓均价 {_px(position.avg_px)} 算这一单赚约 {made:,.2f} {ccy}{share}")
            how = ""
            title = f"快到止盈：{name}"
        if how:
            lines.append(how)
        return Event(
            kind="near_stop" if which == "sl" else "near_tp",
            title=title,
            inst_id=position.inst_id,
            private_lines=tuple(lines),
            public_lines=(where,),
            to_group=True,
        )

    def _without_stops(self, state: AccountState, now: float, risk: AccountRisk | None = None) -> list[Event]:
        """Positions that have gone without a stop-loss for longer than the owner allows.

        Each is pointed out once, and again a day later while it stays that way.
        Whether to set a stop, and where, stays the owner's decision.
        """
        minutes = self._cfg.alert_no_stop_minutes
        if minutes is None or not state.stops_known:
            return []
        events: list[Event] = []
        seen = set()
        for position in state.positions:
            key = position.key
            seen.add(key)
            name = NO_STOP_SETTING + "|".join(key)
            if position.stop_loss is not None:
                if self._bare_since.pop(key, None) is not None or self._journal.get_setting(name):
                    self._journal.set_setting(name, "")  # it has a stop now: the next time starts afresh
                continue
            since = self._bare_since.setdefault(key, now)
            if now - since < minutes * 60:
                continue
            try:
                last = float(self._journal.get_setting(name) or 0)
            except ValueError:
                last = 0.0
            if now - last < NO_STOP_REPEAT_SECONDS:
                continue
            self._journal.set_setting(name, repr(now))
            events.append(self._no_stop_event(position, minutes, risk))
        for key in [key for key in self._bare_since if key not in seen]:
            del self._bare_since[key]  # closed
            self._journal.set_setting(NO_STOP_SETTING + "|".join(key), "")
        return events

    def _no_stop_event(self, position: Position, minutes: float, risk: AccountRisk | None = None) -> Event:
        cfg = self._cfg
        lines = [
            f"数量 {_size(position)}，均价 {_px(position.avg_px)}，标记价 {_px(position.mark_px)}，"
            f"浮动盈亏 {_money(position.upl, position.pnl_ccy)}"
        ]
        measured = risk.of(position) if risk is not None else None
        if measured is not None and liq_line(measured):
            lines.append(liq_line(measured))
            lines.append(stop_line(measured))
        elif position.liq_px and position.liq_distance_pct is not None:
            lines.append(f"预估强平价 {_px(position.liq_px)}，离现在的标记价 {position.liq_distance_pct:.2f}%")
        lines.append(f"这个仓位已经 {minutes:g} 分钟以上没有止损单。")
        how = self._how_to_act([position.inst_id], close=False)
        if how:
            lines.append(how)
        lines.append("挂上止损后不再提醒；一直不挂，每 24 小时提醒一次。")
        return Event(
            kind="no_stop",
            title=f"还没有止损：{position.inst_id} {_side(position)}单",
            inst_id=position.inst_id,
            private_lines=tuple(lines),
            warning=True,
        )

    def _how_to_act(self, inst_ids: list[str], close: bool = True) -> str:
        """The commands that would lower the risk, ready to send; the price is the owner's to fill in.
        "" when there is nothing he could send: trading off, or those commands not open."""
        cfg = self._cfg
        if cfg.trading != "live":
            return ""  # on the demo account an order would not touch the position being watched
        bases = [inst_id.split("-")[0] for inst_id in inst_ids if inst_id in cfg.tradable_instruments]
        if not bases:
            return ""
        commands = []
        if "stops" in cfg.trade_actions:
            commands.append(f"“止损 {bases[0]} 价格”")
        if close and "close" in cfg.trade_actions:
            commands.append(f"“平仓 {bases[0]} 一半”")
        if not commands:
            return ""
        where = "私聊或在群里 @我" if cfg.group_trading and cfg.group_chat_id and cfg.group_show_pnl else "私聊"
        return f"要调的话，{where}发{'或'.join(commands)}，回确认码才执行。"

    def _cooled_down(self, event: Event, now: float) -> bool:
        key = (event.kind, event.inst_id)
        last = self._cooldowns.get(key)
        if last is not None and now - last < self._cfg.alert_cooldown_minutes * 60:
            return False
        self._cooldowns[key] = now
        return True

    def _on_failure(self, exc: Exception) -> None:
        self._failures += 1
        self.last_error = str(exc)
        log.warning("%s read failed (%d in a row): %s", self._cfg.broker_name, self._failures, exc)
        if self._failures == FAILURES_BEFORE_ALERT and not self._down_reported:
            self._down_reported = True
            self._safe_notify(
                Event(
                    kind="okx_down",
                    title=f"读不到 {self._cfg.broker_name} 账户",
                    private_lines=(
                        f"已连续 {self._failures} 次读取失败，监控暂时停摆。",
                        f"原因：{exc}",
                        "MT5 要在服务器上开着并登录账户；远程桌面用完请直接关窗口，不要注销。"
                        if self._cfg.mt5 else "如果提示 IP 不在白名单或 key 无效，请检查 OKX 里这把 key 的设置。",
                    ),
                    warning=True,
                )
            )

    def _on_success(self) -> None:
        if self._down_reported:
            self._safe_notify(Event(kind="okx_recovered", title=f"{self._cfg.broker_name} 账户读取已恢复", private_lines=("监控恢复正常。",)))
        self._failures = 0
        self._down_reported = False
        self.last_error = ""

    def _safe_notify(self, event: Event) -> None:
        try:
            self._notify(event)
        except Exception:
            log.exception("could not deliver event %s", event.kind)
