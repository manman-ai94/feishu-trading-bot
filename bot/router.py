"""Decides what to do with each incoming Feishu message.

Rules that matter:
  * By default the bot takes instructions from the owner only, in the private
    chat and in the group (GROUP_OWNER_ONLY=true). Everyone else is told so.
  * Account details (balance, PnL) are shown in the owner's private chat, and
    in the registered group when GROUP_SHOW_PNL=true (the default). There the
    AI is also given the account, so it can analyse the positions.
  * No other group ever sees account details, whoever asks.
  * Private chats from anyone other than the owner are not served.
  * The knowledge base, the usage report, registering a group and restarting
    are the owner's alone.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Callable

from .analyst import Analyst, AnalystUnavailable
from .config import Config
from .feishu_client import FeishuClient, FeishuError, IncomingMessage
from .instruments import ALIASES, InstrumentResolver
from .knowledge import KnowledgeBase
from .market import build_market_context, quote_line
from .okx_client import OkxError, OkxNetworkError
from .reports import DISCLAIMER, account_text, status_markdown
from .service import Reporter

log = logging.getLogger(__name__)

HELP_TEXT = """我是交易助手，可以做这些事：

- 直接问行情，例如“BTC 现在怎么看”“黄金今晚有什么数据”。OKX 上的品种我都能查实时行情，也会联网查新闻和经济数据
- 发“BTC 报价”，马上回最新价（不经过 AI，几秒就回）
- 发“仓位”，看当前持仓
- 发 /whoami，查看你的飞书 ID

主人专用：
- 问题前面加“深度”，我会多查几轮、分析得更细
- 发“记住：……”，把交易规则、心得或资料存进资料库，以后回答时我会参考。发“资料”看资料库里有什么
- 发“用量”，看今天和本月 AI 大约花了多少钱
- 在群里 @我 发“登记本群”，简报和提醒就会发到那个群
- 私聊我发“重启”，我会重新启动并读取新的配置

