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
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

from .config import Config
from .instruments import ALIASES, InstrumentResolver
from .journal import Journal
from .models import AccountConfig, AccountState, InstrumentSpec, OrderInfo, Position
from .okx_client import OkxError, OkxNetworkError
from .reports import currency_lines

log = logging.getLogger(__name__)

MODE_LABELS = {"off": "已关闭", "demo": "模拟盘", "live": "实盘"}
PAUSED_SETTING = "trading_paused"
MAX_COMMAND_CHARS = 80
MAX_WRONG_CODES = 3
FAR_LIMIT_PCT = Decimal(30)  # a resting limit price further than this from the market is taken for a typo
ACCOUNT_CONFIG_TTL = 600.0
ORDER_NOT_FOUND_CODES = {"51603"}  # OKX: the order does not exist

# A message with any of these is talk about trading, not an order.
QUESTION_MARKS = (
    "?", "怎么", "吗", "如何", "能不能", "该不该", "要不要", "是否", "为什么", "呢", "哪", "什么", "多少", "还是",
    "建议", "分析", "觉得", "合适", "好不好", "行不行", "可以", "应该", "会不会",
)  # fmt: skip

NUM = r"\d+(?:\.\d+)?"
_CONFIRM = re.compile(r"(?:/confirm|确认|确定)\s*(\d{1,8})?")
_POLITE = re.compile(r"^(?:请|麻烦|帮我|给我|帮忙)+\s*")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+){0,2}")
_NOT_INSTRUMENTS = {"u", "usdt", "usdc", "usd", "sl", "tp", "oz", "all"}
_JOIN = r"(?:价格|价|设在|设为|设到|改到|改为|改成|移到|在|到|为|:|=)?"
_SL = re.compile(rf"(?:止损|(?<![a-z0-9])sl)\s*{_JOIN}\s*({NUM})", re.I)
_TP = re.compile(rf"(?:止盈|(?<![a-z0-9])tp)\s*{_JOIN}\s*({NUM})", re.I)
_PX = re.compile(rf"(?:限价|价格|价位|挂在|挂到|挂|在|@)\s*[:=]?\s*({NUM})")
_BARE_NUMBER = re.compile(NUM)
_THOUSANDS = re.compile(r"\d,\d{3}(?!\d)")
_FILLER = re.compile(r"的仓位|仓位|的单子|单子|开仓|数量|现价|永续|合约|的|仓|单")
_STOP_START = re.compile(r"^(?:设置|设|修改|改|移动|调整)?\s*(?=止损|止盈)|^/(?=sl|tp)", re.I)
_FRACTION_WORDS = (("一半", Decimal("0.5")), ("半仓", Decimal("0.5")), ("三分之一", Decimal(1) / 3), ("四分之一", Decimal("0.25")))
_EVERYTHING_WORDS = ("全部", "所有", "全平")

ABORT_WORDS = ("取消", "不要了", "算了", "取消下单", "/abort")
PAUSE_WORDS = ("停止交易", "暂停交易", "停止下单", "暂停下单", "/stoptrading")
RESUME_WORDS = ("恢复交易", "恢复下单", "/resumetrading")
SETTINGS_WORDS = ("交易设置", "交易状态", "下单设置", "下单状态", "/trading")
HELP_WORDS = ("交易帮助", "下单帮助", "怎么下单", "下单指令", "/tradehelp")
ORDERS_WORDS = ("查挂单", "当前挂单", "当前委托", "挂单", "委托", "/orders")

_TYPE_PREFIXES = (("市价", "market"), ("限价", "limit"), ("挂单", "limit"))
_OPEN_VERBS = (
    ("/long", "long", ""), ("/short", "short", ""),
    ("做多", "long", ""), ("开多", "long", ""), ("买入", "long", ""), ("买多", "long", ""), ("买涨", "long", ""),
    ("做空", "short", ""), ("开空", "short", ""), ("卖空", "short", ""), ("买跌", "short", ""),
    ("挂多", "long", "limit"), ("挂空", "short", "limit"),
    ("加仓", "same", ""),
)  # fmt: skip
_CLOSE_ALL_VERBS = ("全部平仓", "平掉全部", "平掉所有", "全平", "清仓", "/closeall")
_CLOSE_VERBS = (("平多", "long"), ("平空", "short"), ("平仓", ""), ("平掉", ""), ("/close", ""))
_CANCEL_VERBS = ("撤单", "撤销挂单", "取消挂单", "撤掉挂单", "撤销委托", "取消委托", "/cancel")
_CLEAR_STOP_VERBS = (
    ("取消止盈止损", True, True), ("取消止损止盈", True, True), ("撤销止盈止损", True, True), ("撤销止损止盈", True, True),
    ("取消止损", True, False), ("撤销止损", True, False), ("取消止盈", False, True), ("撤销止盈", False, True),
)  # fmt: skip

