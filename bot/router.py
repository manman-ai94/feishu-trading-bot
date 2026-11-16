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
  * Order commands are the owner's alone too. They are carried out in his
    private chat, and in the registered group when GROUP_TRADING=true (the
    default) and that group sees the account. Everyone else's are turned away
    before the trade desk sees them, so a code shown in the group works only
    when he types it.

How an answer looks follows the answer. While the AI works, the question
carries an emoji and nothing is said; words come only when the wait is long.
A few sentences are sent as a plain message, the way a person would reply,
and only a longer answer becomes a card, under the title the AI gave it.
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
from .track import scorecard_markdown

log = logging.getLogger(__name__)

HELP_TEXT = """我是交易助手，可以做这些事：

- 直接问行情，例如“BTC 现在怎么看”“黄金今晚有什么数据”。OKX 上的品种我都能查实时行情，也会联网查新闻和经济数据
- 发“BTC 报价”，马上回最新价（不经过 AI，几秒就回）
- 发“仓位”，看当前持仓
- 发 /whoami，查看你的飞书 ID

主人专用：
- 下单、平仓、撤单、设止盈止损：私聊里，或者在登记的群里 @我。每条指令我先回预览，你回复确认码才执行。发“交易帮助”看写法
- 发“算仓位 XAU 止损 3768”：按你定的单笔风险算最多开多大，只算数不下单
- 问题前面加“深度”，我会多查几轮、分析得更细
- 发“记住：……”，把交易规则、心得或资料存进资料库，以后回答时我会参考。发“资料”看资料库里有什么
- 发“用量”，看今天和本月 AI 大约花了多少钱
- 发“成绩单”，看我给过的点位事后对了多少（可以加品种和天数，例如“成绩单 XAU 90天”）
- 在群里 @我 发“登记本群”，简报和提醒就会发到那个群
- 私聊我发“重启”，我会重新启动并读取新的配置

我每天定时发盘前简报和收盘小结，仓位有变动时会自动提醒。我只做分析、提醒和执行主人确认过的指令，不替任何人做买卖决定。"""

OWNER_ONLY_TEXT = "我只响应主人的指令。仓位变动和简报我会自动发到群里。"
TRADE_OWNER_ONLY_TEXT = "下单、平仓、撤单、改止损这类指令只有主人能发。"
TRADE_GROUP_OFF_TEXT = "群里不执行下单类指令（服务器上 GROUP_TRADING 关着），请在私聊里发给我。"
TRADE_GROUP_HIDDEN_TEXT = "这个群看不到账户和持仓（GROUP_SHOW_PNL 关着），下单预览里有持仓数字，所以请在私聊里发给我。"
TRADE_OTHER_GROUP_TEXT = "这个群没有登记，下单类指令只在私聊和登记过的群里执行。"
# Words that make a message from the owner worth reading as an order, when no fixed pattern matched it.
ORDER_WORDS = (
    "止损", "止盈", "平仓", "平掉", "平一半", "减仓", "加仓", "开仓", "做多", "做空", "开多", "开空", "平多", "平空",
    "撤单", "撤掉", "挂单", "清仓", "sl", "tp",
)  # fmt: skip
ORDER_WORDS_MAX_CHARS = 80

HELLO_TEXT = "在。直接问就行，例如“黄金现在怎么看”；发“帮助”可以看我能做什么。"
HELLO_WORDS = ("你好", "您好", "hi", "hello", "在吗", "在不在", "谢谢", "多谢", "辛苦了", "早", "早上好", "晚上好")

# While an answer is being written the question carries this emoji (taken off
# again when the answer is sent), so nothing has to be said about waiting.
WORKING_EMOJI = "Typing"
SLOW_SECONDS = 90  # with the emoji showing, words are sent only when an answer takes longer than this
SLOW_SECONDS_NO_SIGN = 30  # when Feishu would not take the emoji, the wait is acknowledged sooner
SLOW_TEXT = "这个问题要查的东西多一些，还要一会儿。"
SLOW_TEXT_DEEP = "深度分析还在做，再等一两分钟。"
SLOW_TEXT_SELFTEST = "自测还在跑，每一步都要等 OKX 回话，再等一会儿。"
PLAIN_ANSWER_CHARS = 200  # an answer up to this long, without markdown, is sent as an ordinary message
PLAIN_ANSWER_LINES = 4
TITLE_CHARS = 30

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
RECORD_PREFIXES = ("成绩单", "战绩", "/record")
RECORD_DAYS = 30
RECORD_MAX_DAYS = 365
SEEN_LIMIT = 500
HISTORY_TURNS = 5  # earlier questions of the same chat the AI is shown, for follow-ups
HISTORY_SECONDS = 2 * 3600
HISTORY_ANSWER_CHARS = 1500
NOTE_LIMIT = 20000

