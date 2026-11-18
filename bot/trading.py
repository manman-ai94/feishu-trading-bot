"""The trade desk: the owner's order commands, one confirmation at a time.

How an order happens:
  1. The owner sends a command in the private chat, e.g. "做多 XAU 100张 止损 3860".
  2. The desk works out exactly what would be sent to OKX, checks it against
     the limits in .env, and answers with a preview and a four-digit code.
  3. The owner answers "确认 1234". The desk checks everything once more with
     fresh prices and positions, and only then sends the request.

Rules that matter:
  * Nothing reaches OKX without step 3. A code works once, for two minutes,
    and only for the preview it came with.
  * Commands are read by fixed patterns, never by the AI. Anything the desk
    does not fully understand is refused, not guessed.
  * The limits (instruments, order size, position size, orders per day) live
    in .env on the server. No message can change them.
  * "停止交易" stops new positions at once. Closing, cancelling and moving
    stops stay possible, because they only ever reduce risk.
  * The circuit breaker does the same on its own after losing stop-outs or
    liquidations (TRADE_BREAKER_STOPS within TRADE_BREAKER_HOURS, read from
    OKX's own records). Only "解除熔断" plus a code lifts it.
  * When the connection breaks while an order is on its way, the desk looks
    the order up. It never sends the same order twice on its own.
  * Only USDT/USDC-settled perpetual contracts. No leverage changes, no spot,
    no withdrawals or transfers (the client has no such calls at all).
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

from .closes import Hit, hit_line, read_closed, read_triggers, stop_hits
from .exposure import Exposure, Piece, measure
from .config import STOP_TRIGGERS, TRADE_ACTIONS, Config
from .instruments import ALIASES, InstrumentResolver
from .journal import Journal
from .models import AccountConfig, AccountState, InstrumentSpec, OrderInfo, Position
from .okx_client import OkxError, OkxNetworkError
from .reports import currency_lines
from . import sources

log = logging.getLogger(__name__)

MODE_LABELS = {"off": "已关闭", "demo": "模拟盘", "live": "实盘"}
PAUSED_SETTING = "trading_paused"
BREAKER_RESET_SETTING = "breaker_reset_at"  # stop-outs before this moment no longer count
BREAKER_TOLD_SETTING = "breaker_told_at"  # the newest stop-out the owner has been told the breaker tripped on
MAX_COMMAND_CHARS = 80
MAX_WRONG_CODES = 3
FAR_LIMIT_PCT = Decimal(30)  # a resting limit price further than this from the market is taken for a typo
CONFIRM_RISK_SLACK = Decimal("1.1")  # TRADE_MAX_RISK_PCT at the confirmation: a tenth over, for the price moving meanwhile
ACCOUNT_CONFIG_TTL = 600.0
ORDER_NOT_FOUND_CODES = {"51603"}  # OKX: the order does not exist
CAP_SHARE = Decimal("0.99")  # a risk-sized order cut to the limits keeps a hundredth spare, for the price moving before the code
PRICE_MATCH = Decimal("0.00001")  # "撤单 3788" finds the order resting within this share of 3788

# A message with any of these is talk about trading, not an order.
QUESTION_MARKS = (
    "?", "怎么", "吗", "如何", "能不能", "该不该", "要不要", "是否", "为什么", "呢", "哪", "什么", "多少", "还是",
    "建议", "分析", "觉得", "合适", "好不好", "行不行", "可以", "应该", "会不会",
)  # fmt: skip

NUM = r"\d+(?:\.\d+)?"
_CONFIRM = re.compile(r"(?:/confirm|确认|确定)\s*(\d{1,8})?")
# A word that turns a reply to the card into a "no": "取消 4812", "4812 不要执行", "算了 4812", "4812 先等等".
# Such a reply voids the preview; it is never taken as the confirmation, whatever code is in it.
_REFUSAL = re.compile(r"取消|不|别|算了|等|停|撤|作废|放弃|慢|错|(?<![a-z])(?:no|not|don'?t|wait|cancel|stop|hold)(?![a-z])")
REFUSED_NOTE = "refused"
_POLITE = re.compile(r"^(?:请|麻烦|帮我|给我|帮忙)+\s*")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+){0,2}")
_NOT_INSTRUMENTS = {"u", "usdt", "usdc", "usd", "sl", "tp", "oz", "all"}
_JOIN = (
    r"(?:价格|价|设置在|设置为|设置到|设在|设为|设到|修改为|修改到|修改成|改到|改为|改成|调整到|调整为|调整成|调到|调为|调成"
    r"|上移到|下移到|上移|下移|移动到|移到|移至|挪到|放到|放在|挂在|挂到|在|到|为|:|=)?"
)
_SL = re.compile(rf"(?:止损|(?<![a-z0-9])sl)\s*{_JOIN}\s*({NUM})", re.I)
_TP = re.compile(rf"(?:止盈|(?<![a-z0-9])tp)\s*{_JOIN}\s*({NUM})", re.I)
_PX = re.compile(rf"(?:限价|价格|价位|挂在|挂到|挂|在|@)\s*[:=]?\s*({NUM})")
_BARE_NUMBER = re.compile(NUM)
_THOUSANDS = re.compile(r"\d,\d{3}(?!\d)")
_FILLER = re.compile(r"的仓位|仓位|的单子|单子|开仓|数量|现价|永续|合约|的|仓|单")
_STOP_VERB = r"(?:设置|修改|移动|调整|设|改|移|调|挪)(?:一下)?"
_STOP_START = re.compile(rf"^(?:{_STOP_VERB})?\s*(?=止损|止盈)|^/(?=sl|tp)", re.I)
_STOP_VERB_START = re.compile(rf"^{_STOP_VERB}\s*")
# "把我的止损调到 3800": the words people put in front of a stop-loss when they talk.
_STOP_LEAD = re.compile(r"^(?:(?:请|麻烦|帮我|给我|帮忙|把|将|我的)\s*)+")
_BA = re.compile(r"(?:^|\s)(?:把|将)")  # "把止损……" is a request; "我的止损被扫了" is not
_REMOVE_WORDS = ("撤掉", "撤销", "撤了", "取消", "去掉", "删掉", "删除")
_FRACTION_WORDS = (("一半", Decimal("0.5")), ("半仓", Decimal("0.5")), ("三分之一", Decimal(1) / 3), ("四分之一", Decimal("0.25")))
_EVERYTHING_WORDS = ("全部", "所有", "全平")

# The kinds of command TRADE_ACTIONS opens and closes, in the owner's words.
ACTION_NAMES = {
    "stops": "设置和取消止盈止损",
    "close": "平仓和减仓",
    "cancel": "撤单",
    "limit": "限价挂单",
    "open": "市价开仓和加仓",
}

ABORT_WORDS = ("取消", "不要了", "算了", "取消下单", "/abort")
PAUSE_WORDS = ("停止交易", "暂停交易", "停止下单", "暂停下单", "/stoptrading")
RESUME_WORDS = ("恢复交易", "恢复下单", "/resumetrading")
UNBREAK_WORDS = ("解除熔断", "恢复开仓", "/unbreak")
SETTINGS_WORDS = ("交易设置", "交易状态", "下单设置", "下单状态", "/trading")
HELP_WORDS = ("交易帮助", "下单帮助", "怎么下单", "下单指令", "/tradehelp")
ORDERS_WORDS = ("查挂单", "当前挂单", "当前委托", "挂单", "委托", "/orders")
SELFTEST_WORDS = ("模拟盘自测", "模拟自测", "下单自测", "自测下单", "/selftest")
SIZE_WORDS = ("算仓位", "仓位计算", "计算仓位", "算手数", "/size")
_ENTRY = re.compile(rf"(?:入场价|入场|进场价|进场|开仓价|开在|价格)\s*{_JOIN}\s*({NUM})")
SIZE_HELP = "算仓位要写品种和止损价，入场价不写就按现价：算仓位 XAU 止损 3768，或 算仓位 XAU 入场 3873 止损 3768。"

_TYPE_PREFIXES = (("市价", "market"), ("限价", "limit"), ("挂单", "limit"))
_OPEN_VERBS = (
    ("/long", "long", ""), ("/short", "short", ""),
    ("做多", "long", ""), ("开多", "long", ""), ("买入", "long", ""), ("买多", "long", ""), ("买涨", "long", ""),
    ("做空", "short", ""), ("开空", "short", ""), ("卖空", "short", ""), ("买跌", "short", ""),
    ("挂多", "long", "limit"), ("挂空", "short", "limit"),
    ("加仓", "same", ""),
)  # fmt: skip
_BREAK_UP = ("涨破", "突破", "升破", "上破", "站上", "涨过", "涨到")
_BREAK_DOWN = ("跌破", "跌穿", "下破", "跌到", "跌过")
_BREAKOUT = re.compile(
    rf"^(?:等|当|如果)?\s*(?:价格)?\s*({'|'.join(_BREAK_UP + _BREAK_DOWN)})\s*({NUM})\s*(?:点|美金|美元)?\s*"
    r"(?:就|再|后|之后|以后|的时候|时|的话)?\s*[,，]?\s*(?:我)?\s*(?:就|再)?\s*"
    r"(限价|市价|挂单)?\s*(做多|开多|买入|买多|做空|开空|卖空)(.*)$"
)
MAX_TIERS = 5
_LADDER = re.compile(r"^分\s*(\d)?\s*档\s*")
_RANGE = re.compile(rf"^({NUM})\s*(?:到|至|-|~)\s*({NUM})$")
_CLOSE_ALL_VERBS = ("全部平仓", "平掉全部", "平掉所有", "全平", "清仓", "/closeall")
_CLOSE_VERBS = (("平多", "long"), ("平空", "short"), ("平仓", ""), ("平掉", ""), ("/close", ""))
_CANCEL_VERBS = ("撤单", "撤销挂单", "取消挂单", "撤掉挂单", "撤销委托", "取消委托", "/cancel")
_CLEAR_STOP_VERBS = (
    ("取消止盈止损", True, True), ("取消止损止盈", True, True), ("撤销止盈止损", True, True), ("撤销止损止盈", True, True),
    ("取消止损", True, False), ("撤销止损", True, False), ("取消止盈", False, True), ("撤销止盈", False, True),
)  # fmt: skip

STOP_PRICE_MISSING = "要写价格，例如：止损 XAU 3860、把止损调到 3860，或 止损 XAU 3860 止盈 3950。"

HELP_TEXT = """**开仓**（只做 USDT 本位永续合约）
- 市价：“做多 XAU 100张”“做空 BTC 0.01个”“做多 XAU 500U”
- 限价挂单：“限价做多 XAU 100张 3850”“挂单做空 BTC 0.01个 68000”
- 随单带止损止盈：在后面加“止损 3860 止盈 3950”
- 数量带单位：“张”是合约张数，“个”是币的数量（黄金就是盎司），“U”是仓位的名义价值，不是保证金。黄金也可以写“手”，1 手 = 1 XAU
- 只写止损不写数量：“做多 XAU 止损 3816”“限价做多 XAU 3850 止损 3820”，我按你定的单笔风险算数量，预览里写出怎么算的；算出来超过单笔上限或持仓上限时，自动减到上限以内
- “加仓 XAU 100张”：按现有仓位的方向再开。市价加仓带止损时，整个仓位合成一张止损（先挂新的再撤旧的）
- 突破单：“涨破 3900 做多 XAU 止损 3870”“跌破 3800 做空 XAU 止损 3830”，价格到了按市价进场，没到一直挂着
- 分档挂单：“分档做多 XAU 3850 3830 3810 止损 3790”“分3档做多 XAU 3850到3810 止损 3790”，最多 5 档，每档一样多、各带同一个止损；不写数量时，几档全部成交再打到止损合计亏你定的单笔风险
- 账户总风险：所有止损都打到合计最多亏多少（TRADE_MAX_TOTAL_RISK_PCT），每张开仓预览都写；超了就不执行，只写止损的自动减到放得下

**平仓**（都按市价）
- “平仓 XAU”平掉整个仓位；“平仓 XAU 一半”“平仓 XAU 30%”“减仓 XAU 100张”平一部分
- “全部平仓”平掉所有仓位

**挂单**
- “挂单”看当前的挂单和止盈止损
- “撤单 XAU”“撤单 全部”“撤单 订单号”，或按价格撤：“撤单 XAU 3788”“撤单 3788”；突破单按触发价撤：“撤单 XAU 3900”
- 改挂单的价格：先撤单，再重新挂

**止盈止损**（对整个仓位）
- “止损 XAU 3860”“止盈 XAU 3950”“止损 XAU 3860 止盈 3950”
- 仓位上有好几张止损（几次加仓各带一张）时，发“止损 XAU 3860”就合成一张管整个仓位
- 也可以像平时说话：“把止损调到 3860”“把 XAU 的止盈改成 3950”
- “取消止损 XAU”“取消止盈 XAU”“取消止盈止损 XAU”“把止损撤掉”

**算仓位**（只算数，不下单）
- “算仓位 XAU 止损 3768”按现价算；“算仓位 XAU 入场 3873 止损 3768”
- 按你定的单笔风险（RISK_PER_TRADE_PCT，“交易设置”里看现在是多少）算最多开多大

**在群里**
- 你本人在登记过的群里 @我 发这些指令，和私聊一样执行；回确认码也要 @我。群里其他人发的不执行