HELP_TEXT = """**开仓**（只做 USDT 本位永续合约）
- 市价：“做多 XAU 100张”“做空 BTC 0.01个”“做多 XAU 500U”
- 限价挂单：“限价做多 XAU 100张 3850”“挂单做空 BTC 0.01个 68000”
- 随单带止损止盈：在后面加“止损 3860 止盈 3950”
- 数量必须带单位：“张”是合约张数，“个”是币的数量（黄金就是盎司），“U”是仓位的名义价值，不是保证金
- “加仓 XAU 100张”：按现有仓位的方向再开

**平仓**（都按市价）
- “平仓 XAU”平掉整个仓位；“平仓 XAU 一半”“平仓 XAU 30%”“减仓 XAU 100张”平一部分
- “全部平仓”平掉所有仓位

**挂单**
- “挂单”看当前的挂单和止盈止损
- “撤单 XAU”“撤单 全部”“撤单 订单号”

**止盈止损**（对整个仓位）
- “止损 XAU 3860”“止盈 XAU 3950”“止损 XAU 3860 止盈 3950”
- “取消止损 XAU”“取消止盈 XAU”“取消止盈止损 XAU”

**安全**
- 每条指令我先回一张预览，你回复“确认 四位数”才执行，回复“取消”作废，过期也作废
- “停止交易”：立刻不再接受开仓（平仓、撤单、改止损照常）。“恢复交易”解除
- “交易设置”：看当前是模拟盘还是实盘、各项限额、这把 key 的权限
- 限额和品种名单只能在服务器的 .env 里改，飞书里改不了。杠杆倍数请在 OKX 里设"""


# --------------------------------------------------------------------------- commands