_TITLE_LINE = re.compile(r"\A\s*\**\s*(?:标题|title)\s*\**\s*[：:]\s*(.*?)\s*(?:\n+|\Z)", re.IGNORECASE)
_MARKDOWN = re.compile(r"\*\*|\]\(|`|^\s*(?:[-*+]|\d+[.、]|#+|>)\s", re.MULTILINE)

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
        trade=None,
        journal=None,
        later: Callable[[float, Callable[[], None]], Callable[[], None]] | None = None,
    ):
        """`save_setting(name, value)` writes one line of .env, so a group the
        owner registers survives a restart. `restart()` ends the process; the
        start script brings the bot back with the configuration read afresh.
        `usage(start, end)` returns the AI use between two moments. `trade` is
        the trade desk (trading.py), which reads the owner's order commands.
        `journal` holds the track record the owner can ask for. `later(seconds,
        call)` runs a call after a delay and returns what cancels it."""
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
        self._trade = trade
        self._journal = journal
        self._later = later or _call_later
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
            # An order from anyone but the owner is turned away here, before the AI ever sees it.
            if self._trade is not None and self._trade.is_command(text):
                self._feishu.reply_text(message.message_id, TRADE_OWNER_ONLY_TEXT)
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
            self._remember_fixed(message.chat_id, text, "", "（回了“帮助”里的功能清单）")
        elif intent == "hello":
            self._reply_hello(message, text)
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
        elif lowered.startswith(RECORD_PREFIXES) and self._record_request(text) is not None:
            self._reply_record(message, *self._record_request(text))
        else:
            note = _note_body(text)
            if note is None:
                return self._trade_command(message, text)
            self._learn(message, note)
        return True

    def _trade_command(self, message: IncomingMessage, text: str) -> bool:
        """Order commands: the trade desk reads them by fixed patterns, never the AI."""
        if self._trade is None:
            return False
        refused_here = "" if message.is_private else self._group_trade_refusal(message)
        # An order answers in a second or two. Only the rehearsal on the demo account takes long enough for a sign.
        done = self._show_working(message, slow_text=SLOW_TEXT_SELFTEST) if not refused_here and self._trade.takes_long(text) else None
        try:
            reply = self._trade.handle(text, refused_here=refused_here, in_group=not message.is_private)
        finally:
            if done is not None:
                done()
        if (reply is None or reply.unclear) and not refused_here and self._sounds_like_an_order(text):
            # "止损减两点": an order the patterns could not read in full. The AI may make it out from the
            # current stop; when it cannot either, the desk's own question stands.
            reply = self._order_in_own_words(text, in_group=not message.is_private) or reply
        if reply is None:
            return False
        if reply.title:
            self._feishu.reply_markdown(message.message_id, reply.title, reply.text, reply.template)
        else:
            self._feishu.reply_text(message.message_id, reply.text)
        self._remember_fixed(message.chat_id, text, reply.title, reply.text)
        return True

    def _sounds_like_an_order(self, text: str) -> bool:
        """Worth asking the AI whether the owner's words are an order the fixed patterns missed."""
        if not self._analyst.enabled or len(text) > ORDER_WORDS_MAX_CHARS or text.rstrip().endswith(("?", "？")):
            return False
        lowered = text.lower()
        if any(word in text for word in QUESTION_WORDS):
            return False  # "有没有必要平仓，还是再等等" asks for a view, it does not ask for a close
        return any(word in lowered for word in ORDER_WORDS)

    def _order_in_own_words(self, text: str, in_group: bool):
        """The AI writes the owner's words out as a fixed-pattern command; the trade desk reads, checks and
        previews it like any other, and nothing is sent without his code. None when they are not an order."""
        command_text = self._analyst.to_command(text, self._trade.positions_text())
        if not command_text:
            return None
        return self._trade.handle_understood(text, command_text, in_group=in_group)

    def _group_trade_refusal(self, message: IncomingMessage) -> str:
        """Why the owner's order command is not carried out in this group; "" when it is.

        Only the owner gets this far (the router turns everyone else away first),
        and his confirmation code works only from him, wherever it is typed.
        """
        cfg = self._cfg
        if not cfg.group_chat_id or message.chat_id != cfg.group_chat_id:
            return TRADE_OTHER_GROUP_TEXT
        if not cfg.group_trading:
            return TRADE_GROUP_OFF_TEXT
        if not cfg.group_show_pnl:
            return TRADE_GROUP_HIDDEN_TEXT
        return ""

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

    def _record_request(self, text: str) -> tuple[int, str | None] | None:
        """Reads "成绩单", "成绩单 XAU", "成绩单 90天". None when the rest is something else, e.g. a question."""
        rest = text
        for prefix in RECORD_PREFIXES:
            if rest.lower().startswith(prefix):
                rest = rest[len(prefix) :]
                break
        days = RECORD_DAYS
        inst_id = None
        for word in rest.replace("，", " ").replace(",", " ").split():
            number = re.fullmatch(r"(\d{1,3})\s*(天|日|d)?", word.lower())
            if number:
                days = max(1, min(RECORD_MAX_DAYS, int(number.group(1))))
                continue
            found = self._resolver.find_in_text(word, limit=1)
            if not found:
                return None
            inst_id = found[0]
        return days, inst_id

    def _reply_record(self, message: IncomingMessage, days: int, inst_id: str | None) -> None:
        if self._journal is None or not self._cfg.track_record:
            self._feishu.reply_text(message.message_id, "成绩单没有打开。在服务器的 .env 里把 TRACK_RECORD 设成 true 再重启。")
            return
        body = scorecard_markdown(self._journal, self._clock(), self._cfg.tz, days, inst_id, self._cfg.track_window_hours)
        self._feishu.reply_markdown(message.message_id, "成绩单", body, "green")

    # ---------------------------------------------------------------- intents

    def _intent(self, text: str) -> str:
        lowered = text.lower()
        if any(lowered == word or lowered.startswith(word + " ") for word in HELP_WORDS):
            return "help"
        if lowered.strip(" 。.!！~～，,") in HELLO_WORDS:
            return "hello"
        # Short messages like "仓位" or "我的持仓" are a status request; longer
        # ones ("我的仓位风险大吗") go to the AI router below.
        if len(text) <= 6 and any(word in lowered for word in STATUS_WORDS):
            return "status"
        if lowered.startswith(DEEP_PREFIXES):
            return "analysis"
        if self._analyst.enabled:
            intent = self._analyst.route_intent(text)
            # "查看下仓位，有没有必要平仓" is a question, not a request for the card: a fixed reply
            # to a question is the answer that misses it, so anything that asks goes to the AI.
            if intent in ("status", "help") and _asks_something(text):
                return "analysis"
            return intent
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
        risk = self._reporter.risk(state) if with_account else None
        body = status_markdown(state, self._reporter.day_change(state), private=with_account, risk=risk)
        # The owner's own numbers need no disclaimer; a friend looking at his stops may take them as a signal.
        asked_by_owner = bool(self._cfg.owner_open_id) and message.sender_open_id == self._cfg.owner_open_id
        footer = None if message.is_private or asked_by_owner else DISCLAIMER
        self._feishu.reply_markdown(message.message_id, "当前持仓", body, "blue", footer=footer)
        self._remember_fixed(message.chat_id, message.text.strip(), "当前持仓", body)

    def _reply_hello(self, message: IncomingMessage, text: str) -> None:
        """A greeting or a thank-you gets one line back, not the list of everything the bot can do."""
        reply = (self._analyst.small_talk(text) if self._analyst.enabled else "").strip() or HELLO_TEXT
        self._feishu.reply_text(message.message_id, reply)
        self._remember(message.chat_id, text, reply)

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

        started = self._clock()
        done = self._show_working(message, deep)
        try:
            named = self._resolver.find_in_text(text, limit=2)
            market = self._market_text(named)

            account = None
            if self._shows_account(message):
                state = self._reporter.current_state()
                if state is not None:
                    account = account_text(state, self._reporter.day_change(state), self._reporter.risk(state), self._cfg.risk_per_trade_pct)
            try:
                answer = self._analyst.answer(
                    text, market, account, in_group=in_group, deep=deep, history=self._recent(message.chat_id)
                )
            except AnalystUnavailable as exc:
                self._feishu.reply_text(message.message_id, str(exc))
                return
        finally:
            done()
        log.info("answered in %.0f seconds", self._clock() - started)
        title, body = split_title(answer)
        self._remember(message.chat_id, text, body)
        footer = DISCLAIMER if in_group else None
        if is_plain(body):
            if in_group:
                self._feishu.reply_markdown(message.message_id, "", body, "indigo", footer=footer)  # no header bar
            else:
                self._feishu.reply_text(message.message_id, body)
            return
        if not title:
            title = ("深度分析" if deep else "行情分析") + (f" · {'、'.join(named)}" if named else "")
        self._feishu.reply_markdown(message.message_id, title, body, "indigo", footer=footer)

    def _show_working(self, message: IncomingMessage, deep: bool = False, slow_text: str = "") -> Callable[[], None]:
        """Shows that an answer is on its way, and returns what to call when it is there.

        The sign is an emoji on the question. Words follow only when the answer
        takes long, or sooner when Feishu would not take the emoji.
        """
        reaction_id = ""
        try:
            reaction_id = self._feishu.add_reaction(message.message_id, WORKING_EMOJI)
        except Exception as exc:
            log.info("the working emoji could not be put on the question: %s", exc)
        cancel = self._later(
            SLOW_SECONDS if reaction_id else SLOW_SECONDS_NO_SIGN,
            lambda: self._try_reply(message, slow_text or (SLOW_TEXT_DEEP if deep else SLOW_TEXT)),
        )

        def done() -> None:
            cancel()
            if reaction_id:
                try:
                    self._feishu.remove_reaction(message.message_id, reaction_id)
                except Exception as exc:
                    log.info("the working emoji could not be taken off: %s", exc)

        return done

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

    def _remember_fixed(self, chat_id: str, question: str, title: str, text: str) -> None:
        """A reply the program wrote by itself (a preview, an executed order, the positions card), kept with
        the AI's answers so that a follow-up to it makes sense: "我要 @ 哪个机器人" after a refusal. The
        confirmation code is masked; it only ever counts when the owner types it."""
        body = _CODE_IN_TEXT.sub(r"\1****", text.replace("**", ""))
        self._remember(chat_id, question, f"[程序自动回复的卡片：{title}]\n{body}" if title else body)

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


