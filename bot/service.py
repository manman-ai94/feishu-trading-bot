"""Reporter: decides what goes to the owner's private chat and what goes to
the group, and sends it."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from . import __version__
from .analyst import Analyst, AnalystUnavailable
from .book import book_text, read_book
from .closes import close_lines, history_text, read_closed, read_triggers
from .config import Config
from .discipline import DayPlan, day_label, plan_due
from .feishu_client import FeishuClient
from .journal import Journal
from .market import build_market_context
from .exposure import card_lines, measure
from .models import AccountState
from .monitor import Event, Monitor, position_brief
from .okx_client import OkxClient, OkxError, OkxNetworkError
from .reports import DISCLAIMER, account_text, equity_text, event_markdown, journal_text, status_markdown
from .review import week_text
from .risk import AccountRisk, weekend_lines
from .track import scorecard_markdown
from .times import both
from .upkeep import downtime_text
from .watch import SETTING as WATCH_SETTING

log = logging.getLogger(__name__)
WATCH_HINT = "想让我持仓期间按时盯着，发“每 1 小时盯一下仓位”；只想过一会儿再看一次，发“15 分钟后再看一下”。"

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
RECENT_ORDERS_HOURS = 24
RECENT_ORDERS_SHOWN = 8
STOP_NOTE_SECONDS = 600  # a trailing stop's reason waits this long for the card of its change
RESULT_NAMES = {"ok": "已执行", "rejected": "没有执行", "partial": "只执行了一部分", "unknown": "结果不确定"}


def _usd(value: float | None) -> str:
    return "未知" if value is None else f"{value:,.2f} USD"


def recent_orders_text(rows: list[dict], tz) -> str:
    """What the bot sent to OKX on the owner's code lately, and how each went, for the AI: so that
    "刚才那单怎么没成" is answered from the record, OKX's own reason included, and not guessed."""
    if not rows:
        return ""
    lines = [f"机器人最近 {RECENT_ORDERS_HOURS} 小时按他的确认码发给 OKX 的指令（结果是 OKX 的原话）："]
    for row in rows[-RECENT_ORDERS_SHOWN:]:
        when = datetime.fromtimestamp(row["ts"], tz=tz).strftime("%m-%d %H:%M")
        mode = "模拟盘" if row["mode"] == "demo" else "实盘"
        detail = row["detail"].replace("\n", "；")
        if len(detail) > 200:
            detail = detail[:200] + "……"
        auto = "（移动止损，程序自动发的，不用确认码）" if row.get("source") == "trail" else ""
        lines.append(f"- {when} {mode} {row['summary']}{auto}：{RESULT_NAMES.get(row['result'], row['result'])}。{detail}")
    return "\n".join(lines)


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
        self.discipline = None  # discipline.Discipline, set by the app: the day's plan and when the day stops
        self._stop_notes: dict[str, tuple[float, str]] = {}  # instrument -> (when, why its stop is being moved)
        self._notes_lock = threading.Lock()
        self._plan_lock = threading.Lock()

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

    def to_chat(self, chat_id: str, private: bool, title: str, markdown: str, template: str = "blue") -> None:
        """A card with amounts on it, to the chat the owner asked for it in: his private chat, or the
        registered group while it shows the account. Anywhere else (a group registered since, a group that
        no longer shows the account, no chat at all) it goes to him privately."""
        if chat_id and private:
            self._feishu.send_markdown(chat_id, title, markdown, "chat_id", template)
        elif chat_id and chat_id == self._cfg.group_chat_id and self.group_shares_account:
            self._feishu.send_markdown(chat_id, title, markdown, "chat_id", template, DISCLAIMER)
        else:
            self.to_owner(title, markdown, template)

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

    def exposure_lines(self, state: AccountState | None) -> list[str]:
        """What every stop on the account would cost together, and what it is made of: for the owner's card
        and the AI. [] when there is nothing on the account or it cannot be read."""
        if state is None:
            return []
        specs: dict[str, Decimal | None] = {}

        def per(inst_id: str) -> Decimal | None:
            if inst_id not in specs:
                try:
                    specs[inst_id] = self._okx.instrument_spec(inst_id).coins_per_contract
                except (OkxError, OkxNetworkError):
                    specs[inst_id] = None
            return specs[inst_id]

        try:
            orders = self._okx.pending_orders()
            triggers = [row for row in self._okx.pending_algos("trigger") if isinstance(row, dict)]
            exposure = measure(state, orders, triggers, per)
        except Exception:  # figures beside the account: a card is never held up for them
            log.exception("could not measure the account's total risk")
            return []
        equity = Decimal(str(state.snapshot.total_eq or 0))
        return card_lines(exposure, equity, self._cfg.trade_max_total_risk_pct)

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
        total = self.exposure_lines(state)
        if total:
            text += "\n" + "\n".join(line.replace("**", "") for line in total)
        try:
            orders = book_text(read_book(self._okx), self._okx, state.snapshot.total_eq, self._cfg.tz)
        except Exception:  # the orders sit beside the account: an answer is never held up for them
            log.exception("could not write out the waiting orders for the AI")
            orders = "挂着的委托：这次没读到，不要猜，直接说没读到。"
        now = self._clock()
        try:
            sent = recent_orders_text(self._journal.orders_between(now - RECENT_ORDERS_HOURS * 3600, now + 1), self._cfg.tz)
        except Exception:
            log.exception("could not read the bot's recent orders for the AI")
            sent = ""
        return text + "\n" + orders + ("\n" + sent if sent else "")

    def send_alert(self, title: str, markdown: str) -> None:
        """A price alert the owner set: to him, and to the group when he shares the account there.
        Raises when the owner could not be told, so the alert stays and is tried again."""
        self.to_owner(title, markdown, "orange")
        if self.group_shares_account:
            try:
                self.to_group(title, markdown, "orange", footer=DISCLAIMER)
            except Exception:
                log.exception("could not send a price alert to the group")

    def history_for_ai(self, days: int) -> str:
        """Positions closed in the last `days` days: how each closed and what it made, from OKX's own records."""
        try:
            closed, triggers = read_closed(self._okx), read_triggers(self._okx)
        except (OkxError, OkxNetworkError) as exc:
            return f"这次没读到 OKX 的平仓记录：{exc}"
        return history_text(closed, triggers, self._cfg.tz, self._clock() - days * 86400)

    def last_close_lines(self) -> list[str]:
        """The latest close, for the status card while nothing is held: "刚平仓赚了多少" answered on the card."""
        try:
            closed, triggers = read_closed(self._okx), read_triggers(self._okx)
        except (OkxError, OkxNetworkError, AttributeError) as exc:
            log.info("no close history for the status card: %s", exc)
            return []
        if not closed:
            return []
        last = closed[-1]
        side = "多" if last.direction == "long" else "空"
        size = f" {last.closed_size:,.10g} 张" if last.closed_size else ""
        head = f"**最近一笔平仓**：{both(last.closed_at, self._cfg.tz)} {last.inst_id.split('-')[0]} {side}单{size}"
        return [head, *(f"- {line}" for line in close_lines(last, triggers))]

    # ----------------------------------------------------------------- events

    def expect_stop(self, inst_id: str, line: str) -> None:
        """Why the next stop change of this instrument happens (a trailing stop's move, trail.py), for the card
        the monitor sends when it sees it. "" forgets it."""
        with self._notes_lock:
            if line:
                self._stop_notes[inst_id] = (self._clock(), line)
            else:
                self._stop_notes.pop(inst_id, None)

    def _with_stop_note(self, event: Event) -> Event:
        if event.kind != "stop_changed":
            return event
        with self._notes_lock:
            note = self._stop_notes.pop(event.inst_id, None)
        if note is None or self._clock() - note[0] > STOP_NOTE_SECONDS:
            return event
        return replace(
            event, title=event.title.replace("止盈止损变动", "移动止损"),
            private_lines=(*event.private_lines, note[1]), public_lines=(*event.public_lines, note[1]),
        )  # fmt: skip

    def notify_event(self, event: Event) -> None:
        event = self._with_stop_note(event)
        template = "red" if event.warning else EVENT_COLOURS.get(event.kind, "blue")
        hint = self._watch_hint(event)
        try:
            self.to_owner(event.title, event_markdown(event, private=True) + hint, template)
        except Exception:  # still tell the group if the private chat fails
            log.exception("could not send event %s to the owner", event.kind)
        # Position changes always go to the group. Risk warnings go there too
        # once the owner shares the account; technical notices stay private.
        shared = self.group_shares_account
        if self._cfg.group_chat_id and (event.to_group or (shared and event.kind in RISK_KINDS)):
            body = event_markdown(event, private=shared)
            self.to_group(event.title, (body or "（无更多信息）") + hint, template, footer=DISCLAIMER)
        if event.kind in ("closed", "reduced") and self.breaker_alert is not None:
            self._tell_breaker()
        if event.kind in ("closed", "reduced") and self.discipline is not None:
            self._tell_day_stop()
        if event.kind == "closed" and self.reviewer is not None:
            self.reviewer.on_close(event.inst_id)

    def _watch_hint(self, event: Event) -> str:
        """Under an open or an addition, while the timed position watch is off: how to have the position
        watched, so that "现在怎么说" need not be asked again and again."""
        if event.kind not in ("opened", "increased"):
            return ""
        try:
            if self._journal.get_setting(WATCH_SETTING, ""):
                return ""
        except Exception:
            return ""
        return f"\n\n{WATCH_HINT}"

    def send_review(self, title: str, markdown: str) -> None:
        """A closed position's review: to the owner, and to the group when the owner shares the account there."""
        self._owner_safely(title, markdown, "violet")
        if self.group_shares_account:
            self.to_group(title, markdown, "violet", footer=DISCLAIMER)

    def _tell_breaker(self) -> None:
        """After a close: whether that stop-out tripped the circuit breaker. The owner hears it, and the group
        when he shares the account there: the group and the private chat show the same."""
        try:
            text = self.breaker_alert()
            if text:
                self._owner_safely("熔断：开仓已暂停", text, "red")
                if self.group_shares_account:
                    self.to_group("熔断：开仓已暂停", text, "red", footer=DISCLAIMER)
        except Exception:
            log.exception("could not check the circuit breaker")

    def _tell_day_stop(self) -> None:
        """After a close: whether the day has just stopped (losses in a row, or the day's loss limit)."""
        try:
            private, public = self.discipline.stop_alert()
            if private:
                self._owner_safely("今天停手：不再开新仓", private, "red")
                if self._cfg.group_chat_id:
                    self.to_group("今天停手：不再开新仓", private if self.group_shares_account else public, "red", footer=DISCLAIMER)
        except Exception:
            log.exception("could not check whether the day stopped")

    # ------------------------------------------------------------------- plans

    def write_plan(self, reason: str = "", again: bool = False) -> tuple[DayPlan | None, str]:
        """Has the AI write the current trading day's plan, keeps it and sends its card to the owner and the
        group. Returns (the plan, "") or (None, why not). One at a time: the 17:05 job and a start-up that
        finds no plan must not write two."""
        with self._plan_lock:
            return self._write_plan(reason, again)

    def _write_plan(self, reason: str, again: bool) -> tuple[DayPlan | None, str]:
        discipline = self.discipline
        if discipline is None:
            return None, "交易计划没有打开。"
        day = discipline.today()
        earlier = discipline.plan(day)
        if earlier is not None and not again:
            return earlier, ""
        if earlier is not None and discipline.revisions_left(day) <= 0:
            return None, f"今天的计划已经重做过了，一天只能重做一次。新的交易日 {discipline.next_day_text()}开始。"
        try:
            status = discipline.status()
        except (OkxError, OkxNetworkError):
            status = None
        state = self.current_state()
        account = self.account_for_ai() if state is not None and state.positions else None
        try:
            text, items = self._analyst.day_plan(
                day_label(day), self.market_text(), account, discipline.rules_text(status, amounts=True),
                self._cfg.tradable_instruments, earlier.text if earlier else "", reason,
            )  # fmt: skip
        except AnalystUnavailable as exc:
            log.warning("the day's plan was not written: %s", exc)
            return None, f"交易计划没有写成：{exc}"
        if not items:
            log.warning("the day's plan came without the block the program reads")
            return None, "交易计划没有写成：AI 没给出程序能读的方向和关键位。请再发一次“出今天的计划”。"
        plan = discipline.keep(day, items, text, reason)
        title = discipline.title(plan)
        self._owner_safely(title, discipline.card(plan, amounts=True), "turquoise")
        if self._cfg.group_chat_id:
            try:
                self.to_group(title, discipline.card(plan, amounts=self.group_shares_account), "turquoise", footer=DISCLAIMER)
            except Exception:
                log.exception("could not send the plan to the group")
        return plan, ""

    def scheduled_plan(self) -> None:
        """At 17:05 New York, Sunday to Thursday: the plan of the trading day that has just begun."""
        if self.discipline is None or plan_due(self._clock()) is None:
            return
        self._plan_or_say_why()

    def plan_if_missing(self) -> None:
        """On start: the plan of a weekday's trading day that has none yet (the first run after an install, or a
        restart that missed 17:05). Once a day at most, since the plan is kept."""
        discipline = self.discipline
        if discipline is None or not self._cfg.plan_required:
            return
        day = discipline.today()
        if day.weekday() >= 5 or discipline.plan(day) is not None:
            return
        log.info("the trading day %s has no plan yet: writing it now", day)
        self._plan_or_say_why()

    def _plan_or_say_why(self) -> None:
        plan, why = self.write_plan()
        if plan is None:
            self._owner_safely("交易计划没有写成", f"{why}\n没有计划时不能开新仓。", "red")

    def plan_note(self, with_status: bool) -> str:
        if self.discipline is None:
            return ""
        try:
            return self.discipline.notes_for_ai(with_status)
        except Exception:
            log.exception("could not write the day's plan out for the AI")
            return ""

    def send_startup(self, downtime: tuple[float, float] | None = None, restarted: str = "", news: str = "") -> None:
        """`downtime` is (last seen running, now) when the bot was off long enough to matter; `restarted` says
        why the health check restarted it; `news` is what this version changed, on its first run."""
        state = self.current_state()
        version = f"（v{__version__}）"
        if state is None:
            body = f"机器人已启动{version}，但还没读到 {self._cfg.broker_name} 账户。稍后如果连续失败，我会再提醒你。"
        else:
            # Short: a restart is not news. The positions in one line each; the full card is one word away.
            lines = [f"机器人已启动{version}，开始监控。", "", f"**账户权益** {_usd(state.snapshot.total_eq)}"]
            lines.extend(f"- {position_brief(position)}" for position in state.positions)
            if not state.positions:
                lines.append("- 当前没有持仓")
            lines.append("")
            lines.append("完整的账户和风险读数发“仓位”。")
            body = "\n".join(lines)
        if restarted:
            body = restarted + "\n\n" + body
        if downtime is not None:
            body = downtime_text(downtime, self._cfg.tz) + "\n\n" + body
        if self._cfg.trading_on:
            where = "模拟盘" if self._cfg.trading == "demo" else "实盘"
            body += f"\n下单功能开着（{where}），每条指令回确认码才执行，发“停止交易”随时叫停。"
        if news:
            body += "\n\n" + news
        self.to_owner("交易助手已上线", body, "orange" if downtime is not None or restarted else "green")

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
                    kind, market, account, journal if kind == "evening" else None, for_owner=True, shared_with_group=shared,
                    plan_note=self.plan_note(True),
                )  # fmt: skip
            except AnalystUnavailable as exc:
                log.warning("%s brief not generated: %s", kind, exc)
                self._owner_safely(title, f"这次简报没有生成：{exc}", "grey")
            else:
                self._owner_safely(title, text, "indigo")
                if shared:
                    self.to_group(title, text, "indigo", footer=DISCLAIMER)
        if self._cfg.group_chat_id and not shared:
            try:
                text = self._analyst.brief(kind, market, None, None, for_owner=False, plan_note=self.plan_note(False))
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
        # The week's track record goes with the review, wherever the review went.
        if self._cfg.track_record and self._journal.analyses_between(start, end):
            record = scorecard_markdown(self._journal, self._clock(), self._cfg.tz, 7, None, self._cfg.track_window_hours)
            self._owner_safely("本周成绩单", record, "green")
            if shared:
                self.to_group("本周成绩单", record, "green", footer=DISCLAIMER)

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