@dataclass(frozen=True)
class Command:
    """What the owner asked for, as far as the words alone say it."""

    kind: str  # open | close | cancel | stop | orders | pause | resume | settings | help | confirm | abort | unclear
    inst_id: str = ""
    direction: str = ""  # open: long | short | same (加仓). close: long | short | "" (whichever there is)
    ord_type: str = ""  # market | limit
    size: Decimal | None = None
    unit: str = ""  # contracts | coins | usd | fraction
    px: Decimal | None = None
    sl: Decimal | None = None
    tp: Decimal | None = None
    clear_sl: bool = False
    clear_tp: bool = False
    ord_id: str = ""
    everything: bool = False  # every position / every open order
    reduce: bool = False  # 减仓: a part of the position, never all of it by default
    code: str = ""
    note: str = ""  # unclear: what to tell the owner


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
            (ABORT_WORDS, "abort"), (PAUSE_WORDS, "pause"), (RESUME_WORDS, "resume"),
            (SETTINGS_WORDS, "settings"), (HELP_WORDS, "help"),
        ):  # fmt: skip
            if lowered in words:
                return Command(kind)
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
        ord_type = ""
        for word, kind in _TYPE_PREFIXES:
            if text.startswith(word):
                ord_type, text = kind, text[len(word) :].lstrip()
                break
        lowered = text.lower()
        if not ord_type:
            for word, clear_sl, clear_tp in _CLEAR_STOP_VERBS:
                if text.startswith(word):
                    return self._clear_stops(text[len(word) :], clear_sl, clear_tp)
            for word in _CANCEL_VERBS:
                if lowered.startswith(word):
                    return self._cancel(text[len(word) :])
            if _STOP_START.match(text):
                return self._stops(_STOP_START.sub("", text, count=1))
        for word, direction, forced in _OPEN_VERBS:
            if lowered.startswith(word):
                return self._open(direction, forced or ord_type, text[len(word) :])
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

    def _open(self, direction: str, ord_type: str, rest: str) -> Command | None:
        _, ids = self._without_instruments(rest)
        if not re.search(r"\d", rest):
            # "做多情绪很浓" is talk. "做多 BTC" is an order with the size left out.
            if ids and len(rest) <= 24:
                raise _Unclear("要写数量和单位，例如：做多 XAU 100张、做多 BTC 0.01个，或 做多 XAU 500U。")
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
            raise _Unclear("要写数量和单位，例如：100张、0.01个，或 500U。")
        if unit == "fraction":
            raise _Unclear("开仓的数量请用 张、个 或 U，不能用百分比。")
        if px is not None and px <= 0:
            raise _Unclear("价格要大于 0。")
        if ord_type == "market" and px is not None:
            raise _Unclear("市价单不用写价格。想挂在某个价位，请用“限价做多 品种 数量 价格”。")
        if ord_type == "limit" and px is None:
            raise _Unclear("限价单要写价格，例如：限价做多 XAU 100张 3850。")
        return Command(
            "open", inst_id=inst_id, direction=direction, ord_type=ord_type or ("limit" if px is not None else "market"),
            size=size, unit=unit, px=px, sl=sl, tp=tp,
        )  # fmt: skip

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
        if re.fullmatch(r"\d{6,}", rest):
            return Command("cancel", ord_id=rest)
        work, ids = self._without_instruments(rest)
        leftover = _FILLER.sub(" ", work.replace("挂单", " ")).strip()
        if len(ids) == 1 and not leftover:
            return Command("cancel", inst_id=ids[0])
        if len(rest) > 12:
            return None
        raise _Unclear("撤哪张单没看懂。可以发：撤单 XAU、撤单 全部，或 撤单 订单号。")

    def _stops(self, text: str) -> Command | None:
        work, ids = self._without_instruments(text)
        if not re.search(r"\d", work):
            # "止损被扫了" is talk. "止损 XAU" is a command with the price left out.
            if ids and len(text) <= 24:
                raise _Unclear("要写价格，例如：止损 XAU 3860，或 止损 XAU 3860 止盈 3950。")
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
            raise _Unclear("OKX 上没有“手”这个单位。请用 张（合约张数）、个（币的数量）或 U（名义价值）。")
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


class Refused(Exception):
    """A check said no. The message is for the owner, in plain words."""