def split_title(answer: str) -> tuple[str, str]:
    """The title the AI put on the first line of an answer ("标题：…"), and the answer without it."""
    found = _TITLE_LINE.match(answer)
    if found is None:
        return "", answer.strip()
    body = answer[found.end() :].strip()
    title = found.group(1).strip(" *#")
    if not body:
        return "", title or answer.strip()  # a title and nothing else: that line is the answer
    if len(title) > TITLE_CHARS:
        title = title[: TITLE_CHARS - 1] + "…"
    return title, body


def is_plain(answer: str) -> bool:
    """A few sentences without markdown: these read better as an ordinary message than as a card."""
    return (
        len(answer) <= PLAIN_ANSWER_CHARS
        and answer.count("\n") < PLAIN_ANSWER_LINES
        and _MARKDOWN.search(answer) is None
    )


def _call_later(seconds: float, call: Callable[[], None]) -> Callable[[], None]:
    timer = threading.Timer(seconds, call)
    timer.daemon = True
    timer.start()
    return timer.cancel


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


QUESTION_WORDS = ("?", "？", "吗", "要不要", "有没有", "是不是", "能不能", "该不该", "还是", "必要", "怎么样")

_CODE_IN_TEXT = re.compile(r"(确认\s*)\d{4}")

ASKING_WORDS = (
    "?", "？", "吗", "呢", "要不要", "有没有", "是不是", "能不能", "该不该", "可不可以", "还是",
    "怎么", "为啥", "哪", "风险", "必要", "确认", "判断", "分析", "建议", "看法",
)


def _asks_something(text: str) -> bool:
    """Whether a message asks a question or for a judgment, beyond showing the numbers."""
    return len(text) > 6 and any(word in text for word in ASKING_WORDS)