**安全**
- 每条指令我先回一张预览，你回复确认码才执行（“确认 4812”“4812”“确认4812执行”都行），回复“取消”作废；回复里只要带“取消、不要、别、先等等”这类话，即使有确认码也按作废处理；过期也作废
- 不是标准写法也可以，例如“止损减两点”“把 3808 的止损挪到 3806”：我会先写出我理解成哪条指令，再出预览
- “停止交易”：立刻不再接受开仓（平仓、撤单、改止损照常）。“恢复交易”解除
- 熔断：24 小时内亏损的止损或强平满 2 次，我自动不再接受开仓（平仓、撤单、改止损照常），不动任何仓位。“解除熔断”再回确认码恢复
- “交易设置”：看当前是模拟盘还是实盘、各项限额、这把 key 的权限
- “模拟盘自测”：只在模拟盘。我用最小的数量把开仓、止盈止损、减仓、挂单、撤单、平仓各跑一遍，告诉你 OKX 每一步收没收
- 限额和品种名单只能在服务器的 .env 里改，飞书里改不了。杠杆倍数请在 OKX 里设"""


# --------------------------------------------------------------------------- commands


def new_confirmation_code() -> str:
    """A random four-digit code that never looks like a gold price (3000-4999), so a price typed
    while a preview waits cannot be taken for the confirmation by chance."""
    n = secrets.randbelow(7000)
    return str(n + 1000 if n < 2000 else n + 3000)


@dataclass(frozen=True)
class Command:
    """What the owner asked for, as far as the words alone say it."""

    kind: str  # open | close | cancel | stop | orders | pause | resume | unbreak | settings | help | selftest | confirm | abort | unclear
    inst_id: str = ""
    direction: str = ""  # open: long | short | same (加仓). close: long | short | "" (whichever there is). cancel: buy | sell | ""
    ord_type: str = ""  # market | limit | trigger (a breakout: opens at market when the price reaches px)
    size: Decimal | None = None
    unit: str = ""  # contracts | coins | usd | fraction | risk (opening: sized from the stop and RISK_PER_TRADE_PCT)
    px: Decimal | None = None  # open: the limit price. cancel: the price of the resting order to cancel
    sl: Decimal | None = None
    tp: Decimal | None = None
    clear_sl: bool = False
    clear_tp: bool = False
    ord_id: str = ""
    everything: bool = False  # every position / every open order
    reduce: bool = False  # 减仓: a part of the position, never all of it by default
    code: str = ""
    note: str = ""  # unclear: what to tell the owner
    tiers: tuple[Decimal, ...] = ()  # open: the prices of a ladder of limit orders (分档挂单), as written
    source: str = ""  # where the order came from (sources.SOURCE_NAMES); set by the desk, not by the parser


class _Unclear(Exception):
    """The message is an order, but not one the desk can read with certainty."""


def normalise(text: str) -> str:
    """Full-width digits, letters and punctuation become plain ones; separators become spaces."""
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"[,;、。]+", " ", text)
    return re.sub(r"\s+", " ", text)


def _take(pattern: re.Pattern, text: str, what: str) -> tuple[Decimal | None, str]:
    """Cuts the one number `pattern` finds out of `text`. Two of them is one too many."""
    matches = list(pattern.finditer(text))
    if not matches:
        return None, text
    if len(matches) > 1:
        raise _Unclear(f"{what}写了不止一个，我不知道该用哪个。")
    match = matches[0]
    value = Decimal(match.group(1))
    if value <= 0:
        raise _Unclear(f"{what}要大于 0。")
    return value, text[: match.start()] + " " + text[match.end() :]


class CommandParser:
    """Reads order commands by fixed patterns. `parse` returns None for anything that is not one."""

    def __init__(self, resolver, instruments: Callable[[], tuple[str, ...]]):
        self._resolver = resolver
        self._instruments = instruments  # the instruments orders are allowed in

    def parse(self, raw: str) -> Command | None:
        text = normalise(raw)
        if not text:
            return None
        fixed = self._fixed(text)
        if fixed is not None:
            return fixed
        lowered = text.lower()
        if len(text) > MAX_COMMAND_CHARS or any(mark in lowered for mark in QUESTION_MARKS):
            return None
        try:
            command = self._order(_POLITE.sub("", text))
        except _Unclear as exc:
            command = Command("unclear", note=str(exc))
        if command is not None and _THOUSANDS.search(unicodedata.normalize("NFKC", raw)):
            # "65,000" would be read as two numbers. Better to ask than to pick one.
            return Command("unclear", note="数字里请不要带逗号，例如写 65000，不要写 65,000。")
        return command

    # ------------------------------------------------------ whole-message words

    def _fixed(self, text: str) -> Command | None:
        lowered = text.lower()
        confirm = _CONFIRM.fullmatch(lowered)
        if confirm:
            return Command("confirm", code=confirm.group(1) or "")
        for words, kind in (
            (ABORT_WORDS, "abort"), (PAUSE_WORDS, "pause"), (RESUME_WORDS, "resume"), (UNBREAK_WORDS, "unbreak"),
            (SETTINGS_WORDS, "settings"), (HELP_WORDS, "help"), (SELFTEST_WORDS, "selftest"),
        ):  # fmt: skip
            if lowered in words:
                return Command(kind)
        for word in SELFTEST_WORDS:
            if lowered.startswith(word + " "):
                rest, ids = self._without_instruments(text[len(word) :])
                if len(ids) == 1 and not rest.strip():
                    return Command("selftest", inst_id=ids[0])
        for word in ORDERS_WORDS:
            if lowered == word:
                return Command("orders")
            if lowered.startswith(word + " "):
                rest, ids = self._without_instruments(text[len(word) :])
                if len(ids) == 1 and not rest.strip():
                    return Command("orders", inst_id=ids[0])
        return None

    # ------------------------------------------------------------------ orders

    def _order(self, text: str) -> Command | None:
        lowered = text.lower()
        for word in SIZE_WORDS:
            if lowered.startswith(word):
                return self._sizing(text[len(word) :])
        ord_type = ""
        ladder: int | None = None  # "分3档……": a ladder of that many tiers (0: as many as the prices given)
        found = _LADDER.match(text)
        if found:
            ladder, text = int(found.group(1) or 0), text[found.end() :]
            ord_type = "limit"
        for word, kind in _TYPE_PREFIXES:
            if text.startswith(word):
                ord_type, text = kind, text[len(word) :].lstrip()
                break
        if ladder is not None and ord_type != "limit":
            raise _Unclear("分档挂单都是限价单，不能写“市价”。")
        lowered = text.lower()
        if not ord_type:
            for word, clear_sl, clear_tp in _CLEAR_STOP_VERBS:
                if text.startswith(word):
                    return self._clear_stops(text[len(word) :], clear_sl, clear_tp)
            for word in _CANCEL_VERBS:
                if lowered.startswith(word):
                    return self._cancel(text[len(word) :])
            stops = self._stops_said(text)
            if stops is not None:
                return stops
        breakout = _BREAKOUT.match(text) if not ord_type else None
        if breakout:
            return self._breakout(breakout)
        for word, direction, forced in _OPEN_VERBS:
            if lowered.startswith(word):
                return self._open(direction, forced or ord_type, text[len(word) :], ladder)
        if ladder is not None:
            raise _Unclear("分档挂单请这样写：分档做多 XAU 3850 3830 3810 止损 3790，或 分3档做多 XAU 3850到3810 止损 3790。")
        for word in _CLOSE_ALL_VERBS:
            if lowered.startswith(word):
                return self._close(text[len(word) :], "", ord_type, everything=True)
        for word, direction in _CLOSE_VERBS:
            if lowered.startswith(word):
                return self._close(text[len(word) :], direction, ord_type)
        if text.startswith("减仓"):
            return self._close(text[2:], "", ord_type, reduce=True)
        if text.startswith("卖出") and re.search(r"\d", text):
            raise _Unclear("“卖出”可能是平掉多单，也可能是开空单。平仓请发“平仓 品种”，开空请发“做空 品种 数量”。")
        if text.startswith("反手"):
            raise _Unclear("反手请分两步：先发“平仓 品种”，平完再发开反方向的单。")
        return None

    def _open(self, direction: str, ord_type: str, rest: str, ladder: int | None = None) -> Command | None:
        _, ids = self._without_instruments(rest)
        if not re.search(r"\d", rest):
            # "做多情绪很浓" is talk. "做多 BTC" is an order with the size left out.
            if ids and len(rest) <= 24:
                raise _Unclear(
                    "要写数量和单位，例如：做多 XAU 100张、做多 BTC 0.01个，或 做多 XAU 500U。"
                    "也可以只写止损，例如：做多 XAU 止损 3816，我按你定的单笔风险算数量。"
                )
            return None
        inst_id = self._single(ids, required=True)
        base = inst_id.split("-")[0]
        work = rest
        sl, work = _take(_SL, work, "止损价")
        tp, work = _take(_TP, work, "止盈价")
        px, work = _take(_PX, work, "价格")
        size, unit, work = self._take_size(work, base)
        work, _ = self._without_instruments(work)
        if "市价" in work:
            if ord_type == "limit":
                raise _Unclear("这条指令里既有“市价”又有“限价”，请只写一种。")
            ord_type, work = "market", work.replace("市价", " ")
        for word in ("限价", "挂单"):
            if word in work:
                if ord_type == "market":
                    raise _Unclear("这条指令里既有“市价”又有“限价”，请只写一种。")
                ord_type, work = "limit", work.replace(word, " ")
        leftover = _FILLER.sub(" ", work).strip()
        tiers = self._tiers(leftover, px, ladder) if ord_type == "limit" or ladder is not None or px is not None or _RANGE.match(leftover) else ()
        if tiers:
            px, leftover = None, ""
            if sl is None:
                raise _Unclear("分档挂单要带止损，例如：分档做多 XAU 3850 3830 3810 止损 3790。")
        if leftover and ord_type == "limit" and px is None and _BARE_NUMBER.fullmatch(leftover):
            px, leftover = Decimal(leftover), ""
        if leftover:
            if _BARE_NUMBER.fullmatch(leftover):
                raise _Unclear(
                    f"多出来一个数字 {leftover}，我不确定它是数量、价格还是止损。"
                    "数量请带单位（张、个、U），限价请写“限价 价格”，止损请写“止损 价格”。"
                )
            raise _Unclear(f"这部分我没看懂：“{leftover}”。")
        if size is None:
            if sl is None:
                raise _Unclear("要写数量和单位，例如：100张、0.01个，或 500U。只写止损不写数量的话，我按你定的单笔风险算数量。")
            unit = "risk"  # as much as the stop allows under the owner's per-trade risk
        if unit == "fraction":
            raise _Unclear("开仓的数量请用 张、个 或 U，不能用百分比。")
        if px is not None and px <= 0:
            raise _Unclear("价格要大于 0。")
        if ord_type == "market" and px is not None:
            raise _Unclear("市价单不用写价格。想挂在某个价位，请用“限价做多 品种 数量 价格”。")
        if tiers:
            return Command("open", inst_id=inst_id, direction=direction, ord_type="limit", size=size, unit=unit, sl=sl, tp=tp, tiers=tiers)
        if ladder is not None:
            raise _Unclear("分档挂单要写两个以上的价格（最多 5 档），例如：分档做多 XAU 3850 3830 3810 止损 3790。")
        if ord_type == "limit" and px is None:
            raise _Unclear("限价单要写价格，例如：限价做多 XAU 100张 3850。")
        return Command(
            "open", inst_id=inst_id, direction=direction, ord_type=ord_type or ("limit" if px is not None else "market"),
            size=size, unit=unit, px=px, sl=sl, tp=tp,
        )  # fmt: skip

    @staticmethod
    def _tiers(leftover: str, px: Decimal | None, ladder: int | None) -> tuple[Decimal, ...]:
        """The prices of a ladder in what is left of a limit order: "3850 3830 3810", or "3850到3810" with 分3档."""
        found = _RANGE.match(leftover)
        if found:
            if not ladder or ladder < 2:
                raise _Unclear("写了价格区间，要说分几档，例如：分3档做多 XAU 3850到3810 止损 3790。")
            if ladder > MAX_TIERS:
                raise _Unclear(f"最多分 {MAX_TIERS} 档。")
            top, bottom = Decimal(found.group(1)), Decimal(found.group(2))
            step = (bottom - top) / (ladder - 1)
            return tuple(top + step * i for i in range(ladder))
        parts = leftover.split()
        if px is not None:
            parts = [str(px), *parts] if parts else []
        if len(parts) < 2 or not all(_BARE_NUMBER.fullmatch(part) for part in parts):
            return ()
        if len(parts) > MAX_TIERS:
            raise _Unclear(f"最多分 {MAX_TIERS} 档，你写了 {len(parts)} 个价格。")
        prices = tuple(Decimal(part) for part in parts)
        if len(set(prices)) < len(prices) or any(price <= 0 for price in prices):
            raise _Unclear("分档的价格要各不相同，而且大于 0。")
        if ladder and ladder != len(prices):
            raise _Unclear(f"说了分 {ladder} 档，写的是 {len(prices)} 个价格。")
        return prices

    def _breakout(self, found: re.Match) -> Command | None:
        """"涨破 3900 做多 XAU 止损 3870": a trigger order (计划委托) that opens at market once the price gets there."""
        word, trigger, kind, verb, rest = found.group(1), Decimal(found.group(2)), found.group(3), found.group(4), found.group(5)
        direction = "long" if verb in ("做多", "开多", "买入", "买多") else "short"
        up = word in _BREAK_UP
        if up != (direction == "long"):
            plain = "限价做多" if direction == "long" else "限价做空"
            raise _Unclear(
                f"“{word} {_plain(trigger)} {verb}”不是突破单：突破单是涨破某价做多、跌破某价做空。"
                f"想在 {_plain(trigger)} 挂单等价格回来，请用“{plain} 品种 {_plain(trigger)} 止损 价格”。"
            )
        if kind or any(w in rest for w in ("限价", "市价", "挂单")):
            raise _Unclear("突破单到了触发价就按市价成交，后面不用再写“限价”“市价”。")
        command = self._open(direction, "market", rest)
        if command is None or command.kind != "open":
            return command
        return replace(command, ord_type="trigger", px=trigger)

    def _close(
        self, rest: str, direction: str, ord_type: str, everything: bool = False, reduce: bool = False
    ) -> Command | None:
        if ord_type == "limit":
            raise _Unclear("平仓目前只按市价执行。想在某个价位平仓，可以用“止盈 品种 价格”。")
        _, ids = self._without_instruments(rest)
        inst_id = self._single(ids, required=False)
        size, unit, work = self._take_size(rest, inst_id.split("-")[0] if inst_id else "", fraction_ok=True)
        work, _ = self._without_instruments(work)
        for word, value in _FRACTION_WORDS:
            if word in work:
                if size is not None:
                    raise _Unclear("平多少写了不止一个，我不知道该用哪个。")
                size, unit, work = value, "fraction", work.replace(word, " ", 1)
        for word in _EVERYTHING_WORDS:
            if word in work:
                everything, work = True, work.replace(word, " ")
        leftover = _FILLER.sub(" ", work.replace("市价", " ")).strip()
        if leftover:
            if len(leftover) > 12:
                return None  # a sentence that merely starts with "平仓"
            raise _Unclear(f"这部分我没看懂：“{leftover}”。")
        if unit == "usd":
            raise _Unclear("平仓的数量请用 张、个 或 百分比，例如：平仓 XAU 一半、减仓 XAU 100张。")
        if size is not None and everything:
            raise _Unclear("“全部”和具体数量只能写一个。")
        if reduce and size is None:
            raise _Unclear("减仓要写减多少，例如：减仓 XAU 一半，或 减仓 XAU 100张。")
        return Command(
            "close", inst_id=inst_id, direction=direction, size=size, unit=unit,
            everything=everything and not inst_id and not direction and size is None, reduce=reduce,
        )  # fmt: skip

    def _cancel(self, rest: str) -> Command | None:
        rest = rest.strip()
        if not rest:
            return Command("cancel")
        if rest.lower() in ("全部", "所有", "all", "全部挂单", "所有挂单"):
            return Command("cancel", everything=True)
        if re.fullmatch(r"\d{6,}", rest):  # an order id; the plan also tries it as a price ("撤单 100500" on BTC)
            return Command("cancel", ord_id=rest)
        work, ids = self._without_instruments(rest)
        if len(ids) > 1:
            raise _Unclear("一条撤单只能写一个品种。")
        side = ""
        for word, value in (("买单", "buy"), ("卖单", "sell")):
            if word in work:
                side, work = value, work.replace(word, " ")
        px, work = _take(_PX, work, "价格")
        leftover = _FILLER.sub(" ", work.replace("挂单", " ").replace("那张", " ")).strip()
        if px is None and leftover and _BARE_NUMBER.fullmatch(leftover):
            px, leftover = Decimal(leftover), ""  # "撤单 XAU 3788": the order resting at 3788
        if not leftover and (ids or px is not None or side):
            return Command("cancel", inst_id=ids[0] if ids else "", px=px, direction=side)
        if len(rest) > 16:
            return None
        raise _Unclear("撤哪张单没看懂。可以发：撤单 XAU、撤单 XAU 3788（按价格）、撤单 全部，或 撤单 订单号。")

    def _sizing(self, rest: str) -> Command:
        """算仓位: arithmetic on the owner's own prices. Nothing is sent anywhere."""
        work, ids = self._without_instruments(rest)
        sl, work = _take(_SL, work, "止损价")
        entry, work = _take(_ENTRY, work, "入场价")
        leftover = _FILLER.sub(" ", work).strip()
        if entry is None and leftover and _BARE_NUMBER.fullmatch(leftover):
            entry, leftover = Decimal(leftover), ""
        if leftover or sl is None:
            raise _Unclear(SIZE_HELP)
        return Command("size", inst_id=self._single(ids, required=True), px=entry, sl=sl)

    def _stops_said(self, text: str) -> Command | None:
        """A stop-loss or take-profit command, in the plain form or the way people say it:
        "止损 XAU 3800", "把止损调到 3800", "调整一下我的止损", "把 XAU 的止损改成 3800"."""
        if _STOP_START.match(text):
            asked = bool(_STOP_VERB_START.match(text))
            return self._stops(_STOP_START.sub("", text, count=1), asked=asked)
        lead = _STOP_LEAD.match(text)
        rest = text[lead.end() :] if lead else text
        verb = _STOP_VERB_START.match(rest)
        if verb:
            rest = _STOP_LEAD.sub("", rest[verb.end() :])
        probe, ids = self._without_instruments(rest)
        probe = probe.strip().lstrip("的").strip()
        asked = bool(verb) or bool(lead and _BA.search(lead.group(0)))
        if not asked or not probe.startswith(("止损", "止盈")):
            return None
        if not re.search(r"\d", probe) and any(word in probe for word in _REMOVE_WORDS):
            # "把止损撤掉": the same as 取消止损.
            named = probe.split(next(word for word in _REMOVE_WORDS if word in probe))[0]
            return Command(
                "stop", inst_id=self._single(ids, required=False), clear_sl="止损" in named, clear_tp="止盈" in named
            )
        return self._stops(rest, asked=True)

    def _stops(self, text: str, asked: bool = False) -> Command | None:
        """`asked`: the message was put as a request ("调整一下止损"), so without a price it is
        still a command, one that needs the price, and not talk about stops."""
        work, ids = self._without_instruments(text)
        if not re.search(r"\d", work):
            # "止损被扫了" is talk. "止损 XAU" is a command with the price left out.
            if (ids and len(text) <= 24) or (asked and len(text) <= 24):
                raise _Unclear(STOP_PRICE_MISSING)
            return None
        sl, work = _take(_SL, work, "止损价")
        tp, work = _take(_TP, work, "止盈价")
        leftover = _FILLER.sub(" ", work).strip()
        if leftover or (sl is None and tp is None):
            raise _Unclear("止盈止损没看懂。写法：止损 XAU 3860，或 止损 XAU 3860 止盈 3950。")
        return Command("stop", inst_id=self._single(ids, required=False), sl=sl, tp=tp)

    def _clear_stops(self, rest: str, clear_sl: bool, clear_tp: bool) -> Command | None:
        work, ids = self._without_instruments(rest)
        leftover = _FILLER.sub(" ", work).strip()
        if leftover:
            if len(leftover) > 12:
                return None
            raise _Unclear(f"这部分我没看懂：“{leftover}”。写法：取消止损 XAU。")
        return Command("stop", inst_id=self._single(ids, required=False), clear_sl=clear_sl, clear_tp=clear_tp)

    # ----------------------------------------------------------------- pieces

    def _single(self, ids: list[str], required: bool) -> str:
        if len(ids) > 1:
            raise _Unclear("一条指令只能写一个品种。")
        if ids:
            return ids[0]
        allowed = self._instruments()
        if len(allowed) == 1:
            return allowed[0] if required else ""
        if required:
            raise _Unclear("要写品种，例如：做多 XAU 100张。")
        return ""

    _SIZE_UNITS = r"(?:(张|个|枚|币|手|美元|美金|刀|盎司|%)|(usdt|usdc|usd|u|oz{base})(?![a-z0-9]))"

    def _size_pattern(self, base: str) -> re.Pattern:
        extra = f"|{re.escape(base.lower())}" if base else ""
        return re.compile(rf"({NUM})\s*" + self._SIZE_UNITS.replace("{base}", extra), re.I)

    def _take_size(self, text: str, base: str, fraction_ok: bool = False) -> tuple[Decimal | None, str, str]:
        """The size and its unit: (value, "contracts" | "coins" | "usd" | "fraction", text without it)."""
        matches = list(self._size_pattern(base).finditer(text))
        if not matches:
            return None, "", text
        if len(matches) > 1:
            raise _Unclear("数量写了不止一个，我不知道该用哪个。")
        match = matches[0]
        value = Decimal(match.group(1))
        word = (match.group(2) or match.group(3)).lower()
        if value <= 0:
            raise _Unclear("数量要大于 0。")
        if word == "张":
            unit = "contracts"
        elif word in ("个", "枚", "币") or (base and word == base.lower()):
            unit = "coins"
        elif word in ("盎司", "oz"):
            if base not in ("XAU", "XAG", ""):
                raise _Unclear("“盎司”只用在黄金和白银上。其他品种请用 张、个 或 U。")
            unit = "coins"
        elif word == "手":
            gold_only = all(inst.split("-")[0] == "XAU" for inst in self._instruments())
            if base != "XAU" and not (not base and gold_only):
                raise _Unclear("“手”只用在黄金上（1 手 = 1 XAU）。其他品种请用 张（合约张数）、个（币的数量）或 U（名义价值）。")
            unit = "coins"  # the owner's own convention: 1 手 of gold is 1 XAU
        elif word == "%":
            if not fraction_ok or value > 100:
                raise _Unclear("百分比只能用在平仓上，而且不能超过 100%。")
            value, unit = value / 100, "fraction"
        else:
            unit = "usd"
        return value, unit, text[: match.start()] + " " + text[match.end() :]

    def _instruments_in(self, text: str) -> list[tuple[int, int, str]]:
        """Where the message names an instrument: (start, end, instrument id), without overlaps."""
        found: list[tuple[int, int, str]] = []
        lowered = text.lower()
        for alias in ALIASES:
            if alias.isascii():
                continue  # English names are tokens; the scan below finds them
            start = lowered.find(alias)
            if start >= 0:
                inst_id = self._resolver.resolve(alias)
                if inst_id:
                    found.append((start, start + len(alias), inst_id))
        for match in _LATIN.finditer(text):
            if match.group(0).lower() in _NOT_INSTRUMENTS:
                continue
            inst_id = self._resolver.resolve(match.group(0))
            if inst_id:
                found.append((match.start(), match.end(), inst_id))
        kept: list[tuple[int, int, str]] = []
        for spot in sorted(found, key=lambda item: (item[0], -item[1])):
            if not kept or spot[0] >= kept[-1][1]:
                kept.append(spot)
        return kept

    def _without_instruments(self, text: str) -> tuple[str, list[str]]:
        spots = self._instruments_in(text)
        ids: list[str] = []
        for start, end, inst_id in reversed(spots):
            text = text[:start] + " " + text[end:]
        for _, _, inst_id in spots:
            if inst_id not in ids:
                ids.append(inst_id)
        return text, ids