@dataclass(frozen=True)
class Step:
    """One request to OKX."""

    call: str  # place_order | close_position | cancel_order | place_stop | amend_stop | cancel_stops
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
        self._new_code = new_code or (lambda: f"{secrets.randbelow(9000) + 1000}")
        self._new_id = new_id or (lambda: "ftb" + uuid.uuid4().hex[:24])
        self._day_change = day_change
        self._parser = CommandParser(resolver or InstrumentResolver(reader, clock), lambda: cfg.tradable_instruments)
        self._lock = threading.Lock()  # guards the pending confirmation
        self._sending = threading.Lock()  # one order at a time
        self._pending: _Pending | None = None
        self._account_config: tuple[float, AccountConfig] | None = None

    @property
    def mode(self) -> str:
        return self._mode

    def is_command(self, text: str) -> bool:
        return self._parser.parse(text) is not None

    # ------------------------------------------------------------------ entry

    def handle(self, text: str, private: bool = True) -> Reply | None:
        """Answers an order command. None when the message is not one."""
        command = self._parser.parse(text)
        if command is None:
            return None
        if not private:
            return Reply("下单、平仓、撤单这类指令请在私聊里发给我，群里不执行。")
        try:
            return self._dispatch(command)
        except Refused as exc:
            return Reply(str(exc))
        except OkxError as exc:
            return Reply(f"OKX 返回了错误，没有执行任何操作：{exc.message or '没有给出原因'}（代码 {exc.code}）")
        except OkxNetworkError:
            return Reply("现在连不上 OKX，没有执行任何操作。请稍后再试。")

    def _dispatch(self, command: Command) -> Reply:
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
            return self._abort()
        if self._mode == "off":
            return Reply(self._off_text())
        if kind == "unclear":
            return Reply(f"{command.note}\n没有执行任何操作。发“交易帮助”可以看全部写法。")
        plan = self._plan(command)
        code = self._new_code()
        seconds = self._cfg.trade_confirm_seconds
        with self._lock:
            self._pending = _Pending(code, self._clock() + seconds, command, plan)
        body = [f"**{plan.title}**", *(f"- {line}" for line in plan.lines), ""]
        body.append(f"回复 **确认 {code}** 执行，回复“取消”作废。{seconds} 秒内有效，过期要重新发指令。")
        live = self._mode == "live"
        return Reply("\n".join(body), title=f"下单预览 · {MODE_LABELS[self._mode]}", template="red" if live else "orange")

    # ----------------------------------------------------------- confirmation

    def _confirm(self, command: Command) -> Reply:
        with self._lock:
            pending = self._pending
            if pending is None:
                return Reply("现在没有等待确认的指令。")
            if self._clock() > pending.expires_at:
                self._pending = None
                return Reply("这个预览已经过期了，没有执行。请重新发指令。")
            if not command.code:
                return Reply("请带上预览卡片里的四位确认码，例如：确认 1234。")
            if command.code != pending.code:
                pending.wrong += 1
                if pending.wrong >= MAX_WRONG_CODES:
                    self._pending = None
                    return Reply("确认码连续错了三次，这个预览已作废，没有执行。请重新发指令。")
                return Reply("确认码不对，没有执行。请核对预览卡片上的四位数。")
            self._pending = None  # a code works once
        with self._sending:
            try:
                plan = self._plan(pending.command, previous=pending.plan)
            except Refused as exc:
                return Reply(f"没有执行。{exc}")
            return self._execute(plan)

    def _abort(self) -> Reply:
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return Reply("现在没有等待确认的指令。")
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

    # --------------------------------------------------------------- settings

    def _mode_line(self) -> str:
        if self._mode == "off":
            return "下单功能关闭（TRADING=off），发指令不会执行。"
        paused = "，开仓已暂停" if self._paused() else ""
        return f"下单功能已打开，{MODE_LABELS[self._mode]}{paused}。"

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
        lines.append(f"**允许下单的品种**：{'、'.join(cfg.tradable_instruments)}")
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
        lines.append(f"- 新仓位用{'全仓' if cfg.trade_margin_mode == 'cross' else '逐仓'}；开仓{'必须' if cfg.trade_require_stop else '不强制'}带止损")
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
        lines.append("限额和品种名单只能在服务器的 .env 里改，飞书里改不了。")
        return lines

    def _spot_mode_text(self) -> str:
        where = "OKX 的模拟交易里" if self._mode == "demo" else "OKX 里"
        return (
            f"这个{self._account_label()}账户在 OKX 里是“现货模式”，这个模式下 OKX 不接受合约订单，所以开不了仓。"
            f"请在 {where}打开交易页的设置，把“账户模式”改成“现货和合约模式”（第一次只能在网页或 App 里改），改完重发指令就行。"
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
        orders = self._okx.pending_orders(command.inst_id or None)
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

    def _order_line(self, order: OrderInfo) -> str:
        spec = self._spec_or_none(order.inst_id)
        side = "买入" if order.side == "buy" else "卖出"
        if order.reduce_only:
            side += "（只减仓）"
        price = "市价" if order.px is None else f"限价 {self._price(order.px, spec)}"
        size = f"{order.sz:g} 张"
        if spec is not None:
            size = _contracts_text(_dec(order.sz), spec)
        filled = f"，已成交 {order.filled_sz:g} 张" if order.filled_sz else ""
        return f"{order.inst_id} {side} {price}，{size}{filled}，订单号 {order.ord_id}"

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

    def _plan_open(self, cmd: Command, previous: Plan | None) -> Plan:
        cfg = self._cfg
        if self._paused():
            raise Refused("交易已暂停（你发过“停止交易”），现在不接受开仓指令。发“恢复交易”解除。平仓、撤单、改止损不受影响。")
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

        last = _dec(self._okx.ticker(inst_id).last)
        settle = spec.settle_ccy or "USDT"
        px: Decimal | None = None
        ref = last
        if cmd.ord_type == "limit":
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
        if previous is not None:
            contracts = Decimal(previous.data["sz"])
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

        if value > _dec(cfg.trade_max_order_usd):
            raise Refused(
                f"这张单约 {_amt(value)} {settle}，超过了单笔上限 {cfg.trade_max_order_usd:,.0f}。"
                "上限在服务器 .env 的 TRADE_MAX_ORDER_USD，飞书里改不了。"
            )
        side = "buy" if long else "sell"
        held_value = sum((self._value(p, spec) for p in same), Decimal(0))
        queued = Decimal(0)
        for order in self._okx.pending_orders(inst_id):
            if order.side != side or order.reduce_only or order.px is None:
                continue
            if account.hedge_mode and order.pos_side != direction:
                continue
            queued += _dec(order.remaining) * per_contract * _dec(order.px)
        total = held_value + queued + value
        if total > _dec(cfg.trade_max_position_usd):
            raise Refused(
                f"下了这张单，{inst_id} 的{word}单会到约 {_amt(total)} {settle}"
                f"（现有 {_amt(held_value)}，挂单 {_amt(queued)}，这张 {_amt(value)}），"
                f"超过了单个品种的持仓上限 {cfg.trade_max_position_usd:,.0f}。"
                "上限在服务器 .env 的 TRADE_MAX_POSITION_USD，飞书里改不了。"
            )
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

        sl = _round_to(cmd.sl, spec.tick_sz) if cmd.sl is not None else None
        tp = _round_to(cmd.tp, spec.tick_sz) if cmd.tp is not None else None
        if cfg.trade_require_stop and sl is None:
            raise Refused("按你的设置（TRADE_REQUIRE_STOP=true），开仓必须带止损。请在指令后面加上“止损 价格”。")
        entry_name = "限价" if px is not None else "现价"
        self._check_stop_sides(direction, ref, sl, tp, entry_name, spec)

        td_mode = (same or held)[0].mgn_mode if held else cfg.trade_margin_mode
        pos_side = direction if account.hedge_mode else None
        lever = None
        try:
            lever = self._okx.leverage(inst_id, td_mode)
        except (OkxError, OkxNetworkError) as exc:
            log.warning("could not read the leverage of %s: %s", inst_id, exc)

        lines = [f"数量 {_contracts_text(contracts, spec)}，按{entry_name} {_px(ref, spec)} 约 {_amt(value)} {settle}（名义价值）"]
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
        coins = contracts * per_contract
        if sl is not None:
            lines.append(f"止损 {_px(sl, spec)}（离{entry_name} {abs(sl - ref) / ref * 100:.2f}%，触发后大约亏 {_amt(abs(sl - ref) * coins)} {settle}）")
        else:
            lines.append("止损 无（这张单不带止损）")
        if tp is not None:
            lines.append(f"止盈 {_px(tp, spec)}（离{entry_name} {abs(tp - ref) / ref * 100:.2f}%，触发后大约赚 {_amt(abs(tp - ref) * coins)} {settle}）")
        else:
            lines.append("止盈 无")
        lines.append(f"下单后 {inst_id} 的{word}单约 {_amt(total)} {settle}（上限 {cfg.trade_max_position_usd:,.0f}）")

        args = {"inst_id": inst_id, "td_mode": td_mode, "side": side, "ord_type": cmd.ord_type, "sz": _plain(contracts)}
        if px is not None:
            args["px"] = _plain(px)
        if pos_side:
            args["pos_side"] = pos_side
        if sl is not None:
            args["sl_trigger_px"] = _plain(sl)
        if tp is not None:
            args["tp_trigger_px"] = _plain(tp)
        title = f"{'限价' if px is not None else '市价'}做{word} {inst_id}"
        data = {"sz": _plain(contracts), "ref": _plain(last), "positions": self._fingerprint(held)}
        return Plan("open", title, tuple(lines), (Step("place_order", args, "开仓"),), inst_id, data)

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
        everything = self._okx.pending_orders(cmd.inst_id or None)
        orders = [order for order in everything if order.inst_id in allowed]
        skipped = len(everything) - len(orders)
        if cmd.ord_id:
            orders = [order for order in orders if order.ord_id == cmd.ord_id]
            if not orders:
                raise Refused(f"没有找到订单号 {cmd.ord_id} 的挂单。发“挂单”可以看当前的挂单。")
        elif cmd.inst_id and cmd.inst_id not in allowed:
            raise Refused(f"{cmd.inst_id} 不在允许下单的品种里，我不动它的挂单。请在 OKX 里操作。")
        if previous is not None:
            wanted = set(previous.data["ids"])
            orders = [order for order in orders if order.ord_id in wanted]
            if not orders:
                raise Refused("预览里的挂单已经不在了（可能已经成交或被撤掉）。发“挂单”可以看现在的情况。")
        if not orders:
            raise Refused("现在没有可以撤的挂单。" + (f"（另有 {skipped} 张在不允许下单的品种上，我不动。）" if skipped else ""))
        if not (cmd.ord_id or cmd.inst_id or cmd.everything) and len(orders) > 1 and previous is None:
            listing = "\n".join(f"- {self._order_line(order)}" for order in orders)
            raise Refused(f"现在有 {len(orders)} 张挂单，请写明撤哪张：撤单 品种、撤单 订单号，或 撤单 全部。\n{listing}")
        lines = [self._order_line(order) for order in orders]
        if skipped and cmd.everything:
            lines.append(f"另有 {skipped} 张挂单在不允许下单的品种上，我不动它们")
        steps = tuple(
            Step("cancel_order", {"inst_id": order.inst_id, "ord_id": order.ord_id}, f"撤单 {order.ord_id}", independent=True)
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
            raise Refused(
                f"{inst_id} {word}单上有 {len(stops)} 张止盈止损单，我分不清你要改哪一张。"
                f"可以先发“取消止盈止损 {inst_id.split('-')[0]}”全部撤掉再重新设，或者在 OKX 里改。"
            )
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
        lines = [
            f"仓位 {_contracts_text(_dec(position.qty), spec)}，均价 {self._price(position.avg_px or 0, spec)}，现价 {_px(last, spec)}",
            self._stop_line("止损", old_sl, new_sl, last, average, coins, position, spec),
            self._stop_line("止盈", old_tp, new_tp, last, average, coins, position, spec),
            *notes,
        ]
        return Plan("stop", title, tuple(lines), steps, inst_id, {"positions": self._fingerprint([position])})

    @staticmethod
    def _stop_line(
        name: str, old: Decimal | None, new: Decimal | None, last: Decimal, average: Decimal | None, coins: Decimal,
        position: Position, spec: InstrumentSpec,
    ) -> str:  # fmt: skip
        before = "无" if old is None else _px(old, spec)
        if new == old:
            return f"{name} {before}（不变）"
        if new is None:
            return f"{name} {before} → 取消"
        text = f"{name} {before} → {_px(new, spec)}（离现价 {abs(new - last) / last * 100:.2f}%"
        if average is not None:
            result = (new - average) * coins * (1 if position.direction == "long" else -1)
            text += f"，按均价算触发时大约 {float(result):+,.2f} {position.pnl_ccy}"
        return text + "）"

    # -------------------------------------------------------------- execution

    def _execute(self, plan: Plan) -> Reply:
        """Sends the steps of a confirmed plan and reports, truthfully, what happened."""
        lines: list[str] = []
        done = failed = 0
        unknown = False
        for step in plan.steps:
            try:
                lines.extend(self._run(step))
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
        if result in ("unknown", "partial"):
            lines.append("请发“仓位”和“挂单”核对实际情况。我不会自动重试。")
        mode = MODE_LABELS[self._mode]
        now = self._clock()
        self._journal.record_order(
            self._mode, plan.action, plan.title, result, plan.inst_id,
            request=[{"call": step.call, **step.args} for step in plan.steps], detail="\n".join(lines), ts=now,
        )  # fmt: skip
        tag = "[模拟盘] " if self._mode == "demo" else ""
        self._journal.record_event("trade", f"{tag}{plan.title}：{heading}", plan.inst_id, ts=now)
        log.info("order (%s) %s %s: %s", self._mode, plan.action, plan.inst_id or "-", result)
        body = "\n".join([f"**{plan.title}**", *(f"- {line}" for line in lines)])
        return Reply(body, title=f"{heading} · {mode}", template=template)

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
