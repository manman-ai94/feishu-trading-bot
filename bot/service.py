"""Reporter: decides what goes to the owner's private chat and what goes to
the group, and sends it."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

from .analyst import Analyst, AnalystUnavailable
from .book import book_text, read_book
from .closes import history_text, read_closed, read_triggers
from .config import Config
from .feishu_client import FeishuClient
from .journal import Journal
from .market import build_market_context
from .models import AccountState
from .monitor import Event, Monitor, position_brief
from .okx_client import OkxClient, OkxError, OkxNetworkError
from .reports import DISCLAIMER, account_text, equity_text, event_markdown, journal_text, status_markdown
from .review import week_text
from .risk import AccountRisk, weekend_lines
from .track import scorecard_markdown
from .upkeep import downtime_text

log = logging.getLogger(__name__)

EVENT_COLOURS = {
    "near_stop": "orange",
    "near_tp": "green",
    "opened": "blue",
    "increased": "blue",
    "reduced": "wathet",
    "closed": "grey",
    "stop_changed": "yellow",
    "okx_recovered": "green",
}
RISK_KINDS = ("margin_warning", "liq_warning", "daily_loss", "no_stop", "risk_level")


def _usd(value: float | None) -> str:
    return "未知" if value is None else f"{value:,.2f} USD"


class Reporter:
    def __init__(
        self,
        cfg: Config,
        feishu: FeishuClient,
        okx: OkxClient,
        analyst: Analyst,
        journal: Journal,
        clock: Callable[[], float] = time.time,
        crowd=None,
        gauge=None,
        macro=None,
    ):
        self._cfg = cfg
        self._gauge = gauge  # risk.RiskGauge, or None
        self._feishu = feishu
        self._okx = okx
        self._analyst = analyst
        self._journal = journal
        self._clock = clock
        self._crowd = crowd  # crowd.Crowd, or None when CROWD_DATA is off
        self._macro = macro  # macro.Macro, or None when MACRO_DATA is off
        self.breaker_alert: Callable[[], str] | None = None  # set by the app: the trade desk's circuit breaker notice
        self.monitor: Monitor | None = None  # set by the app once both exist
        self.reviewer = None  # review.Reviewer, set by the app when CLOSE_REVIEW is on

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

    def risk(self, state: AccountState | None) -> AccountRisk | None:
        """How the positions stand against the market's own movement. None when it cannot be measured."""
        if self._gauge is None or state is None or not state.positions:
            return None
        try:
            return self._gauge.assess(state)
        except Exception:  # figures beside the position: a card is never held up for them
            log.exception("could not measure the positions' risk")
            return None

    def account_for_ai(self, fresh: bool = False) -> str | None:
        """The account, the positions and every order waiting on it (the bot's and those placed by hand),
        as the AI reads them. `fresh` reads the account from OKX instead of taking the monitor's last read."""
        state = None
        if fresh:
            try:
                state = self._okx.account_state()
            except (OkxError, OkxNetworkError) as exc:
                log.warning("could not read the account for the AI: %s", exc)
        if state is None:
            state = self.current_state()
        if state is None:
            return None
        text = account_text(state, self.day_change(state), self.risk(state), self._cfg.risk_per_trade_pct)
        try:
            orders = book_text(read_book(self._okx), self._okx, state.snapshot.total_eq, self._cfg.tz)
        except Exception:  # the orders sit beside the account: an answer is never held up for them
            log.exception("could not write out the waiting orders for the AI")
            orders = "挂着的委托：这次没读到，不要猜，直接说没读到。"
        return text + "\n" + orders

    def history_for_ai(self, days: int) -> str:
        """Positions closed in the last `days` days: how each closed and what it made, from OKX's own records."""
        try:
            closed, triggers = read_closed(self._okx), read_triggers(self._okx)
        except (OkxError, OkxNetworkError) as exc:
            return f"这次没读到 OKX 的平仓记录：{exc}"
        return history_text(closed, triggers, self._cfg.tz, self._clock() - days * 86400)

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
        if event.kind in ("closed", "reduced") and self.breaker_alert is not None:
            self._tell_breaker()
        if event.kind == "closed" and self.reviewer is not None:
            self.reviewer.on_close(event.inst_id)

    def send_review(self, title: str, markdown: str) -> None:
        """A closed position's review: to the owner, and to the group when the owner shares the account there."""
        self._owner_safely(title, markdown, "violet")
        if self.group_shares_account:
            self.to_group(title, markdown, "violet", footer=DISCLAIMER)

    def _tell_breaker(self) -> None:
        """After a close: whether that stop-out tripped the circuit breaker. Only the owner hears it."""
        try:
            text = self.breaker_alert()
            if text:
                self.to_owner("熔断：开仓已暂停", text, "red")
        except Exception:
            log.exception("could not check the circuit breaker")

    def send_startup(self, downtime: tuple[float, float] | None = None) -> None:
        """`downtime` is (last seen running, now) when the bot was off long enough to matter."""
        state = self.current_state()
        if state is None:
            body = "机器人已启动，但还没读到 OKX 账户。稍后如果连续失败，我会再提醒你。"
        else:
            # Short: a restart is not news. The positions in one line each; the full card is one word away.
            lines = ["机器人已启动，开始监控。", "", f"**账户权益** {_usd(state.snapshot.total_eq)}"]
            lines.extend(f"- {position_brief(position)}" for position in state.positions)
            if not state.positions:
                lines.append("- 当前没有持仓")
            lines.append("")
            lines.append("完整的账户和风险读数发“仓位”。")
            body = "\n".join(lines)
        if downtime is not None:
            body = downtime_text(downtime, self._cfg.tz) + "\n\n" + body
        if self._cfg.trading_on:
            where = "模拟盘" if self._cfg.trading == "demo" else "实盘"
            body += f"\n下单功能开着（{where}），每条指令回确认码才执行，发“停止交易”随时叫停。"
        self.to_owner("交易助手已上线", body, "orange" if downtime is not None else "green")

    # ----------------------------------------------------------------- briefs

    def market_text(self) -> str:
        """What a brief or a review is written from: every watched instrument, how the crowd stands in it,
        and US rates and the dollar."""
        blocks = []
        for inst_id in self._cfg.watch_instruments:
            blocks.append(build_market_context(self._okx, inst_id).to_text())
            crowd = self._crowd_text(inst_id)
            if crowd:
                blocks.append(crowd)
        macro = self._macro_text()
        if macro:
            blocks.append("美国利率和美元（FRED）：\n" + macro)
        return "\n\n".join(blocks)

    def _macro_text(self) -> str:
        if self._macro is None:
            return ""
        try:
            return self._macro.text()
        except Exception:  # background only, like the crowd
            log.exception("could not read US rates and the dollar")
            return ""

    def _crowd_text(self, inst_id: str) -> str:
        if self._crowd is None:
            return ""
        try:
            return self._crowd.text(inst_id)
        except Exception:  # background only: a brief is never held up for it
            log.exception("could not read how the crowd is positioned in %s", inst_id)
            return ""

    def send_brief(self, kind: str) -> None:
        """kind is 'morning' or 'evening'.

        The full version (with the account) goes to the owner, and to the group
        as well when the owner shares the account. Otherwise the group gets a
        separate market-only version.
        """
        title = ("盘前简报" if kind == "morning" else "收盘小结") + " · " + self.now_local().strftime("%m月%d日")
        market = self.market_text()
        state = self.current_state()
        account = self.account_for_ai() if state else None
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
        figures = week_text(self._journal.reviews_between(start, end))
        try:
            text = self._analyst.weekly_review(self.market_text(), journal, equity, shared_with_group=shared, week_figures=figures)
        except AnalystUnavailable as exc:
            self._owner_safely(title, f"这次复盘没有生成：{exc}", "grey")
            return
        self._owner_safely(title, text, "purple")
        if shared:
            self.to_group(title, text, "purple", footer=DISCLAIMER)
        # The week's track record goes with the review, to the owner only: figures, not AI text.
        if self._cfg.track_record and self._journal.analyses_between(start, end):
            record = scorecard_markdown(self._journal, self._clock(), self._cfg.tz, 7, None, self._cfg.track_window_hours)
            self._owner_safely("本周成绩单", record, "green")

    def send_weekend_check(self) -> None:
        """Friday, an hour before spot gold closes: what stands out in the positions that will sit through the weekend.

        Rules only. Nothing is sent when nothing stands out.
        """
        state = self.current_state()
        risk = self.risk(state)
        if risk is None:
            return
        lines = weekend_lines(risk, self._cfg)
        if not lines:
            return
        body = "\n".join(
            [
                "现货黄金周五 22:00 UTC 收盘，周日 23:00 UTC 重开。OKX 的贵金属永续周末照常交易，但成交稀薄、价差更宽（OKX 的商品永续说明）。",
                *(f"- {line}" for line in lines),
                "这些是读数，不是指令。周末前要不要调整，由你决定。",
            ]
        )
        title = "周末前检查 · " + self.now_local().strftime("%m月%d日")
        self._owner_safely(title, body, "yellow")
        if self.group_shares_account:
            self.to_group(title, body, "yellow", footer=DISCLAIMER)

    def _owner_safely(self, title: str, markdown: str, template: str) -> None:
        """Send to the owner; a failure there must not stop the group copy."""
        try:
            self.to_owner(title, markdown, template)
        except Exception:
            log.exception("could not send %r to the owner", title)
