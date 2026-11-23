"""Decides what to do with each incoming Feishu message.

Rules that matter:
  * By default the bot takes instructions from the owner only, in the private
    chat and in the group (GROUP_OWNER_ONLY=true). Everyone else is told so.
  * The owner can make someone an owner too ("设为主人 @某人" in the group). Such
    an owner has the same rights, orders included; his previews wait for his own
    code. Only the owner himself makes or unmakes owners, and the private cards
    (alerts, briefs) still go to him alone.
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

import json
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Callable

from . import sources, wishes
from .alerts import PriceAlerts
from .analyst import Analyst, AnalystUnavailable
from .config import Config
from .discipline import day_label
from .feishu_client import FeishuClient, FeishuError, IncomingMessage
from .instruments import ALIASES, InstrumentResolver
from .knowledge import KnowledgeBase
from .market import build_market_context, quote_line
from .okx_client import OkxError, OkxNetworkError
from .reports import DISCLAIMER, status_markdown
from .service import Reporter
from .times import BEIJING, LOS_ANGELES, both, now_text, set_zone, zone_of
from .track import scorecard_markdown
from .watch import PositionWatch, model_name

log = logging.getLogger(__name__)

HELP_TEXT = """我是交易助手，可以做这些事：

- 直接问行情，例如“BTC 现在怎么看”“黄金今晚有什么数据”。OKX 上的品种我都能查实时行情，也会联网查新闻和经济数据
- 发“BTC 报价”，马上回最新价（不经过 AI，几秒就回）
- 发“仓位”，看当前持仓（没持仓时写最近一笔平仓赚了多少）
- 问“现在几点”“1小时K线几点收盘”，马上回洛杉矶和北京时间（不经过 AI）
- 发 /whoami，查看你的飞书 ID

主人专用：
- 下单、平仓、撤单、设止盈止损：私聊里，或者在登记的群里 @我。每条指令我先回预览，你回复确认码才执行。发“交易帮助”看写法
- 发“算仓位 XAU 止损 3768”：按你定的单笔风险算最多开多大，只算数不下单
- 价格提醒：发“跌破 3800 提醒我”“XAU 涨到 3900 提醒我”，到了我马上告诉你（只提醒，不下单）；要按收盘算，发“1小时收盘跌破 3605 提醒我”（15 分钟、30 分钟、1 小时、4 小时）。发“提醒”看已设的，“取消提醒 编号”取消
- 定时撤单：“周五 13:30 撤单 XAU”“30分钟后撤单 XAU”“跌破 3530 撤单 XAU 3588”，设的时候回一次确认码，到时我自己撤
- 发“15 分钟后再看一下”，到时我再看一次行情和仓位，回在这里（最长 4 小时）
- 发“我用北京时间”，跟你说时间时北京时间写在前面
- 定时盯仓：发“每 4 小时盯一下仓位”（几小时你定），持仓期间按时发卡片：离止损止盈多远、浮动盈亏、几句点评，只看不动。发“盯仓”看设置，“停止盯仓”关掉
- 发“记个诉求：……”，把想让我做到的事记下来；我做不到的事也会自动记。发“诉求清单”看记了什么，发“诉求 3 做好了”或“诉求 3 不做了”标掉一条
- 问题前面加“深度”，我会多查几轮、分析得更细
- 发“记住：……”，把交易规则、心得或资料存进资料库，以后回答时我会参考。发“资料”看资料库里有什么
- 发“用量”，看今天和本月 AI 大约花了多少钱
- 发“成绩单”，看我给过的点位事后对了多少（可以加品种和天数，例如“成绩单 XAU 90天”）
- 在群里 @我 发“登记本群”，简报和提醒就会发到那个群
- 私聊我发“重启”，我会重新启动并读取新的配置
- 交易计划：每个交易日（纽约时间 17:00 开始）我在纽约 17:05 写一份计划发出来，写明今天只做多、只做空、两边都可以还是观望，关键价位和几种走势怎么应对。没有计划、和计划方向相反、超过每天笔数的开仓直接拦下；连亏 3 笔或当天亏到上限就停手到下一个交易日。发“今天的计划”看，“出今天的计划”现在就写，“重做计划”一天能重做一次
- 移动止损：持仓浮盈到 1R，止损自动移到保本，之后跟着最好价走，每移一次在群里说。发“移动止损”看状态，“停止移动止损”关掉，“开启移动止损”打开
- 在群里发“@我 设置主人 @某人”（也可以写他的名字），他就和你有一样的权限（提问、看账户、下单，每单照样要他自己回确认码）；“取消主人”拿掉，发“主人”看名单。只有你能设

