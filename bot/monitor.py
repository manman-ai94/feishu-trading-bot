"""Watches the account: turns changes between two polls into events, and
checks the risk thresholds. No AI involved, only rules."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import Config
from .journal import Journal
from .models import AccountState, Position
from .okx_client import OkxClient, OkxError, OkxNetworkError

log = logging.getLogger(__name__)

QTY_TOLERANCE = 1e-9
FAILURES_BEFORE_ALERT = 3


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
                        "保证金率到 100% 会开始强平。要不要减仓由你决定。",
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
    ):
        self._okx = okx
        self._cfg = cfg
        self._journal = journal
        self._notify = notify
        self._clock = clock
        self._lock = threading.Lock()
        self._state: AccountState | None = None
        self._positions: dict[tuple, Position] | None = None
        self._last_snapshot_at = 0.0
        self._cooldowns: dict[tuple[str, str], float] = {}
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

        current = {position.key: position for position in state.positions}
        events: list[Event] = []
        if self._positions is None:
            log.info("baseline: %d open position(s)", len(current))
        else:
            events.extend(diff_positions(self._positions, current))
        self._positions = current
        with self._lock:
            self._state = state

        if now - self._last_snapshot_at >= self._cfg.snapshot_minutes * 60 or events:
            self._journal.record_snapshot(state, now)
            self._last_snapshot_at = now

        for event in check_risk(state, self._cfg, self.day_start_equity()):
            if self._cooled_down(event, now):
                events.append(event)

        for event in events:
            self._journal.record_event(event.kind, event.title + "；" + "；".join(event.private_lines), event.inst_id, ts=now)
            self._safe_notify(event)
        return state

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:  # keep the loop alive whatever happens
                log.exception("monitor poll crashed")
            stop.wait(self._cfg.poll_seconds)

    # ------------------------------------------------------------- internals

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
        log.warning("OKX read failed (%d in a row): %s", self._failures, exc)
        if self._failures == FAILURES_BEFORE_ALERT and not self._down_reported:
            self._down_reported = True
            self._safe_notify(
                Event(
                    kind="okx_down",
                    title="读不到 OKX 账户",
                    private_lines=(
                        f"已连续 {self._failures} 次读取失败，监控暂时停摆。",
                        f"原因：{exc}",
                        "如果提示 IP 不在白名单或 key 无效，请检查 OKX 里这把 key 的设置。",
                    ),
                    warning=True,
                )
            )

    def _on_success(self) -> None:
        if self._down_reported:
            self._safe_notify(Event(kind="okx_recovered", title="OKX 账户读取已恢复", private_lines=("监控恢复正常。",)))
        self._failures = 0
        self._down_reported = False
        self.last_error = ""

    def _safe_notify(self, event: Event) -> None:
        try:
            self._notify(event)
        except Exception:
            log.exception("could not deliver event %s", event.kind)