# ------------------------------------------------------------------------------ plans


def _action(command: Command) -> str:
    """Which of the TRADE_ACTIONS a command belongs to."""
    if command.kind == "stop":
        return "stops"
    if command.kind in ("close", "cancel"):
        return command.kind
    return "limit" if command.ord_type == "limit" else "open"


class Refused(Exception):
    """A check said no. The message is for the owner, in plain words."""


@dataclass(frozen=True)
class Step:
    """One request to OKX."""

    call: str  # place_order | place_trigger | close_position | cancel_order | place_stop | amend_stop | cancel_stops
    args: dict
    label: str = ""  # what this step is, for the report
    independent: bool = False  # a refusal of this step does not stop the ones after it


@dataclass(frozen=True)
class Plan:
    """Exactly what a confirmation would send, and what the owner is shown before it."""

    action: str  # open | close | cancel | stop
    title: str
    lines: tuple[str, ...]
    steps: tuple[Step, ...]
    inst_id: str = ""
    data: dict = field(default_factory=dict)  # what must still hold when the owner confirms


@dataclass(frozen=True)
class Reply:
    text: str
    title: str = ""  # with a title the reply is a card
    template: str = "blue"
    unclear: bool = False  # an order, but not one the fixed patterns could read in full


@dataclass
class _Pending:
    code: str
    expires_at: float
    command: Command
    plan: Plan
    wrong: int = 0


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _plain(value: Decimal) -> str:
    """A number the way OKX wants it: no exponent, no trailing zeros."""
    text = format(value.normalize(), "f")
    return text if text not in ("", "-0") else "0"


def _amt(value) -> str:
    return f"{float(value):,.2f}"


