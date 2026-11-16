"""Reporter: decides what goes to the owner's private chat and what goes to
the group, and sends it."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

from .analyst import Analyst, AnalystUnavailable
from .config import Config
from .feishu_client import FeishuClient
from .journal import Journal
from .market import build_market_context
from .models import AccountState
from .monitor import Event, Monitor
from .okx_client import OkxClient
from .reports import DISCLAIMER, account_text, equity_text, event_markdown, journal_text, status_markdown

log = logging.getLogger(__name__)

EVENT_COLOURS = {
    "opened": "blue",
    "increased": "blue",
    "reduced": "wathet",
    "closed": "grey",
    "stop_changed": "yellow",
    "okx_recovered": "green",
}
RISK_KINDS = ("margin_warning", "liq_warning", "daily_loss")


class Reporter:
    def __init__(
        self,
        cfg: Config,
        feishu: FeishuClient,
        okx: OkxClient,
        analyst: Analyst,
        journal: Journal,
        clock: Callable[[], float] = time.time,
    ):
        self._cfg = cfg
        self._feishu = feishu
        self._okx = okx
        self._analyst = analyst
        self._journal = journal
        self._clock = clock
        self.monitor: Monitor | None = None  # set by the app once both exist

    # ----------------------------------------------------------- destinations

    def to_owner(self, title: str, markdown: str, template: str = "blue", footer: str | None = None) -> bool:
        if not self._cfg.owner_open_id:
            log.info("no OWNER_OPEN_ID yet; not sending %r", title)
            return False
        self._feishu.send_markdown(self._cfg.owner_open_id, title, markdown, "open_id", template, footer)
        return True

    def to_group(self, title: str, markdown: str, template: str = "blue", footer: str | None = None) -> bool:
        if not self._cfg.group_chat_id:
            return False
        self._feishu.send_markdown(self._cfg.group_chat_id, title, markdown, "chat_id", template, footer)
        return True

    @property
    def group_shares_account(self) -> bool:
        """The owner chose to show balances and PnL in the group (GROUP_SHOW_PNL)."""
        return self._cfg.group_show_pnl and bool(self._cfg.group_chat_id)

    # ---------------------------------------------------------------- account

    def now_local(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=self._cfg.tz)

    def day_change(self, state: AccountState) -> float | None:
        if self.monitor is None or state.snapshot.total_eq is None:
            return None
        start = self.monitor.day_start_equity()
        return None if start is None else state.snapshot.total_eq - start

    def current_state(self) -> AccountState | None:
        return self.monitor.state if self.monitor else None

    # ----------------------------------------------------------------- events

    def notify_event(self, event: Event) -> None:
        template = "red" if event.warning else EVENT_COLOURS.get(event.kind, "blue")
        try:
            self.to_owner(event.title, event_markdown(event, private=True), template)
        except Exception:  # still tell the group if the private chat fails
            log.exception("could not send event %s to the owner", event.kind)
        # Position changes always go to the group. Risk warnings go there too
        # once the owner shares the account; technical notices stay private.
        shared = self.group_shares_account
        if self._cfg.group_chat_id and (event.to_group or (shared and event.kind in RISK_KINDS)):
            body = event_markdown(event, private=shared)
            self.to_group(event.title, body or "（无更多信息）", template, footer=DISCLAIMER)

    def send_startup(self) -> None:
        state = self.current_state()
        if state is None:
            body = "机器人已启动，但还没读到 OKX 账户。稍后如果连续失败，我会再提醒你。"
        else:
            body = "机器人已启动，开始监控。\n\n" + status_markdown(state, self.day_change(state), private=True)
        if self._cfg.trading_on:
            where = "模拟盘" if self._cfg.trading == "demo" else "实盘"
            body += f"\n\n下单功能已打开（{where}）。每条指令都要你回复确认码才执行，发“停止交易”可以随时叫停。"
        self.to_owner("交易助手已上线", body, "green")

    # ----------------------------------------------------------------- briefs

    def market_text(self) -> str:
        blocks = [build_market_context(self._okx, inst_id).to_text() for inst_id in self._cfg.watch_instruments]
        return "\n\n".join(blocks)

    def send_brief(self, kind: str) -> None:
        """kind is 'morning' or 'evening'.

        The full version (with the account) goes to the owner, and to the group
        as well when the owner shares the account. Otherwise the group gets a
        separate market-only version.
        """
        title = ("盘前简报" if kind == "morning" else "收盘小结") + " · " + self.now_local().strftime("%m月%d日")
        market = self.market_text()
        state = self.current_state()
        account = account_text(state, self.day_change(state)) if state else None
        today = self.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        journal = journal_text(self._journal.events_between(today.timestamp(), self._clock() + 1), self._cfg.tz)
        shared = self.group_shares_account

        if self._cfg.owner_open_id or shared:
            try:
                text = self._analyst.brief(
                    kind, market, account, journal if kind == "evening" else None, for_owner=True, shared_with_group=shared
                )
            except AnalystUnavailable as exc:
                log.warning("%s brief not generated: %s", kind, exc)
                self._owner_safely(title, f"这次简报没有生成：{exc}", "grey")
            else:
                self._owner_safely(title, text, "indigo")
                if shared:
                    self.to_group(title, text, "indigo", footer=DISCLAIMER)
        if self._cfg.group_chat_id and not shared:
            try:
                text = self._analyst.brief(kind, market, None, None, for_owner=False)
                self.to_group(title, text, "indigo", footer=DISCLAIMER)
            except AnalystUnavailable as exc:
                log.warning("group brief not generated: %s", exc)

    def send_weekly_review(self) -> None:
        end = self._clock() + 1  # the journal stores whole seconds; include the current one
        start = end - timedelta(days=7).total_seconds()
        journal = journal_text(self._journal.events_between(start, end), self._cfg.tz)
        equity = equity_text(self._journal.equity_series(start, end))
        title = "本周复盘 · " + self.now_local().strftime("%m月%d日")
        shared = self.group_shares_account
        try:
            text = self._analyst.weekly_review(self.market_text(), journal, equity, shared_with_group=shared)
        except AnalystUnavailable as exc:
            self._owner_safely(title, f"这次复盘没有生成：{exc}", "grey")
            return
        self._owner_safely(title, text, "purple")
        if shared:
            self.to_group(title, text, "purple", footer=DISCLAIMER)

    def _owner_safely(self, title: str, markdown: str, template: str) -> None:
        """Send to the owner; a failure there must not stop the group copy."""
        try:
            self.to_owner(title, markdown, template)
        except Exception:
            log.exception("could not send %r to the owner", title)
