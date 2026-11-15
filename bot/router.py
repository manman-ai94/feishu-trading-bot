"""Decides what to do with each incoming Feishu message.

Rules that matter:
  * By default the bot takes instructions from the owner only, in the private
    chat and in the group (GROUP_OWNER_ONLY=true). Everyone else is told so.
  * Account details (balance, PnL) are shown in the owner's private chat, and
    in the registered group when GROUP_SHOW_PNL=true (the default). There the
    AI is also given the account, so it can analyse the positions.
  * No other group ever sees account details, whoever asks.
  * Private chats from anyone other than the owner are not served.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Callable

from .analyst import Analyst, AnalystUnavailable
from .config import Config
from .feishu_client import FeishuClient, FeishuError, IncomingMessage
from .market import build_market_context
from .reports import DISCLAIMER, account_text, status_markdown
from .service import Reporter

log = logging.getLogger(__name__)

HELP_TEXT = """我是交易助手，可以做这些事：

- 发“仓位”，看当前持仓
- 直接问行情，例如“现在黄金怎么看”
- 发 /whoami，查看你的飞书 ID

主人在问题前面加“深度”，我会换更强的模型多想一会儿。

我每天定时发盘前简报和收盘小结，仓位有变动时会自动提醒。我只做分析和提醒，不替任何人做买卖决定。"""

OWNER_ONLY_TEXT = "我只响应主人的指令。仓位变动和简报我会自动发到群里。"

STATUS_WORDS = ("仓位", "持仓", "账户", "余额", "盈亏", "/status", "status", "position")
HELP_WORDS = ("/help", "help", "帮助", "你能做什么", "怎么用")
DEEP_PREFIXES = ("深度", "/deep")
INSTRUMENT_ALIASES = {"黄金": "XAU", "gold": "XAU", "白银": "XAG", "silver": "XAG", "比特币": "BTC", "以太坊": "ETH"}
SEEN_LIMIT = 500


class Router:
    def __init__(
        self,
        cfg: Config,
        feishu: FeishuClient,
        okx,
        analyst: Analyst,
        reporter: Reporter,
        bot_open_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._cfg = cfg
        self._feishu = feishu
        self._okx = okx
        self._analyst = analyst
        self._reporter = reporter
        self._bot_open_id = bot_open_id
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._group_questions: dict[str, int] = {}  # local date -> count

    # ------------------------------------------------------------------ entry

    def handle(self, message: IncomingMessage) -> None:
        if self._already_seen(message.message_id):
            return
        try:
            self._handle(message)
        except FeishuError:
            log.exception("could not reply in Feishu")
        except Exception:
            log.exception("unexpected error while handling a message")
            self._try_reply(message, "处理这条消息时出了点问题，我已经记到日志里了。")

    def _handle(self, message: IncomingMessage) -> None:
        cfg = self._cfg
        is_owner = bool(cfg.owner_open_id) and message.sender_open_id == cfg.owner_open_id

        if not message.is_private:
            # In a group, only answer when the bot itself is @-mentioned.
            if self._bot_open_id and self._bot_open_id not in message.mentioned_open_ids:
                return
            if not self._bot_open_id and not message.mentioned_open_ids:
                return

        text = message.text.strip()
        lowered = text.lower()

        if lowered.startswith("/whoami") or text == "我是谁":
            self._feishu.reply_text(
                message.message_id,
                f"你的 open_id：{message.sender_open_id}\n这个会话的 chat_id：{message.chat_id}\n"
                "私聊里拿到的 open_id 填到 OWNER_OPEN_ID，群里拿到的 chat_id 填到 GROUP_CHAT_ID。",
            )
            return

        if not is_owner:
            if message.is_private and not cfg.owner_open_id:
                self._feishu.reply_text(message.message_id, "机器人还没有登记主人。请发送 /whoami，把拿到的 open_id 填进 .env 的 OWNER_OPEN_ID。")
                return
            # Only the registered group is served. Until GROUP_CHAT_ID is filled in,
            # no group is, so a group the bot was merely added to learns nothing.
            if not message.is_private and message.chat_id != cfg.group_chat_id:
                self._feishu.reply_text(message.message_id, "这个群还没有登记，我暂时不在这里回答问题。")
                return
            if cfg.group_owner_only:
                self._feishu.reply_text(message.message_id, OWNER_ONLY_TEXT)
                return
            if message.is_private:
                self._feishu.reply_text(message.message_id, "这个机器人只在群里回答问题，请到群里 @我。")
                return

        if not message.is_text or not text:
            self._feishu.reply_text(message.message_id, "我目前只看得懂文字消息。发“帮助”可以看我能做什么。")
            return

        intent = self._intent(text)
        if intent == "help":
            self._feishu.reply_text(message.message_id, HELP_TEXT)
        elif intent == "status":
            self._reply_status(message)
        else:
            self._reply_analysis(message, text, is_owner)

    # ---------------------------------------------------------------- intents

    def _intent(self, text: str) -> str:
        lowered = text.lower()
        if any(lowered == word or lowered.startswith(word + " ") for word in HELP_WORDS) or lowered in ("你好", "hi", "hello"):
            return "help"
        # Short messages like "仓位" or "我的持仓" are a status request; longer
        # ones ("我的仓位风险大吗") go to the AI router below.
        if len(text) <= 6 and any(word in lowered for word in STATUS_WORDS):
            return "status"
        if lowered.startswith(DEEP_PREFIXES):
            return "analysis"
        if self._analyst.enabled:
            return self._analyst.route_intent(text)
        return "analysis"

    def _reply_status(self, message: IncomingMessage) -> None:
        state = self._reporter.current_state()
        if state is None:
            self._feishu.reply_text(message.message_id, "还没读到 OKX 账户，请稍后再试。")
            return
        with_account = self._shows_account(message)
        if not with_account and not state.positions:
            self._feishu.reply_text(message.message_id, "当前没有持仓。")
            return
        body = status_markdown(state, self._reporter.day_change(state), private=with_account)
        self._feishu.reply_markdown(message.message_id, "当前持仓", body, "blue", footer=None if message.is_private else DISCLAIMER)

    def _reply_analysis(self, message: IncomingMessage, text: str, is_owner: bool) -> None:
        in_group = not message.is_private
        if in_group and not is_owner and not self._allow_group_question():
            self._feishu.reply_text(message.message_id, "今天群里的提问次数用完了，明天再来。")
            return

        deep = False
        lowered = text.lower()
        for prefix in DEEP_PREFIXES:
            if lowered.startswith(prefix):
                deep = is_owner
                text = text[len(prefix) :].strip(" ：:，,") or "请对当前行情做一次深入分析。"
                break

        inst_id = self._pick_instrument(text)
        self._try_reply(message, "正在看行情，请稍等。")
        market = build_market_context(self._okx, inst_id)
        if not market.usable:
            self._feishu.reply_text(message.message_id, f"暂时拿不到 {inst_id} 的行情数据：{'；'.join(market.errors) or '未知原因'}")
            return

        account = None
        if self._shows_account(message):
            state = self._reporter.current_state()
            if state is not None:
                account = account_text(state, self._reporter.day_change(state))
        try:
            answer = self._analyst.answer(text, market.to_text(), account, in_group=in_group, deep=deep)
        except AnalystUnavailable as exc:
            self._feishu.reply_text(message.message_id, str(exc))
            return
        title = ("深度分析" if deep else "行情分析") + f" · {inst_id}"
        self._feishu.reply_markdown(message.message_id, title, answer, "indigo", footer=DISCLAIMER if in_group else None)

    # ---------------------------------------------------------------- helpers

    def _shows_account(self, message: IncomingMessage) -> bool:
        """Whether balances and PnL may appear in the answer to this message.

        Yes in the owner's private chat (nobody else gets this far in one), and
        in the registered group when the owner opted in. Never in other groups.
        """
        if message.is_private:
            return True
        return self._cfg.group_show_pnl and bool(self._cfg.group_chat_id) and message.chat_id == self._cfg.group_chat_id

    def _pick_instrument(self, text: str) -> str:
        instruments = self._cfg.watch_instruments
        upper = text.upper()
        wanted = {inst.split("-")[0] for inst in instruments if inst.split("-")[0] in upper}
        for alias, base in INSTRUMENT_ALIASES.items():
            if alias.upper() in upper:
                wanted.add(base)
        for inst in instruments:
            if inst.split("-")[0] in wanted:
                return inst
        return instruments[0]

    def _allow_group_question(self) -> bool:
        limit = self._cfg.group_daily_question_limit
        today = self._reporter.now_local().strftime("%Y-%m-%d")
        with self._lock:
            count = self._group_questions.get(today, 0)
            if count >= limit:
                return False
            self._group_questions = {today: count + 1}
            return True

    def _already_seen(self, message_id: str) -> bool:
        """Feishu can deliver the same event twice; answer only once."""
        if not message_id:
            return False
        with self._lock:
            if message_id in self._seen:
                return True
            self._seen[message_id] = None
            while len(self._seen) > SEEN_LIMIT:
                self._seen.popitem(last=False)
            return False

    def _try_reply(self, message: IncomingMessage, text: str) -> None:
        try:
            self._feishu.reply_text(message.message_id, text)
        except Exception:
            log.warning("could not send a short reply", exc_info=True)