def _floor_to(value: Decimal, step: Decimal) -> Decimal:
    return (value // step) * step


def _round_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step


def _px(value, spec: InstrumentSpec) -> str:
    decimals = max(0, -spec.tick_sz.normalize().as_tuple().exponent)
    return f"{_dec(value):,.{decimals}f}"


def _at_price(order: OrderInfo, px: Decimal) -> bool:
    """Whether a resting order is the one at this price."""
    return order.px is not None and abs(_dec(order.px) - px) <= px * PRICE_MATCH


def _attached_sl(row: dict) -> float | None:
    for attached in row.get("attachAlgoOrds") or [row]:
        if isinstance(attached, dict) and attached.get("slTriggerPx"):
            return float(attached["slTriggerPx"])
    return None


def _side_word(direction: str) -> str:
    return "多" if direction == "long" else "空"


def _contracts_text(contracts: Decimal, spec: InstrumentSpec) -> str:
    return f"{_plain(contracts)} 张（合 {_plain(contracts * spec.coins_per_contract)} {spec.ct_val_ccy}）"


# ------------------------------------------------------------------------------- desk


class TradeDesk:
    def __init__(
        self,
        cfg: Config,
        journal: Journal,
        reader,
        trader=None,
        resolver=None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        new_code: Callable[[], str] | None = None,
        new_id: Callable[[], str] | None = None,
        day_change: Callable[[], float | None] | None = None,
    ):
        """`reader` is the read-only OKX client the rest of the bot uses. `trader`
        is the client that can place orders, or None while trading is off; in
        demo mode it talks to OKX demo trading, so every check and every preview
        is made against the account the order would go to. `day_change()` is
        today's change of the real account's equity in USD."""
        self._cfg = cfg
        self._journal = journal
        self._trader = trader
        self._okx = trader if trader is not None else reader
        self._mode = cfg.trading if trader is not None and cfg.trading_on else "off"
        self._clock = clock
        self._sleep = sleep
        self._new_code = new_code or new_confirmation_code
        self._new_id = new_id or (lambda: "ftb" + uuid.uuid4().hex[:24])
        self._day_change = day_change
        self._parser = CommandParser(resolver or InstrumentResolver(reader, clock), lambda: cfg.tradable_instruments)
        self._lock = threading.Lock()  # guards the pending confirmation
        self._sending = threading.Lock()  # one order at a time
        self._pending: _Pending | None = None
        self._last_sizing: tuple[str, float] | None = None  # (instrument, when) of the last "算仓位" answered
        self._account_config: tuple[float, AccountConfig] | None = None
        self.stream_status: Callable[[], str] | None = None  # set by the app: whether OKX's push stream is up

    @property
    def mode(self) -> str:
        return self._mode

    def is_command(self, text: str) -> bool:
        return self._read(text) is not None

    def _read(self, text: str) -> Command | None:
        """The command in a message: one of the fixed patterns, or the code of the preview that is waiting."""
        command = self._parser.parse(text)
        if command is None or command.kind == "unclear":
            return self._code_reply(text) or command
        return command

    def _code_reply(self, text: str) -> Command | None:
        """While a preview waits, a short message with its code in it is the confirmation, however it is
        put: "4812", "确认4812执行", "好 4812". A four-digit number with "确认" in front that is not the
        code is a wrong code, and is answered as one, never passed on as a question. A reply to the card
        with a "no" in it ("取消 4812", "4812 先别执行", "不确认") voids the preview instead."""
        with self._lock:
            pending = self._pending
        if pending is None:
            return None
        plain = normalise(text).lower()
        if len(plain) > 24 or "?" in plain:
            return None
        numbers = re.findall(r"\d+", plain)
        about_the_card = any(len(number) == len(pending.code) for number in numbers) or any(
            word in plain for word in ("确认", "确定", "执行")
        )
        if about_the_card and _REFUSAL.search(plain):
            return Command("abort", note=REFUSED_NOTE)
        if pending.code in numbers:
            return Command("confirm", code=pending.code)
        if len(numbers) == 1 and len(numbers[0]) == len(pending.code):
            if plain == numbers[0] or any(word in plain for word in ("确认", "确定", "执行")):
                return Command("confirm", code=numbers[0])
        return None

    def positions_text(self) -> str:
        """The positions and their stops, for reading a command that names a stop relative to the current one."""
        return self._stops_now().replace("\n现在：\n", "").strip() or "现在没有持仓。"

    def handle_understood(self, said: str, command_text: str, in_group: bool = False) -> Reply | None:
        """A command the AI wrote out from the owner's own words. It goes through the same reading,
        checks, preview and code as one he typed; the preview starts with what it was taken to mean.
        Only orders: a confirmation, a cancellation or a switch is never taken from the AI."""
        command = self._parser.parse(command_text)
        if command is None or command.kind not in ("open", "close", "cancel", "stop", "orders", "unclear"):
            return None
        log.info("an order was read with the AI's help: %s", command.kind)
        command = replace(command, source=sources.of_words(said))
        heading = f"我把“{said.strip()[:60]}”理解为：**{command_text}**"
        try:
            reply = self._dispatch(command, in_group)
        except Refused as exc:
            reply = Reply(str(exc))
        except OkxError as exc:
            reply = Reply(f"OKX 返回了错误，没有执行任何操作：{exc.message or '没有给出原因'}（代码 {exc.code}）")
        except OkxNetworkError:
            reply = Reply("现在连不上 OKX，没有执行任何操作。请稍后再试。")
        ending = "" if command.kind == "orders" else "\n不是这个意思就回“取消”，再换个说法。" if reply.title.startswith("下单预览") else ""
        return Reply(f"{heading}\n\n{reply.text}{ending}", title=reply.title, template=reply.template)

    def takes_long(self, text: str) -> bool:
        """Whether answering this message takes a while: the confirmation that starts the demo rehearsal."""
        command = self._read(text)
        with self._lock:
            pending = self._pending
        return command is not None and command.kind == "confirm" and pending is not None and pending.command.kind == "selftest"

    # ------------------------------------------------------------------ entry

    def handle(self, text: str, refused_here: str = "", in_group: bool = False) -> Reply | None:
        """Answers an order command. None when the message is not one.

        `refused_here` is what to say instead when commands are not carried out
        where this one was sent. The router decides that: the owner's private
        chat always, the registered group when GROUP_TRADING is on.
        """
        command = self._read(text)
        if command is None:
            return None
        if refused_here:
            return Reply(refused_here)
        command = self._tagged(command)
        try:
            return self._dispatch(command, in_group)
        except Refused as exc:
            return Reply(str(exc))
        except OkxError as exc:
            return Reply(f"OKX 返回了错误，没有执行任何操作：{exc.message or '没有给出原因'}（代码 {exc.code}）")
        except OkxNetworkError:
            return Reply("现在连不上 OKX，没有执行任何操作。请稍后再试。")

    def _tagged(self, command: Command) -> Command:
        """A command the owner typed himself: "sized" when it opens on an instrument he had sized just before."""
        if command.kind not in ("open", "close", "cancel", "stop"):
            return command
        sized = self._last_sizing
        if (
            command.kind == "open" and sized is not None and sized[0] == command.inst_id
            and self._clock() - sized[1] <= sources.SIZING_SECONDS
        ):  # fmt: skip
            return replace(command, source="sized")
        return replace(command, source="own")

    def _dispatch(self, command: Command, in_group: bool = False) -> Reply:
        kind = command.kind
        if kind == "help":
            return Reply(HELP_TEXT + f"\n\n现在的状态：{self._mode_line()}", title="下单指令", template="turquoise")
        if kind == "settings":
            return Reply("\n".join(self.status_lines()), title="交易设置", template="grey")
        if kind == "orders":
            return self._list_orders(command)
        if kind == "pause":
            return self._pause()
        if kind == "resume":
            return self._resume()
        if kind == "confirm":
            return self._confirm(command)
        if kind == "abort":
            return self._abort(command.note == REFUSED_NOTE)
        if kind == "size":
            reply = self._size(command)
            self._last_sizing = (command.inst_id, self._clock())
            return reply
        if self._mode == "off":
            return Reply(self._off_text())
        if kind == "unclear":
            where_now = self._stops_now() if command.note == STOP_PRICE_MISSING else ""
            return Reply(f"{command.note}{where_now}\n没有执行任何操作。发“交易帮助”可以看全部写法。", unclear=True)
        if kind == "unbreak":
            return self._ask_unbreak(command, in_group)
        if kind == "selftest":
            plan = self._selftest(command.inst_id).plan()
            return self._ask_to_confirm(
                command, plan, "模拟盘自测 · 预览", "回复 **确认 {code}** 开始，回复“取消”作废。{seconds} 秒内有效。", in_group
            )
        action = _action(command)
        if action not in self._cfg.trade_actions:
            raise Refused(
                f"“{ACTION_NAMES[action]}”现在没有开放，这条指令没有执行。{self._actions_line()}"
                "要开放它，需要在服务器的 .env 里改 TRADE_ACTIONS，飞书里改不了。"
            )
        plan = self._plan(command)
        return self._ask_to_confirm(
            command, plan, f"下单预览 · {MODE_LABELS[self._mode]}",
            "回复 **确认 {code}** 执行，回复“取消”作废。{seconds} 秒内有效，过期要重新发指令。", in_group,
        )  # fmt: skip

    def _ask_to_confirm(self, command: Command, plan: Plan, title: str, ending: str, in_group: bool = False) -> Reply:
        """Shows the owner what would be done and holds it until the code comes back."""
        code = self._new_code()
        seconds = self._cfg.trade_confirm_seconds
        with self._lock:
            self._pending = _Pending(code, self._clock() + seconds, command, plan)
        log.info("preview (%s) %s %s: waiting for the code", self._mode, command.kind, command.inst_id or "-")
        ending = ending.format(code=code, seconds=seconds)
        if in_group:
            ending += "在群里回复要先 @我；只有你本人回的确认码有效。"
        body = [f"**{plan.title}**", *(f"- {line}" for line in plan.lines), "", ending]
        return Reply("\n".join(body), title=title, template="red" if self._mode == "live" else "orange")

    # ----------------------------------------------------------- confirmation

    def _confirm(self, command: Command) -> Reply:
        with self._lock:
            pending = self._pending
            if pending is None:
                log.info("a confirmation came with no preview waiting")
                return Reply("现在没有等待确认的指令。")
            if self._clock() > pending.expires_at:
                self._pending = None
                log.info("a confirmation came after the preview had expired")
                return Reply("这个预览已经过期了，没有执行。请重新发指令。")
            if not command.code:
                return Reply("请带上预览卡片里的四位确认码，例如：确认 1234，或者只发 1234。")
            if command.code != pending.code:
                log.info("a confirmation came with the wrong code")
                pending.wrong += 1
                if pending.wrong >= MAX_WRONG_CODES:
                    self._pending = None
                    return Reply("确认码连续错了三次，这个预览已作废，没有执行。请重新发指令。")
                return Reply("确认码不对，没有执行。请核对预览卡片上的四位数。")
            self._pending = None  # a code works once
        if pending.command.kind == "unbreak":
            return self._unbreak()
        with self._sending:
            if pending.command.kind == "selftest":
                try:
                    return self._selftest(pending.command.inst_id).run()
                except Refused as exc:
                    return Reply(f"没有执行。{exc}")
            try:
                plan = self._plan(pending.command, previous=pending.plan)
            except Refused as exc:
                return Reply(f"没有执行。{exc}")
            return self._execute(plan, pending.command.source)

    def _selftest(self, inst_id: str = ""):
        from .selftest import SelfTest  # imported here because that module is built on this one

        return SelfTest(self, inst_id)

    def _abort(self, refused: bool = False) -> Reply:
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return Reply("现在没有等待确认的指令。")
        if refused:
            log.info("a reply to the card said no: the preview is voided")
            return Reply(
                f"已作废：{pending.plan.title}。没有执行任何操作。\n"
                "（你的回复里有“取消、不要、先等等”这类话，我按不执行处理。要执行就重新发指令，再只回确认码。）"
            )
        return Reply(f"已作废：{pending.plan.title}。没有执行任何操作。")

    def _pause(self) -> Reply:
        self._journal.set_setting(PAUSED_SETTING, "1")
        with self._lock:
            self._pending = None
        log.info("the owner paused trading")
        return Reply(
            "已暂停：从现在起不接受开仓指令，等待确认的预览也作废了。\n"
            "平仓、撤单、改止损照常可用。已经挂在 OKX 的单子我没有动，要撤请发“撤单 全部”。\n"
            "发“恢复交易”解除。"
        )

    def _resume(self) -> Reply:
        self._journal.set_setting(PAUSED_SETTING, "0")
        log.info("the owner resumed trading")
        if self._mode == "off":
            return Reply("暂停已解除。不过下单功能本身是关着的（TRADING=off），所以仍然不会下单。")
        return Reply(f"已恢复，可以开仓了（{MODE_LABELS[self._mode]}）。每张单仍然要你回复确认码才执行。")

    def _paused(self) -> bool:
        return self._journal.get_setting(PAUSED_SETTING, "0") == "1"

    # ---------------------------------------------------------- circuit breaker

    def _breaker_hits(self) -> list[Hit]:
        """Losing stop-outs and liquidations that count now: within the window, and after the last time
        the owner lifted the breaker. Raises OkxError / OkxNetworkError when OKX cannot be read."""
        window_start = self._clock() - self._cfg.trade_breaker_hours * 3600
        reset_at = float(self._journal.get_setting(BREAKER_RESET_SETTING, "0") or 0)
        return stop_hits(read_closed(self._okx), read_triggers(self._okx), max(window_start, reset_at))

    def _breaker_text(self, hits: list[Hit]) -> str:
        cfg = self._cfg
        lines = [f"过去 {cfg.trade_breaker_hours} 小时内止损或强平 {len(hits)} 次，你定的规则是满 {cfg.trade_breaker_stops} 次就暂停开仓："]
        lines += [f"- {hit_line(hit, cfg.tz)}" for hit in hits]
        return "\n".join(lines)

    def _check_breaker(self) -> None:
        """Refuses a new position while the breaker is tripped, or when it cannot be checked."""
        if self._cfg.trade_breaker_stops <= 0:
            return
        try:
            hits = self._breaker_hits()
        except (OkxError, OkxNetworkError) as exc:
            log.warning("circuit breaker: OKX's records could not be read (%s)", type(exc).__name__)
            raise Refused("读不到 OKX 的止损和平仓记录，熔断规则没法核对，这次不开仓。请稍后再试。平仓、撤单、改止损不受影响。")
        if len(hits) >= self._cfg.trade_breaker_stops:
            log.info("circuit breaker: an opening order was refused (%d hits)", len(hits))
            raise Refused(
                "熔断中，不接受开仓指令。" + self._breaker_text(hits) + "\n"
                "平仓、撤单、改止损照常可用。要恢复开仓，发“解除熔断”，再回确认码。"
            )

    def _ask_unbreak(self, command: Command, in_group: bool) -> Reply:
        cfg = self._cfg
        if cfg.trade_breaker_stops <= 0:
            return Reply("熔断没有打开（TRADE_BREAKER_STOPS=0），不用解除。")
        hits = self._breaker_hits()
        if len(hits) < cfg.trade_breaker_stops:
            return Reply(
                f"熔断没有触发：过去 {cfg.trade_breaker_hours} 小时内止损或强平 {len(hits)} 次，"
                f"满 {cfg.trade_breaker_stops} 次才会暂停开仓。不用解除。"
            )
        lines = (
            *self._breaker_text(hits).splitlines(),
            "解除以后可以照常开仓，上面这几次不再计入；之后再满次数，会再次暂停。",
        )
        plan = Plan("unbreak", "解除熔断", lines, ())
        return self._ask_to_confirm(
            command, plan, "解除熔断 · 预览", "回复 **确认 {code}** 解除，回复“取消”作废。{seconds} 秒内有效。", in_group
        )

    def _unbreak(self) -> Reply:
        now = self._clock()
        self._journal.set_setting(BREAKER_RESET_SETTING, f"{now:.3f}")
        self._journal.set_setting(BREAKER_TOLD_SETTING, f"{now:.3f}")
        log.info("the owner lifted the circuit breaker")
        return Reply(
            f"熔断已解除，可以开仓了。之前的止损不再计入；之后 {self._cfg.trade_breaker_hours} 小时内再止损或强平满 "
            f"{self._cfg.trade_breaker_stops} 次，会再次暂停开仓。每张单仍然要你回确认码才执行。"
        )

    def breaker_alert(self) -> str:
        """The owner's notice that the breaker has just tripped; "" when there is nothing new to say.
        Called after a position closes or shrinks. Says nothing while opening is not possible anyway."""
        cfg = self._cfg
        if cfg.trade_breaker_stops <= 0 or self._mode == "off":
            return ""
        if "open" not in cfg.trade_actions and "limit" not in cfg.trade_actions:
            return ""
        try:
            hits = self._breaker_hits()
        except (OkxError, OkxNetworkError):
            return ""  # the check before every opening order still refuses when OKX cannot be read
        told = float(self._journal.get_setting(BREAKER_TOLD_SETTING, "0") or 0)
        if len(hits) < cfg.trade_breaker_stops or hits[-1].at <= told:
            return ""
        self._journal.set_setting(BREAKER_TOLD_SETTING, f"{hits[-1].at:.3f}")
        log.info("circuit breaker tripped (%d hits)", len(hits))
        return (
            self._breaker_text(hits) + "\n\n"
            "从现在起不接受开仓指令，平仓、撤单、改止损照常可用。我没有动任何仓位和挂单。\n"
            "要恢复开仓，发“解除熔断”，再回确认码。"
        )

    def _breaker_status(self) -> str:
        cfg = self._cfg
        if cfg.trade_breaker_stops <= 0:
            return "- 熔断：没有打开（TRADE_BREAKER_STOPS=0）"
        rule = f"{cfg.trade_breaker_hours} 小时内止损或强平满 {cfg.trade_breaker_stops} 次就暂停开仓"
        try:
            hits = self._breaker_hits()
        except (OkxError, OkxNetworkError):
            return f"- 熔断：{rule}；这次没读到 OKX 的记录"
        if len(hits) >= cfg.trade_breaker_stops:
            return f"- 熔断：**已触发**（{len(hits)} 次），不接受开仓，发“解除熔断”恢复。规则：{rule}"
        return f"- 熔断：{rule}；现在 {len(hits)} 次"

    # --------------------------------------------------------------- settings

    def _mode_line(self) -> str:
        if self._mode == "off":
            return "下单功能关闭（TRADING=off），发指令不会执行。"
        paused = "，开仓已暂停" if self._paused() else ""
        return f"下单功能已打开，{MODE_LABELS[self._mode]}{paused}。{self._actions_line()}"

    def _actions_line(self) -> str:
        """Which kinds of command are open, when not all of them are. "" when all are."""
        open_now = [ACTION_NAMES[action] for action in TRADE_ACTIONS if action in self._cfg.trade_actions]
        if len(open_now) == len(TRADE_ACTIONS):
            return ""
        if not open_now:
            return "现在没有开放任何下单操作，只能看挂单和设置。"
        return f"现在只开放：{'、'.join(open_now)}。"

    def _off_text(self) -> str:
        lines = [
            "下单功能现在是关着的，这条指令没有执行。",
            "要打开，需要在服务器的 .env 里把 TRADING 设成 demo（模拟盘）或 live（实盘），飞书里改不了。建议先在模拟盘把每种指令都试一遍。",
            "发“交易帮助”可以先看有哪些指令。",
        ]
        if self._cfg.trading_blocked:
            lines.append(f"另外，配置里有一处要改：{self._cfg.trading_blocked}。")
        return "\n".join(lines)

    def status_lines(self) -> list[str]:
        """The trading set-up in plain words: mode, limits, and what the API key may do."""
        cfg = self._cfg
        lines = [f"**下单功能**：{MODE_LABELS[self._mode]}"]
        if cfg.trading_blocked:
            lines.append(f"- 配置有问题，所以保持关闭：{cfg.trading_blocked}")
        if self._mode != "off":
            lines.append("- 开仓已暂停（发“恢复交易”解除）" if self._paused() else "- 没有暂停")
            lines.append(self._breaker_status())
            if cfg.trade_stop_trigger != "last":
                lines.append(f"- 机器人挂的止损止盈按{STOP_TRIGGERS[cfg.trade_stop_trigger]}触发")
        closed = [ACTION_NAMES[action] for action in TRADE_ACTIONS if action not in cfg.trade_actions]
        if closed:
            lines.append(f"**开放的操作**：{self._actions_line()}没有开放：{'、'.join(closed)}")
        else:
            lines.append("**开放的操作**：全部（止盈止损、平仓、撤单、限价挂单、市价开仓）")
        lines.append(f"**允许下单的品种**：{'、'.join(cfg.tradable_instruments)}")
        if self.stream_status is not None:
            lines.append(f"**OKX 推送**：{self.stream_status()}")
        if not cfg.group_trading:
            group = "不执行，只在私聊里执行（GROUP_TRADING=false）"
        elif not cfg.group_chat_id:
            group = "还没有登记群，现在只在私聊里执行"
        elif not cfg.group_show_pnl:
            group = "不执行，因为群里不显示账户（GROUP_SHOW_PNL=false）"
        else:
            group = "你本人在登记的群里 @我 发的指令也执行，别人发的不执行"
        lines.append(f"**群里**：{group}")
        lines.append(f"**单笔风险**：权益的 {cfg.risk_per_trade_pct:g}%（“算仓位”和只写止损不写数量的开仓按它算数量）")
        lines.append("**限额**")
        lines.append(f"- 单笔上限 {cfg.trade_max_order_usd:,.0f} USDT（名义价值）")
        lines.append(f"- 单个品种的持仓上限 {cfg.trade_max_position_usd:,.0f} USDT（含没成交的挂单）")
        opens = ""
        if self._mode != "off":
            opens = f"，今天已开 {self._journal.count_opens(self._day_start(), self._clock() + 1, self._mode)} 次"
        lines.append(f"- 每天最多开仓 {cfg.trade_max_opens_per_day} 次{opens}")
        if cfg.trade_daily_loss_limit_usd is not None:
            lines.append(f"- 当天权益少了 {cfg.trade_daily_loss_limit_usd:,.0f} USD 以后不再开新仓（只管实盘）")
        else:
            lines.append("- 没有设每日亏损线")
        lines.append(f"- 限价不能比现价差超过 {cfg.trade_max_deviation_pct:g}%（往会立刻成交的方向）")
        lines.append(f"- 预览后 {cfg.trade_confirm_seconds} 秒内确认；这期间价格动了超过 {cfg.trade_max_drift_pct:g}% 就要重新下指令")
        lines.append(f"- 新仓位用{'全仓' if cfg.trade_margin_mode == 'cross' else '逐仓'}；开仓{'必须' if cfg.trade_require_stop or cfg.trade_max_risk_pct is not None else '不强制'}带止损")
        if cfg.trade_max_risk_pct is not None:
            lines.append(f"- 开仓的止损触发时最多亏权益的 {cfg.trade_max_risk_pct:g}%，超过就不执行（TRADE_MAX_RISK_PCT）")
        else:
            lines.append("- 没有设开仓止损的亏损上限（TRADE_MAX_RISK_PCT）")
        lines.append(f"**OKX key**（{self._account_label()}）")
        try:
            account = self._okx.account_config()
        except (OkxError, OkxNetworkError) as exc:
            lines.append(f"- 暂时读不到：{exc}")
        else:
            lines.append(f"- 交易权限：{'有' if account.can_trade else '没有'}")
            lines.append("- 提现权限：有。机器人用不到，建议在 OKX 里关掉" if account.can_withdraw else "- 提现权限：没有")
            lines.append("- 已绑定 IP" if account.ip else "- 没有绑定 IP，建议绑定服务器的 IP")
            lines.append(f"- 持仓模式：{'双向持仓' if account.hedge_mode else '单向持仓'}")
            if account.spot_only:
                lines.append(f"- 账户模式：现货模式。{self._spot_mode_text()}")
            elif account.level_name:
                lines.append(f"- 账户模式：{account.level_name}")
        if self._mode != "off":
            lines.extend(self._funds_lines())
        lines.append(f"**模拟盘自测**：{self._selftest().last_text()}")
        lines.append("限额、品种名单和开放哪些操作只能在服务器的 .env 里改，飞书里改不了。")
        return lines

    def _spot_mode_text(self) -> str:
        where = "OKX 的模拟交易里" if self._mode == "demo" else "OKX 里"
        return (
            f"这个{self._account_label()}账户在 OKX 里是“现货模式”，这个模式下 OKX 不接受合约订单，所以开不了仓。"
            f"请在 {where}进合约的交易页，点右上角的“···”，进“交易设置”里的“交易模式”，把账户模式改成“合约模式”"
            "（以前叫“现货和合约模式”；第一次只能在网页或 App 里改），改完重发指令就行。"
        )

    def _funds_lines(self) -> list[str]:
        """What the account that orders go to holds, with units: in demo mode the cards show the real account only."""
        try:
            state = self._okx.account_state()
        except (OkxError, OkxNetworkError) as exc:
            return [f"**账户资金**（{self._account_label()}）", f"- 暂时读不到：{exc}"]
        lines = [f"**账户资金**（{self._account_label()}）"]
        if state.snapshot.total_eq is not None:
            lines.append(f"- 总权益约 {state.snapshot.total_eq:,.2f} USD")
        lines.extend(f"- {line}" for line in currency_lines(state))
        lines.append(f"- 持仓 {len(state.positions)} 个")
        return lines

    def _account_label(self) -> str:
        if self._trader is not None:
            return MODE_LABELS[self._mode]
        return "模拟盘" if self._cfg.okx_demo else "实盘"

    # ------------------------------------------------------------ open orders

    def _list_orders(self, command: Command) -> Reply:
        orders = self._okx.pending_orders(command.inst_id or None) + self._trigger_orders(command.inst_id or None)
        state = self._okx.account_state()
        lines = [f"**挂单（{len(orders)} 张）**"] if orders else ["现在没有挂单。"]
        lines.extend(f"- {self._order_line(order)}" for order in orders)
        stops = [p for p in state.positions if p.stops and (not command.inst_id or p.inst_id == command.inst_id)]
        if stops:
            lines.append("**止盈止损**")
            for position in stops:
                spec = self._spec_or_none(position.inst_id)
                for stop in position.stops:
                    sl = "无" if stop.sl_trigger_px is None else self._price(stop.sl_trigger_px, spec)
                    tp = "无" if stop.tp_trigger_px is None else self._price(stop.tp_trigger_px, spec)
                    lines.append(f"- {position.inst_id} {_side_word(position.direction)}单：止损 {sl}，止盈 {tp}")
        return Reply("\n".join(lines), title=f"当前挂单 · {self._account_label()}", template="wathet")

    def _size(self, cmd: Command) -> Reply:
        """How big a position may be so that its stop costs the owner's share of the equity (RISK_PER_TRADE_PCT)."""
        spec = self._spec_or_none(cmd.inst_id)
        if spec is None:
            raise Refused(f"读不到 {cmd.inst_id} 的合约规格，算不了。")
        if spec.ct_type and spec.ct_type != "linear":
            raise Refused("算仓位目前只算 USDT 本位合约。")
        state = self._okx.account_state()
        equity = _dec(state.snapshot.total_eq or 0)
        if equity <= 0:
            raise Refused("读不到账户权益，算不了。")
        last = _dec(self._okx.ticker(cmd.inst_id).last)
        entry = cmd.px if cmd.px is not None else last
        gap = abs(entry - cmd.sl)
        if gap == 0:
            raise Refused("止损价和入场价一样，算不了。")
        long = cmd.sl < entry
        base = cmd.inst_id.split("-")[0]
        settle = spec.settle_ccy or "USDT"
        pct = _dec(self._cfg.risk_per_trade_pct)
        budget = equity * pct / 100
        per_contract = spec.coins_per_contract
        contracts = _floor_to(budget / gap / per_contract, spec.lot_sz)
        lines = [
            f"按你定的单笔风险 {float(pct):g}%：权益 {_amt(equity)} USD，这一笔止损触发时最多亏 {_amt(budget)} {settle}",
            f"{_side_word('long' if long else 'short')}单，入场 {_px(entry, spec)}{'（现价）' if cmd.px is None else ''}，"
            f"止损 {_px(cmd.sl, spec)}，相差 {_plain(gap)}（{gap / entry * 100:.2f}%），每 1 {base} 亏 {_plain(gap)} {settle}",
        ]
        if contracts < spec.min_sz or contracts <= 0:
            least = spec.min_sz * per_contract * gap
            lines.append(
                f"按这个止损，最小的 {_plain(spec.min_sz)} 张也会亏约 {_amt(least)} {settle}，"
                f"占权益 {least / equity * 100:.2f}%，超过了 {float(pct):g}%。止损要更近，或者这笔不做。"
            )
        else:
            coins = contracts * per_contract
            notional = coins * entry
            lines.append(
                f"最多 {_contracts_text(contracts, spec)}，名义价值约 {_amt(notional)} {settle}，"
                f"实际杠杆 {notional / equity:.2f} 倍，止损触发亏约 {_amt(coins * gap)} {settle}"
            )
            held = [p for p in state.positions if p.inst_id == cmd.inst_id and p.direction == ("long" if long else "short")]
            if held and held[0].lever:
                margin = notional / _dec(held[0].lever)
                lines.append(f"按你现在 {held[0].lever:g} 倍的杠杆设置，占用保证金约 {_amt(margin)} {settle}")
                if margin > equity:
                    lines.append("保证金超过了账户权益，这么大开不出来")
            if notional > _dec(self._cfg.trade_max_position_usd):
                lines.append(
                    f"超过了单个品种的持仓上限 {_amt(_dec(self._cfg.trade_max_position_usd))} USDT，按现在的限额开不了这么大"
                )
            elif notional > _dec(self._cfg.trade_max_order_usd):
                lines.append(f"超过了单笔上限 {_amt(_dec(self._cfg.trade_max_order_usd))} USDT，要分几次开")
        for position in state.positions:
            if position.inst_id != cmd.inst_id or position.direction != ("long" if long else "short"):
                continue
            coins_held = _dec(position.qty) * per_contract
            cost = coins_held * gap
            lines.append(
                f"你现在的{_side_word(position.direction)}单 {_plain(coins_held)} {base}，按同样的止损距离要亏约 {_amt(cost)} {settle}，"
                f"占权益 {cost / equity * 100:.1f}%"
            )
        resting = cmd.px is None or (cmd.px < last if long else cmd.px > last)  # a limit order there would wait
        if contracts >= spec.min_sz and contracts > 0 and resting:
            order = f"{'限价' if cmd.px is not None else ''}做{'多' if long else '空'} {base}"
            where = f" {_plain(_round_to(cmd.px, spec.tick_sz))}" if cmd.px is not None else ""
            lines.append(
                f"要按这个数量开，发“{order}{where} 止损 {_plain(_round_to(cmd.sl, spec.tick_sz))}”，预览里会按当时的价格再算一遍"
            )
        lines.append("这是按你给的价格和你自己的规则算出的数，不是开仓建议。")
        return Reply("\n".join(f"- {line}" for line in lines), title=f"算仓位 · {cmd.inst_id}", template="wathet")

    def _stops_now(self) -> str:
        """Where each position's stop-loss and take-profit are now, so the owner can pick the new price."""
        try:
            positions = self._okx.account_state().positions
        except (OkxError, OkxNetworkError):
            return ""
        lines = []
        for position in positions:
            spec = self._spec_or_none(position.inst_id)
            sl = next((stop.sl_trigger_px for stop in position.stops if stop.sl_trigger_px is not None), None)
            tp = next((stop.tp_trigger_px for stop in position.stops if stop.tp_trigger_px is not None), None)
            mark = f"，标记价 {self._price(position.mark_px, spec)}" if position.mark_px else ""
            lines.append(
                f"- {position.inst_id} {_side_word(position.direction)}单{mark}："
                f"止损 {'没有' if sl is None else self._price(sl, spec)}，止盈 {'没有' if tp is None else self._price(tp, spec)}"
            )
        return ("\n现在：\n" + "\n".join(lines)) if lines else "\n现在没有持仓。"

    def _order_line(self, order: OrderInfo) -> str:
        spec = self._spec_or_none(order.inst_id)
        side = "买入" if order.side == "buy" else "卖出"
        if order.reduce_only:
            side += "（只减仓）"
        price = "市价" if order.px is None else f"限价 {self._price(order.px, spec)}"
        if order.ord_type == "trigger":
            side = f"突破单{'做多' if order.side == 'buy' else '做空'}"
            price = f"触发价 {self._price(order.px, spec)}" if order.px is not None else "触发价没读到"
            if order.sl_trigger_px:
                price += f"，成交后带止损 {self._price(order.sl_trigger_px, spec)}"
        size = f"{order.sz:g} 张"
        if spec is not None:
            size = _contracts_text(_dec(order.sz), spec)
        filled = f"，已成交 {order.filled_sz:g} 张" if order.filled_sz else ""
        return f"{order.inst_id} {side} {price}，{size}{filled}，{'计划委托' if order.ord_type == 'trigger' else '订单'}号 {order.ord_id}"

    def _spec_or_none(self, inst_id: str) -> InstrumentSpec | None:
        try:
            return self._okx.instrument_spec(inst_id)
        except (OkxError, OkxNetworkError):
            return None

    @staticmethod
    def _price(value, spec: InstrumentSpec | None) -> str:
        return _px(value, spec) if spec is not None else f"{float(value):,.2f}"

    # --------------------------------------------------------------- planning

    def _plan(self, command: Command, previous: Plan | None = None) -> Plan:
        if command.kind == "open":
            return self._plan_open(command, previous)
        if command.kind == "close":
            return self._plan_close(command, previous)
        if command.kind == "cancel":
            return self._plan_cancel(command, previous)
        return self._plan_stop(command, previous)

    def _account(self) -> tuple[AccountConfig, AccountState]:
        known = self._account_config
        if known is None or self._clock() - known[0] > ACCOUNT_CONFIG_TTL:
            known = (self._clock(), self._okx.account_config())
            self._account_config = known
        if not known[1].can_trade:
            self._account_config = None  # read it again next time: the owner may have just changed the key
            raise Refused("这把 OKX key 没有交易权限，下不了单。请在 OKX 里给它开通“交易”权限（不要开提现）。")
        return known[1], self._okx.account_state()

    def _tradable(self, inst_id: str) -> InstrumentSpec:
        allowed = self._cfg.tradable_instruments
        if inst_id not in allowed:
            raise Refused(
                f"{inst_id} 不在允许下单的品种里（现在允许：{'、'.join(allowed)}）。"
                "要加品种，请改服务器 .env 里的 TRADE_INSTRUMENTS，飞书里改不了。"
            )
        if not inst_id.endswith("-SWAP"):
            raise Refused(f"目前只支持永续合约（名字以 -SWAP 结尾），{inst_id} 不是。")
        spec = self._okx.instrument_spec(inst_id)
        if spec.ct_type != "linear":
            raise Refused(f"目前只支持 USDT 或 USDC 本位的合约，{inst_id} 是币本位的。")
        if spec.state != "live":
            raise Refused(f"{inst_id} 现在不在正常交易状态（{spec.state}），先不下单。")
        return spec

    def _day_start(self) -> float:
        now = datetime.fromtimestamp(self._clock(), tz=self._cfg.tz)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    @staticmethod
    def _value(position: Position, spec: InstrumentSpec) -> Decimal:
        if position.notional_usd is not None:
            return _dec(position.notional_usd)
        price = position.mark_px or position.avg_px or 0.0
        return _dec(position.qty) * spec.coins_per_contract * _dec(price)

    @staticmethod
    def _fingerprint(positions: list[Position]) -> list:
        return sorted(
            [p.inst_id, p.direction, p.mgn_mode, f"{p.qty:g}", sorted([s.algo_id, s.sl_trigger_px, s.tp_trigger_px] for s in p.stops)]
            for p in positions
        )

    def _unchanged(self, previous: Plan | None, positions: list[Position]) -> None:
        if previous is not None and previous.data.get("positions") != self._fingerprint(positions):
            raise Refused("在你确认之前，这个品种的仓位或止盈止损变了，预览里的数字已经不准。请重新发指令。")

    # ------------------------------------------------------------------- open

    def _opening(self, cmd: Command, previous: Plan | None) -> tuple[InstrumentSpec, AccountConfig, AccountState, list[Position], list[Position], str]:
        """The checks every opening order goes through first. Returns (spec, account, state, held, the held
        positions on the order's side, the order's direction)."""
        if self._paused():
            raise Refused("交易已暂停（你发过“停止交易”），现在不接受开仓指令。发“恢复交易”解除。平仓、撤单、改止损不受影响。")
        self._check_breaker()
        inst_id = cmd.inst_id
        base = inst_id.split("-")[0]
        spec = self._tradable(inst_id)
        account, state = self._account()
        if account.spot_only:
            self._account_config = None  # read it again next time: the mode may just have been changed
            raise Refused(self._spot_mode_text())
        held = [p for p in state.positions if p.inst_id == inst_id]
        self._unchanged(previous, held)
        direction = cmd.direction
        if direction == "same":
            if not held:
                raise Refused(f"{inst_id} 现在没有仓位，没法加仓。开新仓请发“做多”或“做空”。")
            if len(held) > 1:
                raise Refused(f"{inst_id} 同时有多单和空单，请写明“做多”还是“做空”。")
            direction = held[0].direction
        long = direction == "long"
        word = _side_word(direction)
        same = [p for p in held if p.direction == direction]
        if len(same) < len(held) and not account.hedge_mode:
            other = _side_word("short" if long else "long")
            raise Refused(
                f"{inst_id} 上现在有{other}单。账户是单向持仓，这时候开{word}单只会先把{other}单减掉。"
                f"要平仓请发“平仓 {base}”，平完再开{word}单。"
            )
        return spec, account, state, held, same, direction

    def _plan_open(self, cmd: Command, previous: Plan | None) -> Plan:
        if cmd.tiers:
            return self._plan_ladder(cmd, previous)
        cfg = self._cfg
        spec, account, state, held, same, direction = self._opening(cmd, previous)
        inst_id = cmd.inst_id
        base = inst_id.split("-")[0]
        long = direction == "long"
        word = _side_word(direction)
        last = _dec(self._okx.ticker(inst_id).last)
        settle = spec.settle_ccy or "USDT"
        px: Decimal | None = None
        ref = last
        trigger: Decimal | None = None
        if cmd.ord_type == "trigger":
            trigger = _round_to(cmd.px, spec.tick_sz)
            gap = (trigger - last) / last * 100
            if (gap <= 0) if long else (gap >= 0):
                raise Refused(
                    f"突破{'做多' if long else '做空'}的触发价要{'高' if long else '低'}于现价 {_px(last, spec)}，你写的是 {_px(trigger, spec)}。"
                    f"价格已经过了，想现在进场请用市价单；想等它回来，请用限价单。"
                )
            if abs(gap) > FAR_LIMIT_PCT:
                raise Refused(f"触发价 {_px(trigger, spec)} 离现价 {_px(last, spec)} 有 {abs(gap):.1f}% 远，像是输错了。请核对后重发。")
            ref = trigger
        elif cmd.ord_type == "limit":
            px = _round_to(cmd.px, spec.tick_sz)
            if px <= 0:
                raise Refused("价格要大于 0。")
            gap = (px - last) / last * 100  # above the market when positive
            crossing = gap if long else -gap
            if crossing > _dec(cfg.trade_max_deviation_pct):
                raise Refused(
                    f"限价 {_px(px, spec)} 比现价 {_px(last, spec)} {'高' if long else '低'}了 {abs(gap):.2f}%，"
                    f"这张单会立刻按市价附近成交。想马上成交请用市价单；想挂单等价格，"
                    f"{word}单的限价要{'低' if long else '高'}于现价。"
                )
            if -crossing > FAR_LIMIT_PCT:
                raise Refused(f"限价 {_px(px, spec)} 离现价 {_px(last, spec)} 有 {abs(gap):.1f}% 远，像是输错了。请核对后重发。")
            ref = px
        elif previous is not None:
            before = Decimal(previous.data["ref"])
            drift = abs(last - before) / before * 100
            if drift > _dec(cfg.trade_max_drift_pct):
                raise Refused(
                    f"价格在你确认前动了 {drift:.2f}%（预览时 {_px(before, spec)}，现在 {_px(last, spec)}），"
                    f"超过了允许的 {cfg.trade_max_drift_pct:g}%。请重新发指令。"
                )

        per_contract = spec.coins_per_contract
        equity = _dec(state.snapshot.total_eq or 0)
        entry_name = "触发价" if trigger is not None else "限价" if px is not None else "现价"
        sl = _round_to(cmd.sl, spec.tick_sz) if cmd.sl is not None else None
        tp = _round_to(cmd.tp, spec.tick_sz) if cmd.tp is not None else None
        sized = ""  # how a risk-sized order came to its size, for the preview and for a refusal
        if previous is not None:
            contracts = Decimal(previous.data["sz"])
            sized = previous.data.get("sized", "")
        elif cmd.unit == "risk":
            contracts, sized = self._risk_sized(direction, ref, sl, entry_name, equity, spec)
        elif cmd.unit == "contracts":
            contracts = cmd.size
            if contracts % spec.lot_sz != 0:
                raise Refused(f"{inst_id} 的数量要是 {_plain(spec.lot_sz)} 张的整数倍。")
        elif cmd.unit == "coins":
            contracts = _floor_to(cmd.size / per_contract, spec.lot_sz)
        else:
            contracts = _floor_to(cmd.size / (ref * per_contract), spec.lot_sz)
        if contracts < spec.min_sz:
            least = spec.min_sz * per_contract
            raise Refused(
                f"数量太小。{inst_id} 最少下 {_plain(spec.min_sz)} 张，也就是 {_plain(least)} {spec.ct_val_ccy}，"
                f"按现价约 {_amt(least * ref)} {settle}。"
            )
        value = contracts * per_contract * ref

        side = "buy" if long else "sell"
        held_value = sum((self._value(p, spec) for p in same), Decimal(0))
        queued = self._queued(inst_id, direction, account, per_contract)
        max_order, max_position = _dec(cfg.trade_max_order_usd), _dec(cfg.trade_max_position_usd)
        if cmd.unit == "risk" and previous is None:
            # Sized from the stop: when that comes to more than the limits allow, take as much as they allow.
            room = min(max_order, max_position - held_value - queued)
            if value > room:
                capped = _floor_to(room * CAP_SHARE / (per_contract * ref), spec.lot_sz)
                if capped >= spec.min_sz and capped > 0:
                    which = f"单笔上限 {max_order:,.0f}" if room == max_order else f"持仓上限 {max_position:,.0f} 还剩的额度"
                    sized += f"；这超过了{which}，按上限减到 {_contracts_text(capped, spec)}，打到止损时亏得也相应少"
                    contracts, value = capped, capped * per_contract * ref

        exposure = self._exposure(state)
        merge = bool(same) and sl is not None and px is None and trigger is None  # a market addition: one stop for the whole position
        held_coins = sum((_dec(p.qty) for p in same), Decimal(0)) * per_contract
        mark = _dec(same[0].mark_px or last) if same else last
        others = exposure.without(inst_id, direction) if merge else exposure
        kept = abs(mark - sl) * held_coins if merge else Decimal(0)  # the held part, under the new stop
        total_cap = cfg.trade_max_total_risk_pct
        if total_cap is not None and sl is not None and equity > 0:
            line = equity * _dec(total_cap) / 100
            room = line * (CONFIRM_RISK_SLACK if previous is not None else 1) - others.loss - kept
            gap = abs(ref - sl)
            if gap * contracts * per_contract > room:
                fits = _floor_to(max(room, Decimal(0)) / gap / per_contract, spec.lot_sz)
                where = f"账户现在所有止损都打到要亏约 {_amt(others.loss + kept)}，占权益 {(others.loss + kept) / equity * 100:.1f}%"
                if cmd.unit == "risk" and previous is None and fits >= spec.min_sz and fits > 0:
                    sized += f"；{where}，总风险上限 {total_cap:g}% 还剩约 {_amt(max(room, Decimal(0)))}，按它减到 {_contracts_text(fits, spec)}"
                    contracts, value = fits, fits * per_contract * ref
                else:
                    most = f"按这个止损最多还能开 {_contracts_text(fits, spec)}" if fits >= spec.min_sz and fits > 0 else "按这个止损已经放不下了"
                    raise Refused(
                        (f"{sized}。" if sized else "")
                        + f"{where}；加上这张会超过你定的账户总风险 {total_cap:g}%（TRADE_MAX_TOTAL_RISK_PCT）。{most}。"
                        "可以先平掉或减掉别的仓位、撤掉挂单，或者把止损放近一点。"
                    )

        if value > max_order:
            hint = f"这张单最多能开约 {_contracts_text(_floor_to(max_order / (per_contract * ref), spec.lot_sz), spec)}。"
            raise Refused(
                (f"{sized}。" if sized else "")
                + f"这张单约 {_amt(value)} {settle}，超过了单笔上限 {cfg.trade_max_order_usd:,.0f}。{hint}"
                "上限在服务器 .env 的 TRADE_MAX_ORDER_USD，飞书里改不了。"
            )
        total = held_value + queued + value
        if total > max_position:
            raise Refused(
                (f"{sized}。" if sized else "")
                + f"下了这张单，{inst_id} 的{word}单会到约 {_amt(total)} {settle}"
                f"（现有 {_amt(held_value)}，挂单 {_amt(queued)}，这张 {_amt(value)}），"
                f"超过了单个品种的持仓上限 {cfg.trade_max_position_usd:,.0f}。可以写一个更小的数量。"
                "上限在服务器 .env 的 TRADE_MAX_POSITION_USD，飞书里改不了。"
            )
        self._check_daily()

        if cfg.trade_require_stop and sl is None:
            raise Refused("按你的设置（TRADE_REQUIRE_STOP=true），开仓必须带止损。请在指令后面加上“止损 价格”。")
        self._check_stop_sides(direction, ref, sl, tp, entry_name, spec)
        self._check_max_risk(contracts, ref, sl, entry_name, equity, spec, confirming=previous is not None)

        td_mode = (same or held)[0].mgn_mode if held else cfg.trade_margin_mode
        pos_side = direction if account.hedge_mode else None
        lever = None
        try:
            lever = self._okx.leverage(inst_id, td_mode)
        except (OkxError, OkxNetworkError) as exc:
            log.warning("could not read the leverage of %s: %s", inst_id, exc)

        lines = [f"数量 {_contracts_text(contracts, spec)}，按{entry_name} {_px(ref, spec)} 约 {_amt(value)} {settle}（名义价值）"]
        if sized:
            lines.append(sized)
        mode_word = "全仓" if td_mode == "cross" else "逐仓"
        if lever:
            margin = value / _dec(lever)
            lines.append(f"杠杆 {lever:g} 倍（在 OKX 里设的），大约占用保证金 {_amt(margin)} {settle}，{mode_word}")
            available = next((c.avail_eq for c in state.snapshot.currencies if c.ccy == settle), None)
            if available is not None and margin > _dec(available):
                lines.append(f"注意：可用保证金只有 {_amt(available)} {settle}，可能不够，OKX 会拒单")
        else:
            lines.append(f"{mode_word}，杠杆按 OKX 里的设置")
        if px is not None:
            gap = (px - last) / last * 100
            where = "高" if gap > 0 else "低"
            waits = "会立刻成交" if (gap > 0) == long and gap != 0 else "价格到了才成交"
            lines.append(f"现价 {_px(last, spec)}，限价比现价{where} {abs(gap):.2f}%，{waits}")
        if trigger is not None:
            gap = abs(trigger - last) / last * 100
            kind = {"last": "最新价", "mark": "标记价", "index": "指数价"}.get(cfg.trade_stop_trigger, "最新价")
            lines.append(
                f"突破单（计划委托）：{kind}{'涨到' if long else '跌到'} {_px(trigger, spec)}（离现价 {_px(last, spec)} {gap:.2f}%）"
                f"时按市价{'做多' if long else '做空'}，成交价可能比触发价差一点（滑点）；没到就一直挂着，不占保证金"
            )
        coins = contracts * per_contract
        if sl is not None:
            loss = abs(sl - ref) * coins
            lines.append(
                f"止损 {_px(sl, spec)}（离{entry_name} {abs(sl - ref) / ref * 100:.2f}%，触发后大约亏 {_amt(loss)} {settle}"
                f"{self._risk_share(loss, state)}）"
            )
        else:
            lines.append("止损 无（这张单不带止损）")
        if tp is not None:
            lines.append(f"止盈 {_px(tp, spec)}（离{entry_name} {abs(tp - ref) / ref * 100:.2f}%，触发后大约赚 {_amt(abs(tp - ref) * coins)} {settle}）")
        else:
            lines.append("止盈 无")
        lines.append(f"下单后 {inst_id} 的{word}单约 {_amt(total)} {settle}（上限 {cfg.trade_max_position_usd:,.0f}）")
        lines.extend(self._total_risk_lines(others, kept, (abs(ref - sl) * coins) if sl is not None else None, equity, settle))
        liq = self._liquidation_estimate(state, inst_id, direction, td_mode, lever, held_coins, mark, ref, coins, equity, value)
        if liq is not None:
            lines.append(f"粗估强平价约 {_px(liq, spec)}（{'全仓，按账户权益亏光算' if td_mode == 'cross' else '逐仓，按这张单的保证金亏光算'}；实际强平会早一点，以 OKX 显示为准）")
            if sl is not None and ((long and sl <= liq) or (not long and sl >= liq)):
                lines.append("**注意：止损在粗估强平价外面，价格到止损之前可能先强平**")
        track = self._source_line(cmd.source) if previous is None else ""
        if track:
            lines.append(track)

        args = {"inst_id": inst_id, "td_mode": td_mode, "side": side, "ord_type": cmd.ord_type, "sz": _plain(contracts)}
        if px is not None:
            args["px"] = _plain(px)
        if pos_side:
            args["pos_side"] = pos_side
        if trigger is not None:
            args.pop("ord_type")
            args["trigger_px"] = _plain(trigger)
            steps = [Step("place_trigger", args, "挂突破单")]
        else:
            steps = [Step("place_order", args, "开仓" if not same else "加仓")]
        if merge:
            stop_steps, stop_lines = self._merged_stop(same[0], sl, tp, td_mode, pos_side, spec, held_coins, coins, mark, ref, settle)
            steps.extend(stop_steps)
            lines.extend(stop_lines)
        else:
            if sl is not None:
                args["sl_trigger_px"] = _plain(sl)
            if tp is not None:
                args["tp_trigger_px"] = _plain(tp)
            if same and sl is not None and trigger is None:
                lines.append(
                    f"这张限价单成交后，加的这部分单独带止损；要和原来的合成一张，成交后发“止损 {base} {_plain(sl)}”"
                )
        title = f"{'限价' if px is not None else '市价'}{'加' if same else '做'}{word} {inst_id}"
        if trigger is not None:
            title = f"突破{'加' if same else '做'}{word} {inst_id}（{'涨破' if long else '跌破'} {_plain(trigger)}）"
        data = {"sz": _plain(contracts), "ref": _plain(last), "positions": self._fingerprint(held)}
        if sized:
            data["sized"] = sized
        return Plan("open", title, tuple(lines), tuple(steps), inst_id, data)

    def _plan_ladder(self, cmd: Command, previous: Plan | None) -> Plan:
        """分档挂单: limit orders at several prices on one side, the same size each, each carrying the same stop.
        If they all fill and the stop is hit, the loss is the per-trade risk: the tiers share it, they do not
        each take it. The limits and the account's total risk are measured with every tier filled."""
        cfg = self._cfg
        spec, account, state, held, same, direction = self._opening(cmd, previous)
        inst_id = cmd.inst_id
        base = inst_id.split("-")[0]
        long = direction == "long"
        word = _side_word(direction)
        last = _dec(self._okx.ticker(inst_id).last)
        settle = spec.settle_ccy or "USDT"
        per_contract = spec.coins_per_contract
        equity = _dec(state.snapshot.total_eq or 0)

        prices = sorted({_round_to(p, spec.tick_sz) for p in cmd.tiers}, reverse=long)  # nearest the market first
        if len(prices) < len(cmd.tiers):
            raise Refused(f"有几档按 {inst_id} 的最小价格单位 {_plain(spec.tick_sz)} 取整后成了同一个价，请把档位拉开一点。")
        for price in prices:
            gap = (price - last) / last * 100
            crossing = gap if long else -gap
            if crossing > _dec(cfg.trade_max_deviation_pct):
                raise Refused(
                    f"有一档 {_px(price, spec)} 比现价 {_px(last, spec)} {'高' if long else '低'}了 {abs(gap):.2f}%，会立刻按市价附近成交。"
                    f"分档挂单是等价格{'回落' if long else '反弹'}，{word}单的每一档都要{'低' if long else '高'}于现价。"
                )
            if -crossing > FAR_LIMIT_PCT:
                raise Refused(f"有一档 {_px(price, spec)} 离现价 {_px(last, spec)} 有 {abs(gap):.1f}% 远，像是输错了。请核对后重发。")
        sl = _round_to(cmd.sl, spec.tick_sz)
        tp = _round_to(cmd.tp, spec.tick_sz) if cmd.tp is not None else None
        self._check_stop_sides(direction, prices[-1], sl, None, "最远一档", spec)
        self._check_stop_sides(direction, prices[0], None, tp, "最近一档", spec)
        count = Decimal(len(prices))
        price_sum = sum(prices, Decimal(0))
        gap_sum = sum((abs(price - sl) for price in prices), Decimal(0))  # the loss of one coin in every tier
        average = price_sum / count

        held_value = sum((self._value(p, spec) for p in same), Decimal(0))
        queued = self._queued(inst_id, direction, account, per_contract)
        max_order, max_position = _dec(cfg.trade_max_order_usd), _dec(cfg.trade_max_position_usd)
        exposure = self._exposure(state)
        total_cap = cfg.trade_max_total_risk_pct
        risk_room = None
        if total_cap is not None and equity > 0:
            risk_room = equity * _dec(total_cap) / 100 * (CONFIRM_RISK_SLACK if previous is not None else 1) - exposure.loss

        sized = ""
        if previous is not None:
            sizes = [Decimal(size) for size in previous.data["sizes"]]
            each = sizes[0]
            sized = previous.data.get("sized", "")
        else:
            if cmd.unit == "risk":
                if equity <= 0:
                    raise Refused("读不到账户权益，没法按单笔风险算数量。请写明数量。")
                pct = _dec(cfg.risk_per_trade_pct)
                budget = equity * pct / 100
                each = _floor_to(budget / gap_sum / per_contract, spec.lot_sz)
                sized = (
                    f"数量按你定的单笔风险算：权益 {_amt(equity)} USD 的 {float(pct):g}% 是 {_amt(budget)} {settle}，"
                    f"{len(prices)} 档全部成交再打到止损才亏这么多，每档 {_contracts_text(each, spec)}"
                )
                caps = [
                    (_floor_to(max_order * CAP_SHARE / (max(prices) * per_contract), spec.lot_sz), f"单笔上限 {max_order:,.0f}"),
                    (_floor_to((max_position - held_value - queued) * CAP_SHARE / (price_sum * per_contract), spec.lot_sz), f"持仓上限 {max_position:,.0f} 还剩的额度"),
                ]
                if risk_room is not None:
                    caps.append((_floor_to(max(risk_room, Decimal(0)) / gap_sum / per_contract, spec.lot_sz), f"账户总风险上限 {total_cap:g}% 还剩的额度"))
                for most, which in caps:
                    if each > most:
                        each = most
                        sized += f"；这超过了{which}，减到每档 {_contracts_text(most, spec)}"
            elif cmd.unit == "contracts":
                each = _floor_to(cmd.size / count, spec.lot_sz)
            elif cmd.unit == "coins":
                each = _floor_to(cmd.size / count / per_contract, spec.lot_sz)
            else:
                each = _floor_to(cmd.size / (price_sum * per_contract), spec.lot_sz)
            if cmd.unit in ("contracts", "coins") and each * count * (per_contract if cmd.unit == "coins" else 1) != cmd.size:
                said = f"{_plain(cmd.size)} 张" if cmd.unit == "contracts" else f"{_plain(cmd.size)} {spec.ct_val_ccy}"
                sized = f"一共 {said} 平分成 {len(prices)} 档，按最小单位取整后每档 {_contracts_text(each, spec)}"
            if each < spec.min_sz or each <= 0:
                least = spec.min_sz * per_contract
                raise Refused(
                    (f"{sized}。" if sized else "")
                    + f"每档最少 {_plain(spec.min_sz)} 张（{_plain(least)} {spec.ct_val_ccy}），分 {len(prices)} 档放不下。"
                    "少分几档，或者把止损放近一点。"
                )
            sizes = [each] * len(prices)

        coins = each * count * per_contract
        values = [each * per_contract * price for price in prices]
        value = sum(values, Decimal(0))
        loss = gap_sum * each * per_contract
        if max(values) > max_order:
            raise Refused(
                (f"{sized}。" if sized else "")
                + f"最大的一档约 {_amt(max(values))} {settle}，超过了单笔上限 {cfg.trade_max_order_usd:,.0f}。"
                "上限在服务器 .env 的 TRADE_MAX_ORDER_USD，飞书里改不了。"
            )
        total = held_value + queued + value
        if total > max_position:
            raise Refused(
                (f"{sized}。" if sized else "")
                + f"{len(prices)} 档都成交的话，{inst_id} 的{word}单会到约 {_amt(total)} {settle}"
                f"（现有 {_amt(held_value)}，挂单 {_amt(queued)}，这几档 {_amt(value)}），"
                f"超过了单个品种的持仓上限 {cfg.trade_max_position_usd:,.0f}。可以写一个更小的数量。"
                "上限在服务器 .env 的 TRADE_MAX_POSITION_USD，飞书里改不了。"
            )
        if risk_room is not None and loss > risk_room:
            fits = _floor_to(max(risk_room, Decimal(0)) / gap_sum / per_contract, spec.lot_sz)
            most = f"按这个止损每档最多 {_contracts_text(fits, spec)}" if fits >= spec.min_sz and fits > 0 else "按这个止损已经放不下了"
            raise Refused(
                f"账户现在所有止损都打到要亏约 {_amt(exposure.loss)}，占权益 {exposure.loss / equity * 100:.1f}%；"
                f"这 {len(prices)} 档都成交再打到止损还要亏约 {_amt(loss)}，合起来超过你定的账户总风险 {total_cap:g}%（TRADE_MAX_TOTAL_RISK_PCT）。{most}。"
                "可以先平掉或减掉别的仓位、撤掉挂单，或者把止损放近一点。"
            )
        cap = cfg.trade_max_risk_pct
        if cap is not None:
            if equity <= 0:
                raise Refused("读不到账户权益，没法核对单笔止损上限，这次不开仓。请稍后再试。")
            line = equity * _dec(cap) / 100
            if loss > line * (CONFIRM_RISK_SLACK if previous is not None else 1):
                fits = _floor_to(line / gap_sum / per_contract, spec.lot_sz)
                most = f"按这个止损每档最多 {_contracts_text(fits, spec)}" if fits >= spec.min_sz else "按这个止损，每档最小的数量也超了"
                raise Refused(
                    f"{len(prices)} 档都成交再打到止损大约亏 {_amt(loss)} {settle}，占权益 {loss / equity * 100:.1f}%，"
                    f"超过了你设的单笔上限 {cap:g}%（TRADE_MAX_RISK_PCT）。{most}；不写数量的话，我按单笔风险算。"
                )
        self._check_daily()

        td_mode = (same or held)[0].mgn_mode if held else cfg.trade_margin_mode
        pos_side = direction if account.hedge_mode else None
        lever = None
        try:
            lever = self._okx.leverage(inst_id, td_mode)
        except (OkxError, OkxNetworkError) as exc:
            log.warning("could not read the leverage of %s: %s", inst_id, exc)

        lines = [
            f"{len(prices)} 档限价{word}单，每档 {_contracts_text(each, spec)}，合计 {_contracts_text(each * count, spec)}，"
            f"约 {_amt(value)} {settle}（名义价值），全部成交的均价 {_px(average, spec)}"
        ]
        if sized:
            lines.append(sized)
        mode_word = "全仓" if td_mode == "cross" else "逐仓"
        if lever:
            margin = value / _dec(lever)
            lines.append(f"杠杆 {lever:g} 倍（在 OKX 里设的），挂着就会冻结保证金，合计约 {_amt(margin)} {settle}，{mode_word}")
            available = next((c.avail_eq for c in state.snapshot.currencies if c.ccy == settle), None)
            if available is not None and margin > _dec(available):
                lines.append(f"注意：可用保证金只有 {_amt(available)} {settle}，可能不够，OKX 会拒掉后面几档")
        else:
            lines.append(f"{mode_word}，杠杆按 OKX 里的设置")
        near, far = (abs(prices[0] - last) / last * 100), (abs(prices[-1] - last) / last * 100)
        lines.append(f"现价 {_px(last, spec)}，最近一档离现价 {near:.2f}%，最远一档 {far:.2f}%，价格到了才成交")
        for number, price in enumerate(prices, 1):
            lines.append(f"第 {number} 档 限价 {_px(price, spec)}：{_contracts_text(each, spec)}，打到止损亏约 {_amt(abs(price - sl) * each * per_contract)} {settle}")
        lines.append(
            f"止损 {_px(sl, spec)}（每档成交后各自带上；全部成交再打到止损，合计大约亏 {_amt(loss)} {settle}"
            f"{self._risk_share(loss, state)}）"
        )
        if tp is not None:
            gain = sum((abs(tp - price) for price in prices), Decimal(0)) * each * per_contract
            lines.append(f"止盈 {_px(tp, spec)}（每档各自带上；全部成交再打到止盈，合计大约赚 {_amt(gain)} {settle}）")
        else:
            lines.append("止盈 无")
        lines.append(f"全部成交后 {inst_id} 的{word}单约 {_amt(total)} {settle}（上限 {cfg.trade_max_position_usd:,.0f}）")
        lines.extend(self._total_risk_lines(exposure, Decimal(0), loss, equity, settle))
        held_coins = sum((_dec(p.qty) for p in same), Decimal(0)) * per_contract
        mark = _dec(same[0].mark_px or last) if same else last
        liq = self._liquidation_estimate(state, inst_id, direction, td_mode, lever, held_coins, mark, average, coins, equity, value)
        if liq is not None and td_mode == "cross":
            lines.append(f"全部成交后粗估强平价约 {_px(liq, spec)}（全仓，按账户权益亏光算；实际强平会早一点，以 OKX 显示为准）")
            if (long and sl <= liq) or (not long and sl >= liq):
                lines.append("**注意：止损在粗估强平价外面，价格到止损之前可能先强平**")
        lines.append(f"每档成交后各带一张止损，几档都成交后想合成一张整仓止损，发“止损 {base} {_plain(sl)}”")
        track = self._source_line(cmd.source) if previous is None else ""
        if track:
            lines.append(track)

        side = "buy" if long else "sell"
        steps = []
        for number, (price, size) in enumerate(zip(prices, sizes), 1):
            args = {"inst_id": inst_id, "td_mode": td_mode, "side": side, "ord_type": "limit", "sz": _plain(size), "px": _plain(price)}
            if pos_side:
                args["pos_side"] = pos_side
            args["sl_trigger_px"] = _plain(sl)
            if tp is not None:
                args["tp_trigger_px"] = _plain(tp)
            steps.append(Step("place_order", args, f"第 {number} 档 {_plain(price)}", independent=True))
        title = f"分档限价{'加' if same else '做'}{word} {inst_id}（{len(prices)} 档）"
        data = {
            "sizes": [_plain(size) for size in sizes], "tiers": [_plain(price) for price in prices], "ref": _plain(last),
            "positions": self._fingerprint(held),
        }  # fmt: skip
        if sized:
            data["sized"] = sized
        return Plan("open", title, tuple(lines), tuple(steps), inst_id, data)

    def _queued(self, inst_id: str, direction: str, account: AccountConfig, per_contract: Decimal) -> Decimal:
        """What the orders waiting to open or add on this side are worth: limit orders and trigger orders."""
        side = "buy" if direction == "long" else "sell"
        queued = Decimal(0)
        for order in self._okx.pending_orders(inst_id):
            if order.side != side or order.reduce_only or order.px is None:
                continue
            if account.hedge_mode and order.pos_side != direction:
                continue
            queued += _dec(order.remaining) * per_contract * _dec(order.px)
        for row in self._pending_triggers(inst_id):
            if row.get("side") != side or str(row.get("reduceOnly", "")).lower() == "true":
                continue
            if account.hedge_mode and row.get("posSide") != direction:
                continue
            queued += _dec(row.get("sz") or 0) * per_contract * _dec(row.get("triggerPx") or 0)
        return queued

    def _check_daily(self) -> None:
        """TRADE_MAX_OPENS_PER_DAY and TRADE_DAILY_LOSS_LIMIT_USD."""
        cfg = self._cfg
        opens = self._journal.count_opens(self._day_start(), self._clock() + 1, self._mode)
        if opens >= cfg.trade_max_opens_per_day:
            raise Refused(
                f"今天已经开仓 {opens} 次，到了每天 {cfg.trade_max_opens_per_day} 次的上限（TRADE_MAX_OPENS_PER_DAY）。"
                "平仓、撤单、改止损不受影响。"
            )
        loss_limit = cfg.trade_daily_loss_limit_usd
        if loss_limit is not None and self._mode == "live" and self._day_change is not None:
            change = self._day_change()
            if change is not None and change <= -loss_limit:
                raise Refused(
                    f"今天账户权益已经少了 {abs(change):,.2f} USD，到了你设的每日亏损线 {loss_limit:,.0f} USD，今天不再开新仓。"
                    "平仓、撤单、改止损不受影响。"
                )

    def _merged_stop(
        self, position: Position, sl: Decimal, tp: Decimal | None, td_mode: str, pos_side: str | None, spec: InstrumentSpec,
        held_coins: Decimal, coins: Decimal, mark: Decimal, ref: Decimal, settle: str,
    ) -> tuple[list[Step], list[str]]:  # fmt: skip
        """After a market addition: one stop for the whole position, at the new price."""
        olds = "、".join(_px(s.sl_trigger_px, spec) for s in position.stops if s.sl_trigger_px) or "无"
        tps = {s.tp_trigger_px for s in position.stops if s.tp_trigger_px}
        keep_tp = tp if tp is not None else (_dec(next(iter(tps))) if len(tps) == 1 else None)
        lines = [
            f"合成一张止损：加仓成交后，整个仓位（原有 {_plain(held_coins)} + 这次 {_plain(coins)} {spec.ct_val_ccy}）"
            f"都用止损 {_px(sl, spec)}，原来的止损（{olds}）不再用；打到时整个仓位从现在算约亏 "
            f"{_amt(abs(mark - sl) * held_coins + abs(ref - sl) * coins)} {settle}"
        ]
        if tp is None and len(tps) > 1:
            lines.append("原来几张的止盈价不一样，合成后不带止盈；要的话成交后再发“止盈 品种 价格”")
        steps, notes = self._whole_stop_steps(position, sl, keep_tp, tp is not None, td_mode, pos_side, spec)
        lines.extend(notes)
        return steps, lines

    def _whole_stop_steps(
        self, position: Position, sl: Decimal | None, tp: Decimal | None, tp_given: bool, td_mode: str,
        pos_side: str | None, spec: InstrumentSpec,
    ) -> tuple[list[Step], list[str]]:  # fmt: skip
        """The steps that leave the position with one stop for the whole of it, at `sl` and `tp`, whatever stops it has
        now. A whole-position stop that has a stop-loss already is moved, so there is no moment without one; otherwise
        the new one goes on first and the old ones (each covering only the size it came with) come off after. OKX allows
        one whole-position stop at a time, so one without a stop-loss is taken off before the new one goes on."""
        inst_id = position.inst_id
        closing_side = "sell" if position.direction == "long" else "buy"
        old = list(position.stops)
        steps: list[Step] = []
        notes: list[str] = []
        whole_sl = [s for s in old if s.whole and s.sl_trigger_px]
        if sl is not None and whole_sl:
            keep = whole_sl[0]
            change = {"inst_id": inst_id, "algo_id": keep.algo_id, "sl_trigger_px": _plain(sl)}
            if tp_given and tp is not None and keep.tp_trigger_px:
                change["tp_trigger_px"] = _plain(tp)
            elif tp_given and tp is not None:
                notes.append(f"止盈 {_px(tp, spec)} 这次没加上：原来那张整仓单不带止盈，改不出来；之后再发“止盈 {inst_id.split('-')[0]} {_plain(tp)}”")
            steps.append(Step("amend_stop", change, "把整仓止损改到新价位"))
            rest = [s for s in old if s is not keep]
        else:
            whole = [s for s in old if s.whole]
            if whole:
                steps.append(Step("cancel_stops", {"items": [[inst_id, s.algo_id] for s in whole]}, "撤掉原来的整仓止盈止损单"))
            rest = [s for s in old if not s.whole]
            if sl is not None or tp is not None:
                place = {"inst_id": inst_id, "td_mode": position.mgn_mode or td_mode, "side": closing_side}
                if pos_side:
                    place["pos_side"] = pos_side
                if sl is not None:
                    place["sl_trigger_px"] = _plain(sl)
                if tp is not None:
                    place["tp_trigger_px"] = _plain(tp)
                steps.append(Step("place_stop", place, "挂上整个仓位的止盈止损"))
        if rest:
            steps.append(Step("cancel_stops", {"items": [[inst_id, s.algo_id] for s in rest]}, "撤掉原来只管一部分的止盈止损"))
        if steps and steps[0].call != "cancel_stops":
            notes.append("先挂好（或改好）整仓的，再撤旧的，中间不会没有止损")
        return steps, notes

    def _trigger_orders(self, inst_id: str | None = None) -> list[OrderInfo]:
        """The trigger orders waiting, in the shape of ordinary orders (the price is the trigger price), to list and cancel."""
        return [
            OrderInfo(
                ord_id=str(row.get("algoId", "")), inst_id=str(row.get("instId", "")), side=str(row.get("side", "")),
                pos_side=str(row.get("posSide") or "net"), ord_type="trigger", px=float(row["triggerPx"]) if row.get("triggerPx") else None,
                sz=float(row.get("sz") or 0), reduce_only=str(row.get("reduceOnly", "")).lower() == "true",
                cl_ord_id=str(row.get("algoClOrdId", "")), sl_trigger_px=_attached_sl(row),
            )
            for row in self._pending_triggers(inst_id)
        ]  # fmt: skip

    def _pending_triggers(self, inst_id: str | None = None) -> list[dict]:
        """Trigger orders (计划委托) waiting, as OKX sends them; [] when they cannot be read."""
        try:
            rows = self._okx.pending_algos("trigger")
        except (OkxError, OkxNetworkError) as exc:
            log.warning("could not read the trigger orders: %s", exc)
            return []
        return [row for row in rows if isinstance(row, dict) and (not inst_id or row.get("instId") == inst_id)]

    def _exposure(self, state: AccountState) -> Exposure:
        """Every stop on the account and what it would cost: positions, and orders waiting to open."""
        specs: dict[str, Decimal | None] = {}

        def per(inst_id: str) -> Decimal | None:
            if inst_id not in specs:
                try:
                    specs[inst_id] = self._okx.instrument_spec(inst_id).coins_per_contract
                except (OkxError, OkxNetworkError):
                    specs[inst_id] = None
            return specs[inst_id]

        orders = self._okx.pending_orders()
        missing: list[Piece] = []
        try:
            triggers = [row for row in self._okx.pending_algos("trigger") if isinstance(row, dict)]
        except (OkxError, OkxNetworkError) as exc:
            log.warning("could not read the trigger orders: %s", exc)
            triggers, missing = [], [Piece("突破单（计划委托）这次没读到", None)]
        exposure = measure(state, orders, triggers, per)
        exposure.pieces.extend(missing)
        return exposure

    @staticmethod
    def _total_risk_lines(others: Exposure, kept: Decimal, this: Decimal | None, equity: Decimal, settle: str) -> list[str]:
        if this is None:
            return []
        now = others.loss + kept
        total = now + this
        share = f"，占权益 {total / equity * 100:.1f}%" if equity > 0 else ""
        count = len([p for p in others.pieces if p.loss is not None])
        before = f"现有约 {_amt(now)}，" if now > 0 or count else ""
        lines = [f"账户总风险（所有止损都打到）：{before}加上这张约 {_amt(this)}，合计约 {_amt(total)} {settle}{share}"]
        unknown = others.unknown
        if unknown:
            lines.append("没算进去（算不出）：" + "；".join(p.label for p in unknown[:4]))
        return lines

    def _liquidation_estimate(
        self, state: AccountState, inst_id: str, direction: str, td_mode: str, lever: float | None,
        held_coins: Decimal, mark: Decimal, entry: Decimal, coins: Decimal, equity: Decimal, value: Decimal,
    ) -> Decimal | None:  # fmt: skip
        """Where the position would be liquidated, roughly: cross, when the equity is gone (only when this is the
        one instrument with a position); isolated, when this order's margin is gone (only for a new position)."""
        long = direction == "long"
        after = held_coins + coins
        if after <= 0:
            return None
        if td_mode == "cross":
            if any(p.inst_id != inst_id or p.direction != direction for p in state.positions) or equity <= 0:
                return None
            moved = (mark * held_coins + entry * coins - equity) if long else (mark * held_coins + entry * coins + equity)
            estimate = moved / after
        else:
            if held_coins > 0 or not lever:
                return None
            margin = value / _dec(lever)
            estimate = entry - margin / coins if long else entry + margin / coins
        return estimate if estimate > 0 else None

    def _source_line(self, source: str) -> str:
        """How orders that came the same way have done, once there are enough of them to say."""
        if not source:
            return ""
        now = self._clock()
        try:
            return sources.preview_line(self._journal.reviews_between(now - sources.RECORD_DAYS * 86400, now + 1), source)
        except Exception as exc:  # a figure for the preview never stops an order
            log.warning("could not read the record of %s orders: %s", source, exc)
            return ""

    def _risk_sized(
        self, direction: str, ref: Decimal, sl: Decimal | None, entry_name: str, equity: Decimal, spec: InstrumentSpec
    ) -> tuple[Decimal, str]:
        """The size of an opening order given only a stop: as many contracts as the owner's per-trade risk
        (RISK_PER_TRADE_PCT of the equity) allows if the stop is hit. Returns (contracts, how it was worked out)."""
        if sl is None:
            raise Refused("要写数量和单位，或者写止损让我按单笔风险算数量。")
        if spec.ct_type and spec.ct_type != "linear":
            raise Refused("按单笔风险算数量目前只算 USDT 本位合约，请写明数量。")
        if equity <= 0:
            raise Refused("读不到账户权益，没法按单笔风险算数量。请写明数量。")
        self._check_stop_sides(direction, ref, sl, None, entry_name, spec)
        pct = _dec(self._cfg.risk_per_trade_pct)
        budget = equity * pct / 100
        gap = abs(ref - sl)
        settle = spec.settle_ccy or "USDT"
        contracts = _floor_to(budget / gap / spec.coins_per_contract, spec.lot_sz)
        how = (
            f"数量按你定的单笔风险算：权益 {_amt(equity)} USD 的 {float(pct):g}% 是 {_amt(budget)} {settle}，"
            f"止损离{entry_name} {_plain(gap)}，最多开 {_contracts_text(contracts, spec)}"
        )
        if contracts < spec.min_sz or contracts <= 0:
            least = spec.min_sz * spec.coins_per_contract * gap
            raise Refused(
                f"{how}。按这个止损，最小的 {_plain(spec.min_sz)} 张也会亏约 {_amt(least)} {settle}，"
                f"占权益 {least / equity * 100:.2f}%，超过了 {float(pct):g}%。止损要更近，或者这笔不做。"
            )
        return contracts, how

    def _check_max_risk(
        self, contracts: Decimal, ref: Decimal, sl: Decimal | None, entry_name: str, equity: Decimal,
        spec: InstrumentSpec, confirming: bool,
    ) -> None:  # fmt: skip
        """TRADE_MAX_RISK_PCT: an opening order must carry a stop, and the stop may cost at most that share of
        the equity. When the owner confirms, a market order is measured again from the price then, with a
        tenth of the line to spare: the price may move a little between the preview and the confirmation
        (TRADE_MAX_DRIFT_PCT bounds how much), and an order sized right at the line should not fail for it."""
        cap = self._cfg.trade_max_risk_pct
        if cap is None:
            return
        if sl is None:
            raise Refused(
                f"按你的设置（TRADE_MAX_RISK_PCT={cap:g}），开仓必须带止损，止损触发时最多亏权益的 {cap:g}%。"
                "请在指令后面加上“止损 价格”；不写数量的话，我按单笔风险算数量。"
            )
        if equity <= 0:
            raise Refused("读不到账户权益，没法核对单笔止损上限，这次不开仓。请稍后再试。")
        settle = spec.settle_ccy or "USDT"
        per_contract = spec.coins_per_contract
        loss = abs(ref - sl) * contracts * per_contract
        line = equity * _dec(cap) / 100
        allowed = line * CONFIRM_RISK_SLACK if confirming else line
        if loss <= allowed:
            return
        most = _floor_to(line / abs(ref - sl) / per_contract, spec.lot_sz)
        if confirming:
            raise Refused(
                f"价格在你确认前动了，按{entry_name} {_px(ref, spec)} 算，这张单止损触发要亏约 {_amt(loss)} {settle}，"
                f"占权益 {loss / equity * 100:.1f}%，超过了你设的 {cap:g}%（TRADE_MAX_RISK_PCT）。请重新发指令，我按新价格再算。"
            )
        fits = f"按这个止损最多开 {_contracts_text(most, spec)}" if most >= spec.min_sz else "按这个止损，最小的数量也超了"
        raise Refused(
            f"这张单止损触发大约亏 {_amt(loss)} {settle}，占权益 {loss / equity * 100:.1f}%，"
            f"超过了你设的单笔上限 {cap:g}%（TRADE_MAX_RISK_PCT）。{fits}；只写止损不写数量的话，我按单笔风险算数量。"
        )

    @staticmethod
    def _check_stop_sides(
        direction: str, ref: Decimal, sl: Decimal | None, tp: Decimal | None, ref_name: str, spec: InstrumentSpec
    ) -> None:
        long = direction == "long"
        word = _side_word(direction)
        if sl is not None and (sl >= ref if long else sl <= ref):
            raise Refused(
                f"{word}单的止损要{'低' if long else '高'}于{ref_name} {_px(ref, spec)}，你写的是 {_px(sl, spec)}。"
                "照这样挂上去会马上触发。"
            )
        if tp is not None and (tp <= ref if long else tp >= ref):
            raise Refused(
                f"{word}单的止盈要{'高' if long else '低'}于{ref_name} {_px(ref, spec)}，你写的是 {_px(tp, spec)}。"
                "照这样挂上去会马上触发。"
            )

    # ------------------------------------------------------------------ close

    def _target(self, state: AccountState, cmd: Command, what: str) -> Position:
        """The one position a close or a stop command means."""
        allowed = self._cfg.tradable_instruments
        if cmd.inst_id and cmd.inst_id not in allowed:
            raise Refused(
                f"{cmd.inst_id} 不在允许下单的品种里（现在允许：{'、'.join(allowed)}），我不动它的仓位。"
                "请在 OKX 里操作，或者把它加进服务器 .env 的 TRADE_INSTRUMENTS。"
            )
        pool = [p for p in state.positions if p.inst_id in allowed]
        if cmd.inst_id:
            pool = [p for p in pool if p.inst_id == cmd.inst_id]
        if cmd.direction in ("long", "short"):
            pool = [p for p in pool if p.direction == cmd.direction]
        if not pool:
            name = f"{cmd.inst_id} " if cmd.inst_id else ""
            side = f"{_side_word(cmd.direction)}单" if cmd.direction in ("long", "short") else "仓位"
            raise Refused(f"没有找到 {name}的{side}，没有可以{what}的。发“仓位”可以看当前持仓。")
        if len(pool) > 1:
            names = "、".join(f"{p.inst_id} {_side_word(p.direction)}单" for p in pool)
            raise Refused(f"现在有 {len(pool)} 个仓位（{names}），请写明要{what}哪一个，例如：平仓 XAU，或 平多 XAU。")
        return pool[0]

    def _plan_close(self, cmd: Command, previous: Plan | None) -> Plan:
        account, state = self._account()
        if cmd.everything:
            targets = [p for p in state.positions if p.inst_id in self._cfg.tradable_instruments]
            if not targets:
                raise Refused("现在没有可以平的仓位。")
            self._unchanged(previous, targets)
            lines, steps = [], []
            for position in targets:
                spec = self._okx.instrument_spec(position.inst_id)
                lines.append(
                    f"{position.inst_id} {_side_word(position.direction)}单 {_contracts_text(_dec(position.qty), spec)}，"
                    f"浮动盈亏 {self._pnl_text(position, Decimal(1))}"
                )
                steps.append(self._close_step(position, account, independent=True))
            lines.append("都按市价平掉，仓位上的止盈止损和平仓挂单一并撤掉")
            others = len(state.positions) - len(targets)
            if others:
                lines.append(f"另有 {others} 个仓位不在允许下单的品种里，我不动它们")
            title = f"市价平掉全部仓位（{len(targets)} 个）"
            return Plan("close", title, tuple(lines), tuple(steps), "", {"positions": self._fingerprint(targets)})

        position = self._target(state, cmd, "平")
        self._unchanged(previous, [position])
        spec = self._okx.instrument_spec(position.inst_id)
        held = _dec(position.qty)
        word = _side_word(position.direction)
        data = {"positions": self._fingerprint([position])}
        if cmd.size is None:
            contracts = held
        elif previous is not None:
            contracts = Decimal(previous.data["sz"])
        elif cmd.unit == "fraction":
            contracts = _floor_to(held * cmd.size, spec.lot_sz)
        elif cmd.unit == "coins":
            contracts = _floor_to(cmd.size / spec.coins_per_contract, spec.lot_sz)
        else:
            contracts = cmd.size
            if contracts % spec.lot_sz != 0:
                raise Refused(f"{position.inst_id} 的数量要是 {_plain(spec.lot_sz)} 张的整数倍。")
        if contracts > held:
            raise Refused(f"{position.inst_id} {word}单只有 {_plain(held)} 张，平不了 {_plain(contracts)} 张。要全平请发“平仓 {position.inst_id.split('-')[0]}”。")
        if contracts < spec.min_sz:
            raise Refused(f"算下来不足 {_plain(spec.min_sz)} 张，太小了平不了。")
        data["sz"] = _plain(contracts)
        mark = self._price(position.mark_px, spec) if position.mark_px else "未知"
        if contracts == held:
            lines = [
                f"数量 {_contracts_text(held, spec)}，均价 {self._price(position.avg_px or 0, spec)}，标记价 {mark}",
                f"按现价估算，浮动盈亏 {self._pnl_text(position, Decimal(1))}",
                "按市价成交，这个仓位上的止盈止损和平仓挂单一并撤掉",
            ]
            title = f"市价平仓 {position.inst_id} {word}单（全部）"
            return Plan("close", title, tuple(lines), (self._close_step(position, account),), position.inst_id, data)
        lines = [
            f"平掉 {_contracts_text(contracts, spec)}，剩下 {_plain(held - contracts)} 张；标记价 {mark}",
            f"按现价估算，这部分的浮动盈亏 {self._pnl_text(position, contracts / held)}",
            "按市价成交，只减仓不会反手；止盈止损单不动",
        ]
        args = {
            "inst_id": position.inst_id, "td_mode": position.mgn_mode, "side": "sell" if position.direction == "long" else "buy",
            "ord_type": "market", "sz": _plain(contracts), "reduce_only": True,
        }  # fmt: skip
        if account.hedge_mode and position.pos_side in ("long", "short"):
            args["pos_side"] = position.pos_side
            del args["reduce_only"]  # in hedge mode the position side says it; OKX ignores reduceOnly there
        title = f"市价减仓 {position.inst_id} {word}单"
        return Plan("close", title, tuple(lines), (Step("place_order", args, "减仓"),), position.inst_id, data)

    @staticmethod
    def _close_step(position: Position, account: AccountConfig, independent: bool = False) -> Step:
        args = {"inst_id": position.inst_id, "mgn_mode": position.mgn_mode}
        if account.hedge_mode and position.pos_side in ("long", "short"):
            args["pos_side"] = position.pos_side
        label = f"平掉 {position.inst_id} {_side_word(position.direction)}单"
        return Step("close_position", args, label, independent)

    @staticmethod
    def _pnl_text(position: Position, share: Decimal) -> str:
        if position.upl is None:
            return "未知"
        return f"{float(_dec(position.upl) * share):+,.2f} {position.pnl_ccy}"

    # ----------------------------------------------------------------- cancel

    def _plan_cancel(self, cmd: Command, previous: Plan | None) -> Plan:
        self._account()
        allowed = self._cfg.tradable_instruments
        everything = self._okx.pending_orders(cmd.inst_id or None) + self._trigger_orders(cmd.inst_id or None)
        orders = [order for order in everything if order.inst_id in allowed]
        skipped = len(everything) - len(orders)
        if cmd.ord_id:
            by_id = [order for order in orders if order.ord_id == cmd.ord_id]
            if not by_id and len(cmd.ord_id) < 10:  # OKX's ids are longer: a short number may be a price
                by_id = [order for order in orders if _at_price(order, Decimal(cmd.ord_id))]
            orders = by_id
            if not orders:
                raise Refused(f"没有找到订单号 {cmd.ord_id} 的挂单。发“挂单”可以看当前的挂单。")
        elif cmd.inst_id and cmd.inst_id not in allowed:
            raise Refused(f"{cmd.inst_id} 不在允许下单的品种里，我不动它的挂单。请在 OKX 里操作。")
        if cmd.px is not None or cmd.direction:
            matching = [
                order for order in orders
                if (cmd.px is None or _at_price(order, cmd.px))
                and (not cmd.direction or order.side == cmd.direction)
            ]  # fmt: skip
            if not matching and previous is None:
                which = " ".join(
                    part for part in ((f"挂在 {_plain(cmd.px)}" if cmd.px is not None else ""),
                                      {"buy": "的买单", "sell": "的卖单"}.get(cmd.direction, "的单" if cmd.px is not None else "")) if part
                )  # fmt: skip
                listing = "\n".join(f"- {self._order_line(order)}" for order in orders) or "现在没有挂单。"
                raise Refused(f"没有找到{which}。现在的挂单：\n{listing}")
            orders = matching
        if previous is not None:
            wanted = set(previous.data["ids"])
            orders = [order for order in orders if order.ord_id in wanted]
            if not orders:
                raise Refused("预览里的挂单已经不在了（可能已经成交或被撤掉）。发“挂单”可以看现在的情况。")
        if not orders:
            raise Refused("现在没有可以撤的挂单。" + (f"（另有 {skipped} 张在不允许下单的品种上，我不动。）" if skipped else ""))
        if not (cmd.ord_id or cmd.inst_id or cmd.everything or cmd.px is not None or cmd.direction) and len(orders) > 1 and previous is None:
            listing = "\n".join(f"- {self._order_line(order)}" for order in orders)
            raise Refused(f"现在有 {len(orders)} 张挂单，请写明撤哪张：撤单 品种、撤单 订单号，或 撤单 全部。\n{listing}")
        lines = [self._order_line(order) for order in orders]
        if skipped and cmd.everything:
            lines.append(f"另有 {skipped} 张挂单在不允许下单的品种上，我不动它们")
        steps = tuple(
            Step("cancel_stops", {"items": [[order.inst_id, order.ord_id]]}, f"撤突破单 {order.ord_id}", independent=True)
            if order.ord_type == "trigger"
            else Step("cancel_order", {"inst_id": order.inst_id, "ord_id": order.ord_id}, f"撤单 {order.ord_id}", independent=True)
            for order in orders
        )
        inst_id = orders[0].inst_id if len({order.inst_id for order in orders}) == 1 else ""
        return Plan("cancel", f"撤单（{len(orders)} 张）", tuple(lines), steps, inst_id, {"ids": [order.ord_id for order in orders]})

    # ------------------------------------------------------------------ stops

    def _plan_stop(self, cmd: Command, previous: Plan | None) -> Plan:
        account, state = self._account()
        position = self._target(state, cmd, "设止盈止损")
        self._unchanged(previous, [position])
        inst_id = position.inst_id
        spec = self._okx.instrument_spec(inst_id)
        word = _side_word(position.direction)
        clearing = cmd.clear_sl or cmd.clear_tp
        stops = position.stops
        if not stops and clearing:
            raise Refused(f"{inst_id} {word}单上本来就没有止盈止损单。")
        if len(stops) > 1 and not (cmd.clear_sl and cmd.clear_tp):
            return self._plan_merge(cmd, account, position, spec)
        old_sl = _dec(stops[0].sl_trigger_px) if len(stops) == 1 and stops[0].sl_trigger_px else None
        old_tp = _dec(stops[0].tp_trigger_px) if len(stops) == 1 and stops[0].tp_trigger_px else None
        new_sl = None if cmd.clear_sl else (_round_to(cmd.sl, spec.tick_sz) if cmd.sl is not None else old_sl)
        new_tp = None if cmd.clear_tp else (_round_to(cmd.tp, spec.tick_sz) if cmd.tp is not None else old_tp)
        if len(stops) == 1 and not (cmd.clear_sl and cmd.clear_tp):
            if cmd.clear_sl and old_sl is None:
                raise Refused(f"{inst_id} {word}单上没有止损可以取消。")
            if cmd.clear_tp and old_tp is None:
                raise Refused(f"{inst_id} {word}单上没有止盈可以取消。")

        last = _dec(self._okx.ticker(inst_id).last)
        self._check_stop_sides(
            position.direction, last, new_sl if cmd.sl is not None else None, new_tp if cmd.tp is not None else None, "现价", spec
        )
        closing_side = "sell" if position.direction == "long" else "buy"
        pos_side = position.pos_side if account.hedge_mode and position.pos_side in ("long", "short") else None
        cancel_all = Step("cancel_stops", {"items": [[inst_id, stop.algo_id] for stop in stops]}, "撤掉原来的止盈止损单")
        place_args = {"inst_id": inst_id, "td_mode": position.mgn_mode, "side": closing_side}
        if pos_side:
            place_args["pos_side"] = pos_side
        if new_sl is not None:
            place_args["sl_trigger_px"] = _plain(new_sl)
        if new_tp is not None:
            place_args["tp_trigger_px"] = _plain(new_tp)
        place = Step("place_stop", place_args, "挂上新的止盈止损单")

        notes: list[str] = []
        if new_sl is None and new_tp is None:
            steps = (cancel_all,)
            title = f"取消止盈止损 {inst_id} {word}单"
        elif not stops:
            steps = (place,)
            title = f"设置止盈止损 {inst_id} {word}单"
            notes.append("触发后按市价平掉整个仓位")
        elif (new_sl is None) == (old_sl is None) and (new_tp is None) == (old_tp is None):
            changes = {"inst_id": inst_id, "algo_id": stops[0].algo_id}
            if new_sl != old_sl:
                changes["sl_trigger_px"] = _plain(new_sl)
            if new_tp != old_tp:
                changes["tp_trigger_px"] = _plain(new_tp)
            if len(changes) == 2:
                raise Refused("止盈止损已经是这个价位了，不用改。")
            steps = (Step("amend_stop", changes, "修改止盈止损单"),)
            title = f"修改止盈止损 {inst_id} {word}单"
        else:
            steps = (cancel_all, place)
            title = f"修改止盈止损 {inst_id} {word}单"
            notes.append("要先撤掉原来的那张再挂新的，中间有一两秒这个仓位没有止盈止损")
            notes.append("触发后按市价平掉整个仓位")

        coins = _dec(position.qty) * spec.coins_per_contract
        average = _dec(position.avg_px) if position.avg_px else None
        equity = _dec(state.snapshot.total_eq) if state.snapshot.total_eq else None
        lines = [
            f"仓位 {_contracts_text(_dec(position.qty), spec)}，均价 {self._price(position.avg_px or 0, spec)}，现价 {_px(last, spec)}",
            self._stop_line("止损", old_sl, new_sl, last, average, coins, position, spec, equity),
            self._stop_line("止盈", old_tp, new_tp, last, average, coins, position, spec, equity),
            *notes,
        ]
        return Plan("stop", title, tuple(lines), steps, inst_id, {"positions": self._fingerprint([position])})

    def _plan_merge(self, cmd: Command, account: AccountConfig, position: Position, spec: InstrumentSpec) -> Plan:
        """A position with several stops (each addition brought its own): one stop for the whole of it."""
        inst_id = position.inst_id
        base = inst_id.split("-")[0]
        word = _side_word(position.direction)
        stops = position.stops
        sls = sorted({s.sl_trigger_px for s in stops if s.sl_trigger_px})
        tps = sorted({s.tp_trigger_px for s in stops if s.tp_trigger_px})
        if cmd.clear_sl:
            new_sl = None
        elif cmd.sl is not None:
            new_sl = _round_to(cmd.sl, spec.tick_sz)
        elif len(sls) == 1:
            new_sl = _dec(sls[0])
        elif sls:
            raise Refused(
                f"{inst_id} {word}单上有 {len(stops)} 张止盈止损单，止损价不一样（{'、'.join(self._price(v, spec) for v in sls)}）。"
                f"要合成一张，请一起写上止损：止损 {base} 价格" + (f" 止盈 {_plain(_dec(cmd.tp))}" if cmd.tp is not None else "")
            )
        else:
            new_sl = None
        notes: list[str] = []
        if cmd.clear_tp:
            new_tp = None
        elif cmd.tp is not None:
            new_tp = _round_to(cmd.tp, spec.tick_sz)
        elif len(tps) == 1:
            new_tp = _dec(tps[0])
        else:
            new_tp = None
            if tps:
                notes.append(f"原来几张的止盈不一样（{'、'.join(self._price(v, spec) for v in tps)}），合成后不带止盈；要的话再发“止盈 {base} 价格”")
        last = _dec(self._okx.ticker(inst_id).last)
        self._check_stop_sides(position.direction, last, new_sl, new_tp, "现价", spec)
        pos_side = position.pos_side if account.hedge_mode and position.pos_side in ("long", "short") else None
        steps, more = self._whole_stop_steps(position, new_sl, new_tp, cmd.tp is not None, position.mgn_mode, pos_side, spec)
        coins = _dec(position.qty) * spec.coins_per_contract
        average = _dec(position.avg_px) if position.avg_px else None
        old_text = "；".join(
            "、".join(part for part in (
                f"止损 {self._price(s.sl_trigger_px, spec)}" if s.sl_trigger_px else "",
                f"止盈 {self._price(s.tp_trigger_px, spec)}" if s.tp_trigger_px else "",
            ) if part) + (f"（管 {_plain(_dec(s.size) * spec.coins_per_contract)} {spec.ct_val_ccy}）" if s.size and not s.whole else "（整个仓位）")
            for s in stops
        )  # fmt: skip
        lines = [
            f"仓位 {_contracts_text(_dec(position.qty), spec)}，均价 {self._price(position.avg_px or 0, spec)}，现价 {_px(last, spec)}",
            f"现在有 {len(stops)} 张：{old_text}",
            f"合成一张，管整个仓位，触发后按市价全部平掉：",
            self._stop_line("止损", None, new_sl, last, average, coins, position, spec, None).replace("止损 无 → ", "止损 "),
            self._stop_line("止盈", None, new_tp, last, average, coins, position, spec, None).replace("止盈 无 → ", "止盈 "),
            *notes,
            *more,
        ]
        title = f"合并止盈止损 {inst_id} {word}单"
        return Plan("stop", title, tuple(line for line in lines if line), tuple(steps), inst_id, {"positions": self._fingerprint([position])})

    @staticmethod
    def _stop_line(
        name: str, old: Decimal | None, new: Decimal | None, last: Decimal, average: Decimal | None, coins: Decimal,
        position: Position, spec: InstrumentSpec, equity: Decimal | None = None,
    ) -> str:  # fmt: skip
        """Two figures, each saying where it counts from: from now (what the account would lose or gain
        from here) and from the entry (what the whole trade would come to). Side by side without that
        they look like a sum done twice with two answers."""
        before = "无" if old is None else _px(old, spec)
        if new == old:
            return f"{name} {before}（不变）"
        if new is None:
            return f"{name} {before} → 取消"
        sign = 1 if position.direction == "long" else -1
        ccy = position.pnl_ccy
        from_now = (new - last) * coins * sign
        share = f"，占权益 {abs(from_now) / equity * 100:.1f}%" if equity else ""
        text = (
            f"{name} {before} → {_px(new, spec)}（离现价 {abs(new - last) / last * 100:.2f}%；"
            f"触发时账户比现在{'少' if from_now < 0 else '多'}约 {_amt(abs(from_now))} {ccy}{share}"
        )
        if average is not None:
            result = (new - average) * coins * sign
            text += f"；从开仓均价算，这笔交易 {float(result):+,.2f} {ccy}"
        return text + "）"

    def _risk_share(self, loss: Decimal, state: AccountState) -> str:
        """", 占权益 x%, 你定的单笔风险是 y%" for an opening order's stop: the same yardstick as 算仓位."""
        equity = _dec(state.snapshot.total_eq or 0)
        if equity <= 0:
            return ""
        share = loss / equity * 100
        rule = _dec(self._cfg.risk_per_trade_pct)
        verdict = "超过了" if share > rule else "没超过"
        return f"，占权益 {share:.1f}%，{verdict}你定的单笔风险 {float(rule):g}%"

    # -------------------------------------------------------------- execution

    def _execute(self, plan: Plan, source: str = "") -> Reply:
        """Sends the steps of a confirmed plan and reports, truthfully, what happened."""
        _, heading, template, lines = self._carry_out(plan, source=source)
        body = "\n".join([f"**{plan.title}**", *(f"- {line}" for line in lines)])
        return Reply(body, title=f"{heading} · {MODE_LABELS[self._mode]}", template=template)

    def _carry_out(self, plan: Plan, in_diary: bool = True, source: str = "") -> tuple[str, str, str, list[str]]:
        """Sends the steps and keeps the record. Returns (ok | rejected | partial | unknown, heading, colour, lines).
        Every plan goes into the order record; `in_diary` says whether it is also a line of the day's events."""
        lines: list[str] = []
        done = failed = 0
        unknown = False
        ladder = bool(plan.data.get("tiers"))
        for step in plan.steps:
            try:
                said = self._run(step)
                lines.extend([f"{step.label}：{said[0]}", *said[1:]] if ladder and said else said)
                done += 1
            except OkxError as exc:
                failed += 1
                reason = f"{exc.message or '没有给出原因'}（代码 {exc.code}）"
                lines.append(f"{step.label}：OKX 拒绝了，{reason}" if len(plan.steps) > 1 else f"OKX 拒绝了：{reason}")
                if not step.independent:
                    break
            except _Lost as lost:
                unknown = True
                lines.append(str(lost))
                break
        if unknown:
            result, heading, template = "unknown", "结果不确定", "red"
        elif failed and done:
            result, heading, template = "partial", "只执行了一部分", "yellow"
        elif failed:
            result, heading, template = "rejected", "没有执行", "grey"
        else:
            result, heading, template = "ok", "已执行", "green"
        if plan.action == "stop" and result != "ok" and done and plan.steps[0].call == "cancel_stops" and len(plan.steps) > 1:
            lines.append("**注意：原来的止盈止损已经撤掉，新的没有挂上。现在这个仓位没有止盈止损，请马上重新设一次。**")
        if plan.action == "open" and result in ("partial", "unknown") and done and len(plan.steps) > 1 and not ladder:
            lines.append("**注意：加仓单已经下了，但整仓止损没有全部做完。请马上发“仓位”看止损，再发“止损 品种 价格”补上。**")
        if result in ("unknown", "partial"):
            lines.append("请发“仓位”和“挂单”核对实际情况。我不会自动重试。")
        now = self._clock()
        self._journal.record_order(
            self._mode, plan.action, plan.title, result, plan.inst_id,
            request=[{"call": step.call, **step.args} for step in plan.steps], detail="\n".join(lines), ts=now,
            source=source,
        )  # fmt: skip
        if in_diary:
            tag = "[模拟盘] " if self._mode == "demo" else ""
            self._journal.record_event("trade", f"{tag}{plan.title}：{heading}", plan.inst_id, ts=now)
        log.info("order (%s) %s %s: %s", self._mode, plan.action, plan.inst_id or "-", result)
        return result, heading, template, lines

    def _run(self, step: Step) -> list[str]:
        args = dict(step.args)
        if step.call == "place_order":
            return self._send_order(step, args)
        if step.call == "close_position":
            return self._send_close(step, args)
        try:
            if step.call == "cancel_order":
                self._trader.cancel_order(args["inst_id"], args["ord_id"])
                return [f"已撤：订单号 {args['ord_id']}"]
            if step.call == "cancel_stops":
                self._trader.cancel_stops([tuple(item) for item in args["items"]])
                return [f"已撤掉 {len(args['items'])} 张止盈止损单"]
            if step.call == "amend_stop":
                self._trader.amend_stop(**args)
                return [f"已改好：{self._stops_text(args)}"]
            if step.call == "place_trigger":
                algo_id = self._trader.place_trigger(cl_ord_id=self._new_id(), **args)
                spec = self._spec_or_none(args["inst_id"])
                long = args["side"] == "buy"
                stops = self._stops_text(args)
                return [
                    f"已挂上突破单：价格{'涨到' if long else '跌到'} {self._price(args['trigger_px'], spec)} 时按市价{'做多' if long else '做空'}"
                    + (f"，成交后带{stops}" if stops else ""),
                    f"计划委托单号 {algo_id}",
                ]
            if step.call == "place_stop":
                algo_id = self._trader.place_stop(cl_ord_id=self._new_id(), **args)
                return [f"已挂上：{self._stops_text(args)}，触发后按市价平掉整个仓位", f"止盈止损单号 {algo_id}"]
        except OkxNetworkError:
            raise _Lost(f"{step.label}：发给 OKX 的时候连接断了，不确定有没有生效。") from None
        raise ValueError(f"unknown step {step.call}")

    def _stops_text(self, args: dict) -> str:
        spec = self._spec_or_none(args["inst_id"])
        parts = []
        if args.get("sl_trigger_px"):
            parts.append(f"止损 {self._price(args['sl_trigger_px'], spec)}")
        if args.get("tp_trigger_px"):
            parts.append(f"止盈 {self._price(args['tp_trigger_px'], spec)}")
        return "，".join(parts)

    def _send_order(self, step: Step, args: dict) -> list[str]:
        inst_id = args["inst_id"]
        client_id = self._new_id()
        try:
            order_id = self._trader.place_order(cl_ord_id=client_id, **args)
        except OkxNetworkError:
            order = self._look_up(inst_id, client_id)  # raises _Lost when it cannot tell
            order_id = order.ord_id
        else:
            order = self._read_order(inst_id, order_id=order_id)
        lines = self._fill_lines(order, inst_id, order_id)
        if args.get("sl_trigger_px") or args.get("tp_trigger_px"):
            lines.append(f"随单带的{self._stops_text(args)}，成交后自动生效")
        return lines

    def _send_close(self, step: Step, args: dict) -> list[str]:
        inst_id = args["inst_id"]
        client_id = self._new_id()
        try:
            self._trader.close_position(inst_id, args["mgn_mode"], args.get("pos_side"), cl_ord_id=client_id)
        except OkxNetworkError:
            if self._still_open(inst_id, args.get("pos_side")) is False:
                return [f"{step.label}：连接断了一下，但仓位已经不在了，平仓生效了"]
            raise _Lost(f"{step.label}：发给 OKX 的时候连接断了，不确定平仓有没有生效。") from None
        order = self._read_order(inst_id, client_id=client_id)
        if order is not None:
            return [f"{step.label}：" + "；".join(self._fill_lines(order, inst_id, order.ord_id))]
        gone = self._still_open(inst_id, args.get("pos_side")) is False
        return [f"{step.label}：{'仓位已经平掉' if gone else 'OKX 已接受平仓指令，稍后发“仓位”确认'}"]

    def _read_order(self, inst_id: str, order_id: str = "", client_id: str = "") -> OrderInfo | None:
        """The order a moment after it was sent, so the report can say whether it filled."""
        self._sleep(1.0)
        try:
            return self._okx.order(inst_id, ord_id=order_id, cl_ord_id=client_id)
        except (OkxError, OkxNetworkError) as exc:
            log.warning("could not read back an order on %s: %s", inst_id, exc)
            return None

    def _look_up(self, inst_id: str, client_id: str) -> OrderInfo:
        """After a broken connection: did the order arrive? Never answered by sending it again."""
        for _ in range(3):
            self._sleep(1.0)
            try:
                return self._okx.order(inst_id, cl_ord_id=client_id)
            except OkxError as exc:
                if exc.code in ORDER_NOT_FOUND_CODES:
                    raise _Lost(
                        "发单的时候连接断了，之后 OKX 那边查不到这张单，多半没有下出去。"
                        "请先核对，再决定要不要重新下。"
                    ) from None
            except OkxNetworkError:
                continue
        raise _Lost("发单的时候连接断了，不确定这张单有没有到 OKX。") from None

    def _still_open(self, inst_id: str, pos_side: str | None) -> bool | None:
        """Whether the position is still there. None when that cannot be read."""
        self._sleep(1.0)
        try:
            positions = self._okx.account_state().positions
        except (OkxError, OkxNetworkError):
            return None
        return any(p.inst_id == inst_id and (not pos_side or p.pos_side == pos_side) for p in positions)

    def _fill_lines(self, order: OrderInfo | None, inst_id: str, order_id: str) -> list[str]:
        if order is None:
            return [f"OKX 已接受这张单（订单号 {order_id}），成交情况暂时没查到，发“仓位”或“挂单”可以看"]
        spec = self._spec_or_none(inst_id)
        filled = _contracts_text(_dec(order.filled_sz), spec) if spec is not None else f"{order.filled_sz:g} 张"
        average = self._price(order.avg_px, spec) if order.avg_px else "未知"
        if order.state == "filled":
            lines = [f"已成交 {filled}，均价 {average}"]
        elif order.state == "partially_filled":
            lines = [f"部分成交：{filled}，均价 {average}；剩下 {order.remaining:g} 张还挂着"]
        elif order.state == "live":
            price = f"限价 {self._price(order.px, spec)}" if order.px is not None else "市价"
            lines = [f"已挂单，等待成交：{price}，{order.sz:g} 张"]
        elif order.state == "canceled":
            lines = [f"订单被 OKX 撤销了，成交了 {order.filled_sz:g} 张"]
        else:
            lines = [f"订单状态：{order.state or '未知'}"]
        if order.pnl:
            ccy = spec.settle_ccy if spec is not None else ""
            lines.append(f"这笔的盈亏 {order.pnl:+,.2f} {ccy}（没扣手续费）".replace("  ", " "))
        lines.append(f"订单号 {order.ord_id or order_id}")
        return lines


class _Lost(Exception):
    """The connection broke while a request was on its way: whether it took effect is not known."""