我每天定时发盘前简报和收盘小结，仓位有变动时会自动提醒。我只做分析、提醒和执行主人确认过的指令，不替任何人做买卖决定。"""

_PLAN_SHOW = re.compile(r"^(?:看一?下|看看)?(?:今天|今日)?的?(?:交易)?计划(?:呢|是什么|是啥)?$|^/plan$")
_PLAN_MAKE = re.compile(r"^(?:请|帮我|给我)?(?:出|做|写|生成)(?:一份|一下|个)?(?:今天|今日)?的?(?:交易)?计划(?:吧)?$|^/plan new$")
_PLAN_REDO = re.compile(r"^(?:请|帮我|给我)?(?:重做|重新做|重写|重新写|更新)(?:一下)?(?:今天|今日)?的?(?:交易)?计划|^/plan redo")

OWNER_ONLY_TEXT = "我只响应主人的指令。仓位变动和简报我会自动发到群里。"
TRADE_OWNER_ONLY_TEXT = "下单、平仓、撤单、改止损这类指令只有主人能发。"
TRADE_GROUP_OFF_TEXT = "群里不执行下单类指令（服务器上 GROUP_TRADING 关着），请在私聊里发给我。"
TRADE_GROUP_HIDDEN_TEXT = "这个群看不到账户和持仓（GROUP_SHOW_PNL 关着），下单预览里有持仓数字，所以请在私聊里发给我。"
TRADE_OTHER_GROUP_TEXT = "这个群没有登记，下单类指令只在私聊和登记过的群里执行。"
# Words that make a message from the owner worth reading as an order, when no fixed pattern matched it.
ORDER_WORDS = (
    "止损", "止盈", "平仓", "平掉", "平一半", "减仓", "加仓", "开仓", "做多", "做空", "开多", "开空", "平多", "平空",
    "撤单", "撤掉", "挂上", "挂单", "挂个", "挂一张", "挂一单", "开单", "清仓", "下单", "按你说的", "照你说的", "按刚才",
    "按计划", "照计划", "按照", "照着", "sl", "tp",
    # "帮我设置": after the AI gave the line to send, he asks for it to be done, not to copy it out himself
    "帮我设", "帮我挂", "帮我改", "帮我调", "帮我下", "帮我开", "帮我平", "帮我撤", "帮我执行", "帮我弄", "帮我做",
    "给我设", "给我挂", "给我做", "给我改", "给我调", "就按这个", "按这个来", "按这个做", "照这个", "就这么办", "执行吧",
)  # fmt: skip  # "按照这个计划开单": the AI's rewrite takes the prices from the earlier answer
# Words of someone still talking a plan over, not asking for it to be done: "把把握提到 60%，要确保单子挂上".
DISCUSS_WORDS = (
    "把握", "确保", "尽可能", "尽量", "讨论", "商量", "思路", "如果", "假如", "要是", "的话", "先别", "先不", "不要下",
    "别下",
)  # fmt: skip
ORDER_WORDS_MAX_CHARS = 80
# A follow-up of a few words on the answer before: the cheaper MODEL_SHORT rewrites or explains it.
SHORT_FOLLOW_UPS = (
    "简单说", "简单点", "说简单点", "简单一点", "简单说说", "说重点", "重点", "然后呢", "还有呢", "继续", "接着说",
    "具体点", "具体一点", "详细点", "展开说说", "为什么", "为啥", "什么意思", "啥意思", "没懂", "没看懂", "再说一遍",
    "翻译一下", "用英文说",
)  # fmt: skip
RECHECK_MAX_MINUTES = 240
_RECHECK = re.compile(
    r"(?:过\s*(半|\d+(?:\.\d+)?)\s*(?:个)?\s*(分钟|小时|钟头)(?:以后|之后|后)?|(半|(?<![\d.])\d+(?:\.\d+)?)\s*(?:个)?\s*(分钟|小时|钟头)\s*(?:以后|之后|后))"
    r"\s*(?:你|再|给我|帮我|跟我|回头)*\s*(?:看|复查|答复|回复|回我|告诉我|说|分析|检查|盯)"
)
RECHECK_QUESTION = "过了一会儿，再看一次现在的行情和仓位：和上一次比有什么变化，原来的判断还成不成立，有没有要留意的。"
_ZONE_PREF = re.compile(r"^(?:我|以后|以后我|我以后)?(?:都|就)?(?:用|按|看|习惯)(北京|洛杉矶|美西)时间(?:吧|就行|就好|为准)?$")
_TIME_QUESTION = re.compile(
    r"^(?:现在|当前|目前)?(?:北京时间|洛杉矶时间|美西时间|北京|洛杉矶)?(?:现在|这会儿)?(?:是)?(?:几点了?|几点钟|什么时间|啥时间|几号)(?:了)?$"
)
_BAR_CLOSE_QUESTION = re.compile(
    r"(?:(15|30)\s*(?:分钟|min|m)|([14])\s*(?:个)?\s*(?:小时|h))\s*(?:k\s*线|线)?\s*(?:几点|什么时候|啥时候|还有多久|多久)\s*(?:收盘|收线|收)|"
    r"(?:(15|30)\s*(?:分钟|min|m)|([14])\s*(?:个)?\s*(?:小时|h))\s*(?:k\s*线|线)?\s*(?:收盘|收线)\s*(?:是|在)?\s*(?:几点|什么时候|啥时候|还有多久)",
    re.I,
)
COMMAND_HISTORY_TURNS = 3  # earlier answers the AI may take an order's prices from

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
OWNER_LIST_WORDS = ("主人", "主人列表", "主人名单", "谁是主人", "有哪些主人", "看主人", "查主人")
# "设为主人", "把他设成主人", "让阿强当主人", "给他主人权限", "取消他的主人", "他不再是主人", "主人 2"
_ADD_OWNER = re.compile(r"(?:设|加|添|当|成为|升|做|给|授权|开通|也是)[^，。,.!！?？]{0,8}主人|主人[^，。,.]{0,3}(?:权限)")
_REMOVE_OWNER = re.compile(r"(?:取消|移除|删除|删掉|去掉|撤销|撤掉|拿掉|收回|解除)[^，。,.!！?？]{0,8}主人|不再是主人|主人[^，。,.]{0,3}(?:取消|去掉|拿掉|撤掉)")
_NOT_A_COMMAND = re.compile(r"[?？吗]|怎么|如何|什么|哪|为啥|为什么|讲讲|介绍|区别|解释")
_OPEN_ID = re.compile(r"ou_[0-9a-z]{16,}")
_AT_TAG = re.compile(r'<at user_id="[^"]*">([^<]*)</at>')
_FILLER_WORDS = re.compile(
    "|".join(
        (
            "设置为", "设置成", "设置", "设为", "设成", "置为", "成为", "加为", "添加", "加入", "给予", "授权", "开通", "权限",
            "主人", "和我", "跟我", "一样", "同样", "相同", "帮我", "帮忙", "麻烦", "一下", "这个人", "那个人", "这位", "那位",
            "群里", "唯一", "取消", "移除", "删除", "删掉", "去掉", "撤销", "撤掉", "拿掉", "收回", "解除", "不再是", "不再",
            "升级", "一位", "也是",
        )
    )
)  # fmt: skip
_FILLER_CHARS = "把将让当也为给他她是的设加请吧啊呀呢"


def _typed_name(text: str) -> str:
    """The name in "设置主人 张小明" or "把阿强设成主人"; "" when only the command words are there."""
    rest = _FILLER_WORDS.sub(" ", _OPEN_ID.sub(" ", text))
    rest = re.sub(r"[\s\d，。,.!！?？~：:“”\"'、@()（）]+", " ", rest)
    parts = [part.strip(_FILLER_CHARS) for part in rest.split()]
    parts = [part for part in parts if len(part) >= 2]
    return max(parts, key=len) if parts else ""
OWNER_NAMES_SETTING = "co_owner_names"  # open_id -> the name Feishu showed when he was made an owner
PEOPLE_SETTING = "group_people"  # open_id -> name, when last seen, what he said: people who @-ed the bot in the group
PEOPLE_DAYS = 30  # someone not seen for this long is no longer offered
PICK_SECONDS = 600  # how long "设为主人 2" picks from the list just shown
MAX_CO_OWNERS = 3
LEARN_PREFIXES = ("记住", "/learn", "投喂")
KNOWLEDGE_WORDS = ("资料", "资料库", "知识库", "/kb")
FORGET_WORDS = ("删除上一条", "删除上一条笔记", "撤销上一条", "/forget")
USAGE_WORDS = ("用量", "费用", "花费", "/usage")
QUOTE_WORDS = ("报价", "现价", "价格", "多少钱", "/price")
RECORD_PREFIXES = ("成绩单", "战绩", "/record")
RECORD_DAYS = 30
RECORD_MAX_DAYS = 365
SEEN_LIMIT = 500
HISTORY_TURNS = 10  # earlier questions of the same chat the AI is shown, for follow-ups
HISTORY_SECONDS = 24 * 3600
HISTORY_KEPT_DAYS = 7  # the journal keeps a chat's questions and answers this long, so a restart forgets nothing
HISTORY_ANSWER_CHARS = 1500
NOTE_LIMIT = 20000

_TITLE_LINE = re.compile(r"\A\s*\**\s*(?:标题|title)\s*\**\s*[：:]\s*(.*?)\s*(?:\n+|\Z)", re.IGNORECASE)
_MARKDOWN = re.compile(r"\*\*|\]\(|`|^\s*(?:[-*+]|\d+[.、]|#+|>)\s", re.MULTILINE)

# What may surround a ticker in a plain request for a price ("BTC 现在的价格").
_QUOTE_FILLER = re.compile(
    r"[a-z0-9/\-\s，,。.?？!！、:：]+|现在|当前|最新|实时|目前|看下|看看|一下|给我|帮我|查|发|和|与|跟|及|的|是|呢|啊"
)


def owners_intent(text: str, others_mentioned: bool = False) -> str:
    """'list', 'add' or 'remove' when the message changes or asks who the owners are; '' otherwise.
    Loose on purpose: "设为主人 @阿强", "把阿强设成主人", "让他当主人 @阿强", "给他主人权限", "取消阿强的主人"
    all count. A question about owners ("主人能做什么？") is left to the AI."""
    bare = text.strip(" 。.!！?？~")
    if bare in OWNER_LIST_WORDS:
        return "list"
    if len(text) > 40 or "主人" not in text:
        return ""
    if _REMOVE_OWNER.search(text):
        return "remove"
    if _NOT_A_COMMAND.search(text) and not others_mentioned:
        return ""  # "可以把他设为主人吗 @阿强" is still a request
    return "add" if _ADD_OWNER.search(text) else ""


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
        alerts: PriceAlerts | None = None,
        watch: PositionWatch | None = None,
        discipline=None,
        trail=None,
    ):
        """`save_setting(name, value)` writes one line of .env, so a group the
        owner registers survives a restart. `restart()` ends the process; the
        start script brings the bot back with the configuration read afresh.
        `usage(start, end)` returns the AI use between two moments. `trade` is
        the trade desk (trading.py), which reads the owner's order commands.
        `journal` holds the track record the owner can ask for. `later(seconds,
        call)` runs a call after a delay and returns what cancels it. `alerts`
        reads the owner's price alerts ("跌破 3800 提醒我"). `watch` reads "每 4 小时盯一下仓位". `discipline`
        keeps the day's plan ("今天的计划"); `trail` moves stops ("停止移动止损")."""
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
        self._alerts = alerts
        self._watch = watch
        self._discipline = discipline
        self._trail = trail
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._group_questions: dict[str, int] = {}  # local date -> count
        self._history: dict[str, deque[tuple[float, str, str]]] = {}  # chat_id -> (time, question, answer)
        self._owner_pick: tuple[float, list[str]] | None = None  # (when, open_ids) of the list "设为主人 2" picks from
        self._dialog_pruned_at = 0.0

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
        is_owner = cfg.is_owner(message.sender_open_id)

        if not message.is_private:
            # In a group, only answer when the bot itself is @-mentioned.
            if self._bot_open_id and self._bot_open_id not in message.mentioned_open_ids:
                return
            if not self._bot_open_id and not message.mentioned_open_ids:
                return
            self._note_people(message)

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

        told = self._time_answer(text, message.sender_open_id)
        if told:
            self._feishu.reply_text(message.message_id, told)
            self._remember_fixed(message.chat_id, text, "", told)
            return

        quoted = self._quote_instruments(text)
        if quoted:
            self._reply_quote(message, text, quoted)
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
        intent = owners_intent(text, bool(self._others_mentioned(message)))
        if intent:
            self._owners_command(message, text, intent)
        elif text.startswith(BIND_WORDS):
            self._bind_group(message)
        elif lowered == "/restart" or text in RESTART_WORDS:
            self._restart_bot(message)  # the group and the private chat do the same for the owner
        elif lowered in KNOWLEDGE_WORDS:
            self._reply_knowledge(message)
        elif lowered in FORGET_WORDS:
            self._forget_note(message)
        elif lowered in USAGE_WORDS:
            self._reply_usage(message)
        elif lowered.startswith(RECORD_PREFIXES) and self._record_request(text) is not None:
            self._reply_record(message, *self._record_request(text))
        elif wishes.is_list_request(text):
            self._reply_wishes(message)
        elif wishes.status_request(text) is not None:
            self._mark_wishes(message, *wishes.status_request(text))
        elif wishes.wish_body(text) is not None:
            self._record_wish(message, wishes.wish_body(text))
        elif _note_body(text) is not None:
            self._learn(message, _note_body(text))
        elif _ZONE_PREF.match(text.strip(" 。.!！~")):
            self._zone_command(message, text)
        elif self._plan_command(message, text) or self._trail_command(message, text):
            pass
        elif not self._watch_command(message, text) and not self._recheck_command(message, text):
            # Before the orders: "跌破 3800 提醒我平仓" asks to be told, never for a close.
            alert = self._alerts.handle(text, message.chat_id) if self._alerts is not None else None
            if alert is None:
                return self._trade_command(message, text)
            self._reply_alert(message, text, alert)
        return True

    def _zone_command(self, message: IncomingMessage, text: str) -> None:
        """ "我用北京时间": times are written to him in that zone first."""
        key = "Asia/Shanghai" if "北京" in text else "America/Los_Angeles"
        word = "北京时间" if key == "Asia/Shanghai" else "洛杉矶时间"
        other = "洛杉矶时间" if key == "Asia/Shanghai" else "北京时间"
        if self._journal is None:
            reply = "现在记不住这个设置（没有数据库）。"
        else:
            set_zone(self._journal, message.sender_open_id, key)
            reply = f"好，以后跟你说时间先写{word}，后面带上{other}。定时撤单不写哪里的时间时，也按{word}算。"
        self._feishu.reply_text(message.message_id, reply)
        self._remember_fixed(message.chat_id, text, "", reply)

    def _zone(self, open_id: str):
        return zone_of(self._journal, open_id) if self._journal is not None else None

    def _time_answer(self, text: str, open_id: str) -> str:
        """ "现在几点", "北京时间几点", "1小时K线几点收盘": answered by the program, never the AI."""
        plain = re.sub(r"[\s?？,，。.!！吗呀啊呢]", "", text)
        if len(plain) > 20:
            return ""
        now = self._clock()
        first = self._zone(open_id)
        if "北京" in plain:
            first = BEIJING
        elif "洛杉矶" in plain or "美西" in plain:
            first = LOS_ANGELES
        if _TIME_QUESTION.match(plain):
            return f"现在是 {now_text(now, self._cfg.tz, first)}。"
        found = _BAR_CLOSE_QUESTION.search(plain)
        if not found:
            return ""
        minutes = found.group(1) or found.group(3)
        hours = found.group(2) or found.group(4)
        length = int(minutes) * 60 if minutes else int(hours) * 3600
        name = f"{minutes} 分钟" if minutes else f"{hours} 小时"
        ends = (int(now) // length + 1) * length  # OKX's candles up to 4 hours start on the hour, in UTC
        left = int(ends - now)
        gap = f"{left // 3600} 小时 {left % 3600 // 60} 分钟" if left >= 3600 else f"{max(1, left // 60)} 分钟"
        return f"这根 {name} K 线 {both(ends, self._cfg.tz, '%H:%M', first=first)}收盘，还有 {gap}。"

    def _recheck_command(self, message: IncomingMessage, text: str) -> bool:
        """ "15 分钟后再看一下": one more look later, answered where he asked. Kept in the alert table, so a
        restart forgets nothing; "取消提醒 编号" drops it."""
        found = _RECHECK.search(text)
        if found is None or self._journal is None:
            return False
        amount = found.group(1) or found.group(3)
        unit = found.group(2) or found.group(4)
        minutes = (0.5 if amount == "半" else float(amount)) * (1 if unit == "分钟" else 60)
        if not 1 <= minutes <= RECHECK_MAX_MINUTES:
            reply = "过一会儿再看一次，最长 4 小时。要持仓期间一直盯着，发“每 1 小时盯一下仓位”。"
            self._feishu.reply_text(message.message_id, reply)
            return True
        question = (text[: found.start()] + text[found.end() :]).strip(" ，,。.!！一下吧")
        now = self._clock()
        due = now + minutes * 60
        data = {"message_id": message.message_id, "chat_type": message.chat_type, "sender": message.sender_open_id,
                "question": question, "minutes": minutes}  # fmt: skip
        inst = self._cfg.watch_instruments[0] if self._cfg.watch_instruments else ""
        job = self._journal.add_alert(
            inst, "at", due, said=question or "再看一次行情和仓位", chat=message.chat_id, ts=now, action="recheck",
            data=json.dumps(data, ensure_ascii=False),
        )  # fmt: skip
        reply = f"好，到 {both(due, self._cfg.tz, '%H:%M', first=self._zone(message.sender_open_id))}，我再看一次，结果回在这里。不用了就发“取消提醒 {job}”。"
        self._feishu.reply_text(message.message_id, reply)
        self._remember_fixed(message.chat_id, text, "", reply)
        return True

    def recheck(self, job: dict) -> None:
        """A second look that is due: the question asked (or a fresh look at the market and the positions),
        answered as a reply to the message that asked for it."""
        try:
            data = json.loads(job.get("data") or "{}")
        except ValueError:
            data = {}
        sender = data.get("sender", "")
        if not data.get("message_id") or not self._cfg.is_owner(sender):
            log.info("a second look was due, but whoever asked for it is no longer an owner")
            return
        message = IncomingMessage(data["message_id"], job.get("chat") or "", data.get("chat_type", "p2p"), sender, "")
        minutes = data.get("minutes") or 0
        asked = data.get("question") or RECHECK_QUESTION
        self._reply_analysis(message, f"（{minutes:g} 分钟前他让你过一会儿再看一次，现在到时间了）{asked}", True)

    def _plan_command(self, message: IncomingMessage, text: str) -> bool:
        """ "今天的计划", "出今天的计划", "重做计划：原因". False when the message is none of them."""
        if self._discipline is None:
            return False
        said = re.sub(r"\s+", " ", text.strip().strip(" 。.!！~?？")).lower()
        redo = _PLAN_REDO.match(said)
        if not redo and not _PLAN_MAKE.match(said) and not _PLAN_SHOW.match(said):
            return False
        plan = self._discipline.plan()
        if not redo and (plan is not None or not _PLAN_MAKE.match(said)):
            if plan is None:
                reply = f"今天（交易日 {day_label(self._discipline.today())}）还没有交易计划，现在不能开新仓。发“出今天的计划”，我现在就写一份。"
                self._feishu.reply_text(message.message_id, reply)
            else:
                amounts = message.is_private or self._reporter.group_shares_account
                card = self._discipline.card(plan, amounts)
                if _PLAN_MAKE.match(said):
                    card += "\n\n（今天的计划已经有了。要重做发“重做计划”，可以带上原因，一天能重做一次。）"
                footer = None if message.is_private else DISCLAIMER
                self._feishu.reply_markdown(message.message_id, self._discipline.title(plan), card, "turquoise", footer=footer)
                reply = "（回了今天的交易计划）"
            self._remember_fixed(message.chat_id, text, "", reply)
            return True
        reason = text.strip()[redo.end():].strip(" ：:，,。") if redo else ""
        done = self._show_working(message)  # "重做计划" with no plan yet writes the first one
        try:
            made, why = self._reporter.write_plan(reason=reason, again=bool(redo and plan is not None))
        finally:
            done()
        if made is None:
            reply = why
        elif not message.is_private:
            reply = f"写好了，见群里的“{self._discipline.title(made)}”卡片。"
        elif message.sender_open_id == self._cfg.owner_open_id:
            reply = "写好了，卡片已经发给你，也发到了群里。"
        else:
            self._feishu.reply_markdown(message.message_id, self._discipline.title(made), self._discipline.card(made, True), "turquoise")
            self._remember_fixed(message.chat_id, text, "", "（回了今天的交易计划）")
            return True
        self._feishu.reply_text(message.message_id, reply)
        self._remember_fixed(message.chat_id, text, "", reply)
        return True

    def _trail_command(self, message: IncomingMessage, text: str) -> bool:
        """ "停止移动止损", "开启移动止损", "移动止损". False when the message is none of them."""
        if self._trail is None:
            return False
        reply = self._trail.handle(text)
        if reply is None:
            return False
        self._feishu.reply_text(message.message_id, reply)
        self._remember_fixed(message.chat_id, text, "", reply)
        return True

    def _watch_command(self, message: IncomingMessage, text: str) -> bool:
        """ "每 4 小时盯一下仓位", "停止盯仓", "盯仓". False when the message is none of them."""
        if self._watch is None:
            return False
        reply = self._watch.handle(text, message.chat_id, message.is_private, self._shows_account(message))
        if reply is None:
            return False
        self._feishu.reply_text(message.message_id, reply.text)
        self._remember_fixed(message.chat_id, text, "", reply.text)
        if reply.then is not None:
            reply.then()  # the first card, right after the reply
        return True

    # --------------------------------------------------------------- owners

    def _others_mentioned(self, message: IncomingMessage) -> list[tuple[str, str]]:
        """(open_id, name) of the people @-mentioned besides the bot and the first owner."""
        names = message.mentioned_names or ("",) * len(message.mentioned_open_ids)
        skip = {self._bot_open_id, self._cfg.owner_open_id, ""}
        return [(open_id, name) for open_id, name in zip(message.mentioned_open_ids, names) if open_id not in skip]

    def _owners_command(self, message: IncomingMessage, text: str, intent: str) -> None:
        """ "设为主人 @某人", "取消主人", "主人". Only the first owner changes who is an owner. One message is
        enough: the person can be @-mentioned, named, picked by number from the list the bot just showed,
        or left out when there is only one person it could be."""
        if intent == "list":
            reply = self._owners_text()
        elif message.sender_open_id != self._cfg.owner_open_id:
            reply = "设主人、取消主人只有最早登记的那位主人能做。"
        else:
            people, reply = self._owner_targets(message, text, intent)
            if people:
                reply = self._remove_owners(people) if intent == "remove" else self._add_owners(people)
        self._feishu.reply_text(message.message_id, reply)
        self._remember_fixed(message.chat_id, text, "", _AT_TAG.sub(lambda m: m.group(1) or "他", reply))

    def _owner_targets(self, message: IncomingMessage, text: str, intent: str) -> tuple[list[tuple[str, str]], str]:
        """Whom "设为主人" or "取消主人" is about, or why that is not clear (and what to send instead)."""
        cfg = self._cfg
        mentioned = self._others_mentioned(message)
        if mentioned:
            return mentioned, ""
        skip = {self._bot_open_id, cfg.owner_open_id, ""}
        typed = [open_id for open_id in dict.fromkeys(_OPEN_ID.findall(text)) if open_id not in skip]
        if typed:
            return [(open_id, "") for open_id in typed], ""
        # "设为主人 2": a number from the list just shown.
        pick = re.findall(r"\d+", text)
        if self._owner_pick is not None and len(pick) == 1 and self._clock() - self._owner_pick[0] <= PICK_SECONDS:
            listed = self._owner_pick[1]
            if 1 <= int(pick[0]) <= len(listed):
                self._owner_pick = None
                return [(listed[int(pick[0]) - 1], "")], ""
        name = _typed_name(text)
        people = self._people()
        if intent == "add" and (name or not any(open_id not in skip for open_id in people)):
            people = self._learn_members(message, people)  # the group's member list, when the app may read it
        names = {**{open_id: item.get("name", "") for open_id, item in people.items()}, **self._owner_names()}
        if intent == "remove":
            pool = list(cfg.co_owner_open_ids)
        else:
            cutoff = self._clock() - PEOPLE_DAYS * 86400
            seen = sorted(people.items(), key=lambda item: -float(item[1].get("last", 0) or 0))
            pool = [
                open_id
                for open_id, item in seen
                if open_id not in skip and open_id not in cfg.co_owner_open_ids
                and (item.get("member") or float(item.get("last", 0) or 0) >= cutoff)
            ]  # fmt: skip
        if name:
            # "设置主人 张小明": a name the bot has seen, written in full or in part.
            named = [
                (open_id, known)
                for open_id, known in names.items()
                if open_id not in skip and known and (known in text or name in known)
            ]
            exact = [(open_id, known) for open_id, known in named if known == name]
            if len(exact) == 1:
                return exact, ""
            if intent == "remove":  # only an owner can be taken off
                named = [item for item in named if item[0] in cfg.co_owner_open_ids] or named
            if len(named) == 1:
                return named, ""
            if named:
                # "设置主人 小明" fits 小明 and 王小明: never all of them, he picks one by number.
                head = f"“{name}”对得上 {len(named)} 个人。"
                return [], head + self._choose(intent, [open_id for open_id, _ in named], people, names)
            # Never guessed: the list @-mentions each person, so Feishu shows his real name to pick by number.
            return [], self._not_found(name, intent, pool, people, names)
        if intent == "remove" and not pool:
            return [], "现在除了你没有别的主人。"
        if not pool:
            return [], self._not_found("", intent, pool, people, names)
        if len(pool) == 1:
            return [(pool[0], names.get(pool[0], ""))], ""
        return [], self._choose(intent, pool, people, names)

    def _choose(self, intent: str, pool: list[str], people: dict[str, dict], names: dict[str, str]) -> str:
        self._owner_pick = (self._clock(), pool)
        word = "取消主人" if intent == "remove" else "设为主人"
        lines = [f"是哪一位？回“@交易助手 {word} 编号”："]
        for number, open_id in enumerate(pool, 1):
            item = people.get(open_id, {})
            said = f"，最近 @我 说“{item['said']}”" if item.get("said") else ""
            lines.append(f"{number}. {self._at(open_id, names)}{said}")
        return "\n".join(lines)

    def _not_found(self, name: str, intent: str, pool: list[str], people: dict[str, dict], names: dict[str, str]) -> str:
        if pool:
            head = f"没找到“{name}”。" if name else ""
            return head + self._choose(intent, pool, people, names)
        who = f"“{name}”" if name else "他"
        if intent == "remove":
            return f"{who}本来就不是主人。发“主人”看名单。"
        return (
            f"我还不认识{who}：群里只有 @过我 的人我才知道是谁。两个办法，任选一个：\n"
            f"1. 发“@交易助手 设置主人 @{name or '他'}”，第二个 @ 要从弹出的成员列表里点他的名字，变成蓝色再发。\n"
            f"2. 让他在群里 @我 随便说一句，你再发“@交易助手 设置主人{(' ' + name) if name else ''}”。"
        )

    def _learn_members(self, message: IncomingMessage, people: dict[str, dict]) -> dict[str, dict]:
        """Adds the registered group's members to the people the bot knows. Feishu refuses unless the app was
        given the permission to read group members; then the bot goes by the people it has seen."""
        chat_id = self._cfg.group_chat_id or ("" if message.is_private else message.chat_id)
        if not chat_id or self._journal is None:
            return people
        try:
            members = self._feishu.chat_members(chat_id)
        except Exception as exc:
            log.info("could not read the group's members (%s); going by the people seen", exc)
            return people
        for open_id, name in members:
            if open_id and open_id != self._bot_open_id:
                item = people.setdefault(open_id, {})
                item["member"] = True
                if name:
                    item["name"] = name
        try:
            self._journal.set_setting(PEOPLE_SETTING, json.dumps(people, ensure_ascii=False))
        except Exception:
            log.exception("could not keep the group's members")
        return people

    def _note_people(self, message: IncomingMessage) -> None:
        """Remembers who @-mentions the bot in the registered group, and the names Feishu shows for the people
        mentioned there, so that "设为主人" can find the person without his name being typed."""
        if message.is_private or not message.chat_id or message.chat_id != self._cfg.group_chat_id or self._journal is None:
            return
        try:
            people = self._people()
            now = self._clock()
            sender = message.sender_open_id
            if sender and sender != self._bot_open_id:
                item = people.setdefault(sender, {})
                item["last"] = now
                said = " ".join(message.text.split())
                item["said"] = said[:12] + ("…" if len(said) > 12 else "")
            names = message.mentioned_names or ("",) * len(message.mentioned_open_ids)
            for open_id, name in zip(message.mentioned_open_ids, names):
                if open_id and open_id != self._bot_open_id and name:
                    people.setdefault(open_id, {})["name"] = name
            cutoff = now - PEOPLE_DAYS * 86400
            kept = {
                open_id: item
                for open_id, item in people.items()
                if float(item.get("last", now) or now) >= cutoff or open_id in self._cfg.co_owner_open_ids
            }
            self._journal.set_setting(PEOPLE_SETTING, json.dumps(kept, ensure_ascii=False))
        except Exception:
            log.exception("could not note who is in the group")

    def _people(self) -> dict[str, dict]:
        if self._journal is None:
            return {}
        try:
            people = json.loads(self._journal.get_setting(PEOPLE_SETTING, "") or "{}")
            return {str(open_id): dict(item) for open_id, item in people.items() if isinstance(item, dict)}
        except (ValueError, TypeError, AttributeError):
            return {}

    def _owner_names(self) -> dict[str, str]:
        if self._journal is None:
            return {}
        try:
            return {key: value for key, value in dict(json.loads(self._journal.get_setting(OWNER_NAMES_SETTING, "") or "{}")).items() if value}
        except (ValueError, TypeError):
            return {}

    def _known_names(self) -> dict[str, str]:
        return {**{open_id: item.get("name", "") for open_id, item in self._people().items()}, **self._owner_names()}

    def _at(self, open_id: str, names: dict[str, str]) -> str:
        """The person as Feishu shows an @: his name, even when the bot never saw it written."""
        return f'<at user_id="{open_id}">{names.get(open_id, "")}</at>'

    def _owners_text(self) -> str:
        names = self._known_names()
        others = self._cfg.co_owner_open_ids
        if not others:
            return "现在只有你一位主人。在群里发“@交易助手 设为主人 @某人”，他就和你有一样的权限。"
        listed = "、".join(self._at(open_id, names) for open_id in others)
        return (
            f"除了你，还有 {len(others)} 位主人：{listed}。他们和你的权限一样：提问、看账户、下单改单撤单，"
            "每单都要他们自己回确认码。提醒和简报的私聊卡片只发给你。"
        )

    def _save_owners(self, open_ids: tuple[str, ...], names: dict[str, str]) -> bool:
        saved = True
        if self._save_setting is not None:
            try:
                self._save_setting("CO_OWNER_OPEN_IDS", ",".join(open_ids))
            except Exception:
                saved = False
                log.exception("could not write CO_OWNER_OPEN_IDS to the configuration file")
        self._cfg.set_co_owners(open_ids)
        if self._journal is not None:
            try:
                self._journal.set_setting(OWNER_NAMES_SETTING, json.dumps(names, ensure_ascii=False))
            except Exception:
                log.exception("could not keep the owners' names")
        log.info("owners: the owner now shares his rights with %d other(s)", len(open_ids))
        return saved

    def _add_owners(self, people: list[tuple[str, str]]) -> str:
        current = list(self._cfg.co_owner_open_ids)
        names = self._owner_names()
        added = []
        for open_id, name in people:
            if name:
                names[open_id] = name
            if open_id not in current:
                current.append(open_id)
                added.append(open_id)
        shown = {**self._known_names(), **names}
        if len(current) > MAX_CO_OWNERS:
            return f"除了你最多再设 {MAX_CO_OWNERS} 位主人。发“主人”看名单，取消几位再设。"
        if not added:
            return f"{'、'.join(self._at(open_id, shown) for open_id, _ in people)} 已经是主人了。"
        saved = self._save_owners(tuple(current), names)
        who = "、".join(self._at(open_id, shown) for open_id in added)
        lines = [
            f"好，{who} 现在也是主人，和你的权限一样：在这里和私聊里 @我 提问、看账户和仓位分析、设提醒和盯仓，"
            "也能下单、改单、撤单。每张单照样先出预览，只认发指令的人自己回的确认码。",
            "提醒和简报的私聊卡片还是只发给你，群里照常都能看到。只有你能设或取消主人，发“取消主人”拿掉。",
        ]
        if not saved:
            lines.append("注意：这次没能写进配置文件，机器人重启后要重新设。")
        return "\n".join(lines)

    def _remove_owners(self, people: list[tuple[str, str]]) -> str:
        names = self._owner_names()
        shown = {**self._known_names(), **{open_id: name for open_id, name in people if name}}
        gone = [open_id for open_id, _ in people if open_id in self._cfg.co_owner_open_ids]
        if not gone:
            return f"{'、'.join(self._at(open_id, shown) for open_id, _ in people)} 本来就不是主人。"
        who = "、".join(self._at(open_id, shown) for open_id in gone)
        remaining = tuple(open_id for open_id in self._cfg.co_owner_open_ids if open_id not in gone)
        for open_id in gone:
            names.pop(open_id, None)
        saved = self._save_owners(remaining, names)
        reply = f"好，{who} 不再是主人了。他在等确认的预览也作废了。" if self._trade is not None else f"好，{who} 不再是主人了。"
        if self._trade is not None:
            for open_id in gone:
                self._trade.forget_preview(open_id)
        return reply + ("" if saved else "\n注意：这次没能写进配置文件，机器人重启后他会恢复成主人，请再取消一次。")

    def _reply_alert(self, message: IncomingMessage, text: str, alert) -> None:
        if alert.title:
            self._feishu.reply_markdown(message.message_id, alert.title, alert.text, "orange")
        else:
            self._feishu.reply_text(message.message_id, alert.text)
        self._remember_fixed(message.chat_id, text, alert.title, alert.text)

    def _record_wish(self, message: IncomingMessage, body: str) -> None:
        if self._journal is None:
            self._feishu.reply_text(message.message_id, "这种运行方式没有记录诉求的地方。")
            return
        if not body:
            self._feishu.reply_text(message.message_id, "把想让我做到的事写在后面，例如：记个诉求：跌破某个价位时提醒我。")
            return
        wish_id = wishes.record(self._journal, body, "owner", message.chat_id, self._clock())
        reply = (
            f"记下了（诉求 #{wish_id}）。会交给开发这个机器人的 Claude，每天看一次，做好了告诉你。发“诉求清单”看全部。"
            if wish_id is not None
            else "这条今天已经记过了。发“诉求清单”看全部。"
        )
        self._feishu.reply_text(message.message_id, reply)
        self._remember_fixed(message.chat_id, f"记个诉求：{body}", "", reply)

    def _mark_wishes(self, message: IncomingMessage, ids: list[int], status: str) -> None:
        if self._journal is None:
            self._feishu.reply_text(message.message_id, "这种运行方式没有记录诉求的地方。")
            return
        marked, missing = wishes.mark(self._journal, ids, status, "你标的")
        reply = wishes.status_reply(marked, missing, status)
        self._feishu.reply_text(message.message_id, reply)
        self._remember_fixed(message.chat_id, message.text.strip(), "", reply)

    def _reply_wishes(self, message: IncomingMessage) -> None:
        if self._journal is None:
            self._feishu.reply_text(message.message_id, "这种运行方式没有记录诉求的地方。")
            return
        body = wishes.list_markdown(self._journal, self._clock(), self._cfg.tz)
        self._feishu.reply_markdown(message.message_id, "诉求清单", body, "purple")
        self._remember_fixed(message.chat_id, message.text.strip(), "诉求清单", body)

    def _note_wish(self, chat_id: str, wish: str, source: str = "auto") -> bool:
        """Records what this bot could not do or read. True when it went into the list."""
        if self._journal is None or not wish:
            return False
        try:
            return wishes.record(self._journal, wish, source, chat_id, self._clock()) is not None
        except Exception:
            log.exception("could not record a wish")
            return False

    def _trade_command(self, message: IncomingMessage, text: str) -> bool:
        """Order commands: the trade desk reads them by fixed patterns, never the AI."""
        if self._trade is None:
            return False
        refused_here = "" if message.is_private else self._group_trade_refusal(message)
        # An order answers in a second or two. Only the rehearsal on the demo account takes long enough for a sign.
        sender = message.sender_open_id
        done = (
            self._show_working(message, slow_text=SLOW_TEXT_SELFTEST)
            if not refused_here and self._trade.takes_long(text, sender)
            else None
        )
        try:
            reply = self._trade.handle(text, refused_here=refused_here, in_group=not message.is_private, sender=sender)
        finally:
            if done is not None:
                done()
        if (reply is None or reply.unclear) and not refused_here and self._sounds_like_an_order(text):
            # "止损减两点": an order the patterns could not read in full. The AI may make it out from the
            # current stop; when it cannot either, the desk's own question stands.
            understood = self._order_in_own_words(text, message.chat_id, in_group=not message.is_private, sender=sender)
            if understood is None and reply is not None and reply.unclear:
                # Neither could read it: worth a look at how he puts his orders.
                self._note_wish(message.chat_id, f"下单的话没看懂：{text}", "unclear")
            reply = understood or reply
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
        if any(word in text for word in DISCUSS_WORDS):
            return False  # "把把握提到 60%，要确保单子挂上" talks the plan over; the AI answers it
        return any(word in lowered for word in ORDER_WORDS)

    def _order_in_own_words(self, text: str, chat_id: str, in_group: bool, sender: str = ""):
        """The AI writes the owner's words out as a fixed-pattern command; the trade desk reads, checks and
        previews it like any other, and nothing is sent without his code. None when they are not an order.
        The last few answers of the chat go along, so "按刚才的情景一挂上" can take its prices from them."""
        earlier = self._recent(chat_id)[-COMMAND_HISTORY_TURNS:]
        command_text = self._analyst.to_command(text, self._trade.positions_text(), earlier)
        if not command_text:
            return None
        return self._trade.handle_understood(text, command_text, in_group=in_group, sender=sender)

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
        where = "" if message.is_private else "在私聊里"
        self._feishu.reply_text(message.message_id, f"好的，我现在重启，大约半分钟后回来。回来时会{where}给你发“交易助手已上线”。")
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
        if self._journal is not None:
            try:
                watched = self._journal.usage_between(today_start.replace(day=1).timestamp(), end, purpose_like="position watch%")
            except Exception:
                log.exception("could not read what the position watch cost")
                watched = {"jobs": 0, "cost_usd": 0.0}
            if watched["jobs"]:
                lines.append(
                    f"其中定时盯仓本月 {watched['jobs']} 次，约 {watched['cost_usd']:.2f} 美元"
                    f"（例行用 {model_name(cfg.model_watch)}，要留意时用 {model_name(cfg.model_daily)}）。"
                )
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
        now = self._clock()
        body = scorecard_markdown(self._journal, now, self._cfg.tz, days, inst_id, self._cfg.track_window_hours)
        trades = [
            row for row in self._journal.reviews_between(now - days * 86400, now + 1) if inst_id is None or row["inst_id"] == inst_id
        ]  # fmt: skip
        by_source = sources.record_block(trades, days)
        if by_source:
            body += "\n\n" + by_source
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
        if len(text) <= 6 and any(word in lowered for word in STATUS_WORDS) and not any(word in text for word in ("挂单", "委托")):
            return "status"  # the card shows positions, not orders: "仓位挂单" goes to the AI, which sees both
        if lowered.startswith(DEEP_PREFIXES):
            return "analysis"
        if self._analyst.enabled:
            intent = self._analyst.route_intent(text)
            # "查看下仓位，有没有必要平仓" is a question, not a request for the card: a fixed reply
            # to a question is the answer that misses it, so anything that asks goes to the AI.
            if intent in ("status", "help") and _asks_something(text):
                return "analysis"
            if intent == "status" and any(word in text for word in ("挂单", "委托")):
                return "analysis"  # "查询我现在的仓位和挂单情况": the card would leave the orders out
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

    def _reply_quote(self, message: IncomingMessage, text: str, inst_ids: list[str]) -> None:
        lines = []
        for inst_id in inst_ids:
            try:
                lines.append(quote_line(self._okx, inst_id))
            except (OkxError, OkxNetworkError) as exc:
                lines.append(f"**{inst_id}** 暂时取不到：{exc}")
        footer = None if message.is_private else DISCLAIMER
        title = "实时报价" if self._cfg.mt5 else "OKX 实时报价"  # MT5: gold from MT5, the rest from OKX
        self._feishu.reply_markdown(message.message_id, title, "\n".join(lines), "wathet", footer=footer)
        self._remember_fixed(message.chat_id, text, title, "\n".join(lines))  # so that "那白银呢" follows on

    def _reply_status(self, message: IncomingMessage) -> None:
        state = self._reporter.current_state()
        if state is None:
            self._feishu.reply_text(message.message_id, f"还没读到 {self._cfg.broker_name} 账户，请稍后再试。")
            return
        with_account = self._shows_account(message)
        if not with_account and not state.positions:
            self._feishu.reply_text(message.message_id, "当前没有持仓。")
            return
        risk = self._reporter.risk(state) if with_account else None
        exposure = self._reporter.exposure_lines(state) if with_account else None
        last_close = self._reporter.last_close_lines() if with_account and not state.positions else None
        body = status_markdown(
            state, self._reporter.day_change(state), private=with_account, risk=risk, exposure=exposure, last_close=last_close
        )
        # The owner's own numbers need no disclaimer; a friend looking at his stops may take them as a signal.
        asked_by_owner = self._cfg.is_owner(message.sender_open_id)
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

            account = self._reporter.account_for_ai() if self._shows_account(message) else None
            history = self._recent(message.chat_id)
            short = not deep and bool(history) and _short_follow_up(text)
            try:
                answer = self._analyst.answer(
                    text, market, account, in_group=in_group, deep=deep, history=history, short=short,
                    notes=self._notes(message, account is not None),
                )
            except AnalystUnavailable as exc:
                self._feishu.reply_text(message.message_id, str(exc))
                return
        finally:
            done()
        log.info("answered in %.0f seconds", self._clock() - started)
        title, body = split_title(answer)
        body, wish = wishes.take_ai_wish(body)
        if wish and is_owner and self._note_wish(message.chat_id, wish):
            body += "\n\n（这件事已记进诉求清单。）"
        self._remember(message.chat_id, text, body)
        footer = DISCLAIMER if in_group else None
        if is_plain(body):
            if in_group:
                self._feishu.reply_markdown(message.message_id, "", body, "indigo", footer=footer)  # no header bar
            else:
                self._feishu.reply_text(message.message_id, body)
            return
        if not title and deep:
            title = "深度分析" + (f" · {'、'.join(named)}" if named else "")
        # Without a title of its own the answer goes without a header: "行情分析" on a reply about R multiples misleads.
        self._feishu.reply_markdown(message.message_id, title, body, "indigo", footer=footer)

    def _notes(self, message: IncomingMessage, with_account: bool) -> list[str]:
        """What the program knows that the AI cannot see: the zone the asker reads, the watch being off."""
        notes = []
        plan = self._reporter.plan_note(with_account) if self._discipline is not None else ""
        if plan:
            notes.append(plan)
        if self._zone(message.sender_open_id) is BEIJING or getattr(self._zone(message.sender_open_id), "key", "") == "Asia/Shanghai":
            notes.append("提问的人习惯用北京时间，写时刻时北京时间写在前面。")
        if with_account and self._watch is not None:
            try:
                state = self._reporter.current_state()
                if state is not None and state.positions and self._watch.watching() is None:
                    notes.append("定时盯仓现在没开。")
            except Exception:
                log.exception("could not tell whether the position watch is on")
        return notes

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

        Yes in an owner's private chat (nobody else gets this far in one), and
        in the registered group when the owner opted in. Never in other groups.
        """
        if message.is_private:
            return True
        return self._cfg.group_show_pnl and bool(self._cfg.group_chat_id) and message.chat_id == self._cfg.group_chat_id

    def _recent(self, chat_id: str) -> list[tuple[str, str]]:
        """The last few questions and answers of this chat, so a follow-up makes sense."""
        cutoff = self._clock() - HISTORY_SECONDS
        if self._journal is not None:
            try:
                return self._journal.recent_dialog(chat_id, cutoff, HISTORY_TURNS)
            except Exception:  # an answer without the earlier ones is better than none
                log.exception("could not read this chat's earlier questions")
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
        now = self._clock()
        with self._lock:
            self._history.setdefault(chat_id, deque(maxlen=HISTORY_TURNS)).append((now, question, answer))
        if self._journal is None:
            return
        try:
            self._journal.record_dialog(chat_id, question, answer, now)
            if now - self._dialog_pruned_at > 86400:
                self._journal.forget_dialog_before(now - HISTORY_KEPT_DAYS * 86400)
                self._dialog_pruned_at = now
        except Exception:
            log.exception("could not keep this question and answer")

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


QUESTION_WORDS = (
    "?", "？", "吗", "要不要", "有没有", "是不是", "能不能", "该不该", "还是", "必要", "怎么样",
    "分析", "怎么看", "建议", "判断",
)  # fmt: skip  # "看下挂单情况" still reaches the AI's rewrite, which leaves a request to look alone

_CODE_IN_TEXT = re.compile(r"(确认\s*)\d{4}")

ASKING_WORDS = (
    "?", "？", "吗", "呢", "要不要", "有没有", "是不是", "能不能", "该不该", "可不可以", "还是",
    "怎么", "为啥", "哪", "风险", "必要", "确认", "判断", "分析", "建议", "看法",
)


def _short_follow_up(text: str) -> bool:
    """ "简单说", "为什么？": a few words that only make sense against the answer before."""
    plain = re.sub(r"[\s?？,，。.!！~吗呀啊呢吧]", "", text.lower())
    return 0 < len(plain) <= 8 and plain in SHORT_FOLLOW_UPS


def _asks_something(text: str) -> bool:
    """Whether a message asks a question or for a judgment, beyond showing the numbers."""
    return len(text) > 6 and any(word in text for word in ASKING_WORDS)