我每天定时发盘前简报和收盘小结，仓位有变动时会自动提醒。我只做分析和提醒，不替任何人做买卖决定。"""

OWNER_ONLY_TEXT = "我只响应主人的指令。仓位变动和简报我会自动发到群里。"

STATUS_WORDS = ("仓位", "持仓", "账户", "余额", "盈亏", "/status", "status", "position")
HELP_WORDS = ("/help", "help", "帮助", "你能做什么", "怎么用")
DEEP_PREFIXES = ("深度", "/deep")
BIND_WORDS = ("/bindgroup", "登记本群", "登记这个群", "绑定本群", "绑定这个群")
RESTART_WORDS = ("/restart", "重启", "重新启动")
LEARN_PREFIXES = ("记住", "/learn", "投喂")
KNOWLEDGE_WORDS = ("资料", "资料库", "知识库", "/kb")
FORGET_WORDS = ("删除上一条", "删除上一条笔记", "撤销上一条", "/forget")
USAGE_WORDS = ("用量", "费用", "花费", "/usage")
QUOTE_WORDS = ("报价", "现价", "价格", "多少钱", "/price")
SEEN_LIMIT = 500
HISTORY_TURNS = 3  # earlier questions of the same chat the AI is shown, for follow-ups
HISTORY_SECONDS = 30 * 60
HISTORY_ANSWER_CHARS = 1500
NOTE_LIMIT = 20000

# What may surround a ticker in a plain request for a price ("BTC 现在的价格").
_QUOTE_FILLER = re.compile(
    r"[a-z0-9/\-\s，,。.?？!！、:：]+|现在|当前|最新|实时|目前|看下|看看|一下|给我|帮我|查|发|和|与|跟|及|的|是|呢|啊"
)


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
        save_setting: Callable[[str, str], None] | None = None,
        restart: Callable[[], None] | None = None,
        resolver: InstrumentResolver | None = None,
        knowledge: KnowledgeBase | None = None,
        usage: Callable[[float, float], dict] | None = None,
    ):
        """`save_setting(name, value)` writes one line of .env, so a group the
        owner registers survives a restart. `restart()` ends the process; the
        start script brings the bot back with the configuration read afresh.
        `usage(start, end)` returns the AI use between two moments."""
        self._cfg = cfg
        self._save_setting = save_setting
        self._restart = restart
        self._feishu = feishu
        self._okx = okx
        self._analyst = analyst
        self._reporter = reporter
        self._bot_open_id = bot_open_id
        self._clock = clock
        self._resolver = resolver or InstrumentResolver(okx, clock)
        self._knowledge = knowledge
        self._usage = usage
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._group_questions: dict[str, int] = {}  # local date -> count
        self._history: dict[str, deque[tuple[float, str, str]]] = {}  # chat_id -> (time, question, answer)

    # ------------------------------------------------------------------ entry

    def handle(self, message: IncomingMessage) -> None:
        if self._already_seen(message.message_id):
            return
        # Who and where, never what: the text of a message stays out of the log.
        log.info(
            "message from %s in %s chat %s (%d characters)",
            message.sender_open_id,
            "private" if message.is_private else "group",
            message.chat_id,
            len(message.text),
        )
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
                "私聊里拿到的 open_id 填到 OWNER_OPEN_ID。群不用填 chat_id，主人在群里 @我 发“登记本群”就行。",
            )
            return

        if is_owner and self._owner_command(message, text, lowered):
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

        quoted = self._quote_instruments(text)
        if quoted:
            self._reply_quote(message, quoted)
            return

        intent = self._intent(text)
        if intent == "help":
            self._feishu.reply_text(message.message_id, HELP_TEXT)
        elif intent == "status":
            self._reply_status(message)
        else:
            self._reply_analysis(message, text, is_owner)

    # ------------------------------------------------------- owner's commands

    def _owner_command(self, message: IncomingMessage, text: str, lowered: str) -> bool:
        """Handles the commands only the owner has. False when the message is not one of them."""
        if text.startswith(BIND_WORDS):
            self._bind_group(message)
        elif lowered == "/restart" or text in RESTART_WORDS:
            if message.is_private:
                self._restart_bot(message)
            else:
                self._feishu.reply_text(message.message_id, "重启请在私聊里发给我。")
        elif lowered in KNOWLEDGE_WORDS:
            self._reply_knowledge(message)
        elif lowered in FORGET_WORDS:
            self._forget_note(message)
        elif lowered in USAGE_WORDS:
            self._reply_usage(message)
        else:
            note = _note_body(text)
            if note is None:
                return False
            self._learn(message, note)
        return True

    def _bind_group(self, message: IncomingMessage) -> None:
        """The owner says "登记本群" in a group: that group becomes the one that
        gets the briefs and alerts. Nobody has to copy a chat_id into .env."""
        if message.is_private:
            self._feishu.reply_text(message.message_id, "请到要登记的那个群里 @我，再发“登记本群”。")
            return
        previous = self._cfg.group_chat_id
        if message.chat_id == previous:
            self._feishu.reply_text(message.message_id, "这个群已经登记过了，简报和提醒都会发到这里。")
            return
        saved = True
        if self._save_setting is not None:
            try:
                self._save_setting("GROUP_CHAT_ID", message.chat_id)
            except Exception:
                saved = False
                log.exception("could not write GROUP_CHAT_ID to the configuration file")
        self._cfg.register_group(message.chat_id)
        log.info("the owner registered group %s (was %s)", message.chat_id, previous or "none")
        lines = ["已登记本群。以后简报和仓位提醒会发到这里，你在这里 @我 提问我也会回答。"]
        if previous:
            lines.append("之前登记的那个群不再接收。")
        if not saved:
            lines.append("注意：这次没能写进配置文件，机器人重启后需要重新登记。")
        self._feishu.reply_text(message.message_id, "\n".join(lines))

    def _restart_bot(self, message: IncomingMessage) -> None:
        if self._restart is None:
            self._feishu.reply_text(message.message_id, "这种运行方式不支持在飞书里重启，请到服务器上重启。")
            return
        self._feishu.reply_text(message.message_id, "好的，我现在重启，大约半分钟后回来。回来时会给你发“交易助手已上线”。")
        log.info("the owner asked for a restart")
        self._restart()

    def _learn(self, message: IncomingMessage, note: str) -> None:
        if self._knowledge is None:
            self._feishu.reply_text(message.message_id, "这种运行方式没有启用资料库。")
            return
        if not note:
            self._feishu.reply_text(message.message_id, "把要我记住的内容写在后面，例如：记住：止损设好以后不往远处移动。")
            return
        if len(note) > NOTE_LIMIT:
            self._feishu.reply_text(message.message_id, f"这条太长了（超过 {NOTE_LIMIT} 字）。请分几条发，或者把文件交给 Claude 放进服务器的资料库文件夹。")
            return
        count = self._knowledge.add_note(note, self._reporter.now_local())
        log.info("the owner added a note to the knowledge base (%d characters, %d notes now)", len(note), count)
        self._feishu.reply_text(
            message.message_id, f"记住了，这是资料库里的第 {count} 条笔记。以后回答相关问题时我会参考。记错了可以发“删除上一条”。"
        )

    def _forget_note(self, message: IncomingMessage) -> None:
        removed = self._knowledge.remove_last_note() if self._knowledge is not None else None
        if removed is None:
            self._feishu.reply_text(message.message_id, "资料库里没有可以删除的笔记。")
            return
        preview = removed if len(removed) <= 60 else removed[:60] + "……"
        self._feishu.reply_text(message.message_id, f"已删除最新的一条笔记：{preview}")

    def _reply_knowledge(self, message: IncomingMessage) -> None:
        files = self._knowledge.summary() if self._knowledge is not None else []
        if not files:
            body = "资料库现在是空的。"
        else:
            lines = [f"- {name}（约 {size:,} 字）" for name, size in files]
            body = f"资料库里有 {len(files)} 个文件，其中你发给我的笔记 {self._knowledge.note_count()} 条：\n" + "\n".join(lines)
        body += (
            "\n\n往里加内容：发“记住：……”，一条一个要点或一段资料。"
            "整本的书或很长的文档，交给 Claude 放进服务器的资料库文件夹。"
        )
        self._feishu.reply_markdown(message.message_id, "资料库", body, "turquoise")

    def _reply_usage(self, message: IncomingMessage) -> None:
        if self._usage is None:
            self._feishu.reply_text(message.message_id, "这种运行方式没有记录 AI 用量。")
            return
        cfg = self._cfg
        today_start = self._reporter.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        end = self._clock() + 1
        today = self._usage(today_start.timestamp(), end)
        month = self._usage(today_start.replace(day=1).timestamp(), end)

        def line(label: str, totals: dict) -> str:
            return f"**{label}**：AI 分析 {totals['jobs']} 次，联网搜索 {totals['web_searches']} 次，约 {totals['cost_usd']:.2f} 美元"

        lines = [line("今天", today), line("本月", month)]
        if cfg.ai_monthly_budget_usd is not None:
            lines.append(f"本月预算 {cfg.ai_monthly_budget_usd:g} 美元，用到这个数以后我会自动改用最省的档位。")
        lines.append(f"当前设置：平时用 {cfg.model_daily} 的 {cfg.effort_daily} 档，“深度”用 {cfg.model_deep} 的 {cfg.effort_deep} 档。")
        lines.append("金额按公开价格估算，实际以 Claude Console 的账单为准。")
        self._feishu.reply_markdown(message.message_id, "AI 用量", "\n".join(lines), "grey")

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

    def _quote_instruments(self, text: str) -> list[str]:
        """The instruments of a plain request for a price, e.g. "BTC 报价". Empty for anything more."""
        lowered = text.lower()
        if len(text) > 40 or not any(word in lowered for word in QUOTE_WORDS):
            return []
        rest = lowered
        for word in QUOTE_WORDS + tuple(ALIASES):
            rest = rest.replace(word, " ")
        if _QUOTE_FILLER.sub("", rest):
            return []  # there is a real question in there: let the AI answer it
        return self._resolver.find_in_text(text, limit=4)

    def _reply_quote(self, message: IncomingMessage, inst_ids: list[str]) -> None:
        lines = []
        for inst_id in inst_ids:
            try:
                lines.append(quote_line(self._okx, inst_id))
            except (OkxError, OkxNetworkError) as exc:
                lines.append(f"**{inst_id}** 暂时取不到：{exc}")
        footer = None if message.is_private else DISCLAIMER
        self._feishu.reply_markdown(message.message_id, "OKX 实时报价", "\n".join(lines), "wathet", footer=footer)

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

        self._try_reply(message, "收到，这次做深度分析，可能要两三分钟。" if deep else "收到，正在查行情和资料，请稍等一两分钟。")
        named = self._resolver.find_in_text(text, limit=2)
        market = self._market_text(named)

        account = None
        if self._shows_account(message):
            state = self._reporter.current_state()
            if state is not None:
                account = account_text(state, self._reporter.day_change(state))
        try:
            answer = self._analyst.answer(
                text, market, account, in_group=in_group, deep=deep, history=self._recent(message.chat_id)
            )
        except AnalystUnavailable as exc:
            self._feishu.reply_text(message.message_id, str(exc))
            return
        self._remember(message.chat_id, text, answer)
        title = ("深度分析" if deep else "行情分析") + (f" · {'、'.join(named)}" if named else "")
        self._feishu.reply_markdown(message.message_id, title, answer, "indigo", footer=DISCLAIMER if in_group else None)

    def _market_text(self, named: list[str]) -> str:
        """Market data fetched before the AI starts, so it need not ask for the obvious.

        The instruments the message names; when it names none, the first one on
        the watch list. Anything else the AI fetches itself with its tools.
        """
        blocks = []
        if not named:
            blocks.append("（消息里没有点名品种。下面是交易员平时主要关注的品种，和问题无关时不用管它。）")
        for inst_id in named or list(self._cfg.watch_instruments[:1]):
            context = build_market_context(self._okx, inst_id)
            if context.usable:
                blocks.append(context.to_text())
            else:
                blocks.append(f"品种：{inst_id}\n（OKX 行情暂时取不到：{'；'.join(context.errors) or '未知原因'}）")
        return "\n\n".join(blocks)

    # ---------------------------------------------------------------- helpers

    def _shows_account(self, message: IncomingMessage) -> bool:
        """Whether balances and PnL may appear in the answer to this message.

        Yes in the owner's private chat (nobody else gets this far in one), and
        in the registered group when the owner opted in. Never in other groups.
        """
        if message.is_private:
            return True
        return self._cfg.group_show_pnl and bool(self._cfg.group_chat_id) and message.chat_id == self._cfg.group_chat_id

    def _recent(self, chat_id: str) -> list[tuple[str, str]]:
        """The last few questions and answers of this chat, so a follow-up makes sense."""
        cutoff = self._clock() - HISTORY_SECONDS
        with self._lock:
            return [(question, answer) for when, question, answer in self._history.get(chat_id, ()) if when >= cutoff]

    def _remember(self, chat_id: str, question: str, answer: str) -> None:
        if len(answer) > HISTORY_ANSWER_CHARS:
            answer = answer[:HISTORY_ANSWER_CHARS] + "……（后面略）"
        with self._lock:
            self._history.setdefault(chat_id, deque(maxlen=HISTORY_TURNS)).append((self._clock(), question, answer))

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


def _note_body(text: str) -> str | None:
    """The note in "记住：..." (may be empty), or None when the message is not that command."""
    lowered = text.lower()
    for prefix in LEARN_PREFIXES:
        if lowered.startswith(prefix):
            rest = text[len(prefix) :]
            if rest and rest[0] not in " ：:，,\n\t":
                return None  # "记住了吗" is a question, not a note
            return rest.strip(" ：:，,\n\t")
    return None
