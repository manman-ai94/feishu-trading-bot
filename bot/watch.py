"""Watching the positions every few hours, when the owner asks for it ("每 4 小时盯一下仓位").

The monitor already reads the account every few seconds and tells the owner the moment a position
opens or closes, a stop goes missing or the price nears liquidation; that costs nothing and goes on as
before. This adds one card at the interval he picked, and only while there is a position: where each
position stands against its stop and take-profit, what it has made, the account's total risk, and a
few sentences from the AI.

The sentences cost money, so the routine ones come from a cheaper model (MODEL_WATCH, Sonnet 5.5 at
low effort by default). The stronger model (MODEL_DAILY) writes them only when a position needs a
closer look: no stop, close to its stop or take-profit, a big move since the last card, or a data
release coming up. A follow-up question to a card goes the usual way, to the stronger model.

It only tells. It never places, changes or cancels an order. The setting is kept in the database, so a
restart does not lose it.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Callable

from . import econ
from .analyst import PRICES, UNKNOWN_MODEL_PRICE, AnalystUnavailable
from .indicators import atr
from .market import build_market_context, candles_text, fmt_price
from .models import AccountState, Candle, Position, size_text

log = logging.getLogger(__name__)

SETTING = "position_watch"
CHECK_EVERY_SECONDS = 60
DEFAULT_HOURS = 4.0
MIN_HOURS = 0.5
MAX_HOURS = 24.0
RETRY_SECONDS = 5 * 60  # after a card could not be sent
MAX_INSTRUMENTS = 3  # market data is read for at most this many instruments per card
RECENT_CANDLES = 8  # 1-hour candles shown to the AI
HOUR_CANDLES = 60  # read for the 1-hour ATR
DIALOG_CHARS = 1500

# When the stronger model writes the comment.
NEAR_STOP_ATR = 1.0  # the stop is within this many 1-hour ATRs of the mark
NEAR_STOP_SHARE = 0.3  # or less than this share of the way from the entry to the stop is left
NEAR_TAKE_PROFIT_ATR = 0.5
BIG_MOVE_ATR = 2.0  # the mark moved this many 1-hour ATRs since the last card
RELEASE_HOURS = 3.0  # a release within this many hours (and before the next card)

# What one comment roughly takes, for the cost the owner is told (measured on a one-position card).
WATCH_INPUT_TOKENS = 4000
WATCH_OUTPUT_TOKENS = 700
CLOSE_OUTPUT_TOKENS = 2500

STATUS_WORDS = ("盯仓", "盯盘", "盯仓状态", "盯仓设置", "盯仓开着吗", "还在盯吗", "在盯吗", "还在盯仓吗", "盯仓情况")
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUMBER = r"(\d+(?:\.\d+)?|[零一二两三四五六七八九十]+(?:个半)?|半)"
_UNIT = r"(小时|钟头|h|hr|hour|hours|分钟|min|mins|minutes)"
_EVERY = re.compile(rf"每\s*(?:隔)?\s*{_NUMBER}?\s*个?\s*{_UNIT}", re.IGNORECASE)
_EVERY_AFTER = re.compile(rf"{_NUMBER}\s*个?\s*{_UNIT}\s*(?:一次|一回|一趟|看一次|盯一次|看一眼|发一次|报一次)", re.IGNORECASE)
_EVERY_DAY = re.compile(r"每\s*天|一天一次")
_VERB = re.compile(r"盯|看|检查|汇报|报告|报一下|过一遍|更新|提醒|播报")
_HOLDING = re.compile(r"仓|单子|持仓|盘")
_STOP = re.compile(r"停|取消|关掉|关闭|关了|不用|别|不要|暂停|算了")
# A question about the watching, not a request for it: the AI answers those.
_QUESTION = re.compile(r"费|额度|花多少|多少钱|贵不贵|会不会|为什么|为啥|怎么|如何|做不到")


@dataclass(frozen=True)
class WatchCommand:
    kind: str  # "set", "stop" or "status"
    hours: float | None = None  # for "set"; None when he gave no interval
    bad: str = ""  # for "set": why the interval cannot be used


@dataclass
class WatchReply:
    text: str
    then: Callable[[], None] | None = None  # sends the first card, after the reply


@dataclass
class Watching:
    hours: float
    chat_id: str  # "" sends to the owner's private chat
    private: bool  # chat_id is his private chat, not the group
    since: float
    last: float  # the last card, or when it was set
    marks: dict[str, float] = field(default_factory=dict)  # position key -> mark price on the last card

    def as_json(self) -> str:
        return json.dumps(
            {
                "hours": self.hours, "chat_id": self.chat_id, "private": self.private, "since": self.since,
                "last": self.last, "marks": self.marks,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "Watching | None":
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(
                hours=float(data["hours"]),
                chat_id=str(data.get("chat_id", "")),
                private=bool(data.get("private", True)),
                since=float(data.get("since", 0)),
                last=float(data.get("last", 0)),
                marks={str(k): float(v) for k, v in (data.get("marks") or {}).items()},
            )
        except (ValueError, KeyError, TypeError):
            log.warning("the position watch setting could not be read; it is treated as off")
            return None

    @property
    def every(self) -> float:
        return self.hours * 3600.0


# ------------------------------------------------------------------- reading


def _cn_number(word: str) -> float | None:
    if word == "半":
        return 0.5
    half = word.endswith("个半")
    if half:
        word = word[:-2]
    if "十" in word:
        tens, _, ones = word.partition("十")
        value = (_CN_DIGITS.get(tens, 0) if tens else 1) * 10 + (_CN_DIGITS.get(ones, 0) if ones else 0)
        if (tens and tens not in _CN_DIGITS) or (ones and ones not in _CN_DIGITS):
            return None
    elif len(word) == 1 and word in _CN_DIGITS:
        value = _CN_DIGITS[word]
    else:
        return None
    return value + (0.5 if half else 0)


def _hours(number: str | None, unit: str) -> float | None:
    if number is None:
        value: float | None = 1.0
    elif number[0].isdigit():
        value = float(number)
    else:
        value = _cn_number(number)
    if value is None:
        return None
    return value / 60.0 if unit.lower().startswith(("分", "min")) else value


def read_command(text: str) -> WatchCommand | None:
    """What the owner asked of the position watch; None when the message is not about it."""
    text = unicodedata.normalize("NFKC", text).strip()
    if not text or len(text) > 60:
        return None
    bare = text.strip(" 。.!！?？~")
    if bare in STATUS_WORDS:
        return WatchCommand("status")
    if "盯" not in text and not (_VERB.search(text) and _HOLDING.search(text)):
        return None
    every = _EVERY.search(text) or _EVERY_AFTER.search(text)
    daily = _EVERY_DAY.search(text)
    if every is None and daily is None:
        if "盯" in text and _STOP.search(text) and len(text) <= 20:
            return WatchCommand("stop")
        # "帮我盯着仓位": no interval, so the usual one.
        if re.search(r"盯\s*(?:一下|下|着|住|紧)?\s*(?:我的)?\s*(?:仓|持仓|仓位|单子|盘)", text) and not _QUESTION.search(text):
            if re.search(r"^\s*(?:帮我|你|请|麻烦)?\s*(?:帮我)?\s*(?:继续)?\s*盯", text) or "帮我盯" in text:
                return WatchCommand("set", None)
        return None
    if _QUESTION.search(text):
        return None
    if daily is not None and every is None:
        return WatchCommand("set", 24.0)
    hours = _hours(every.group(1), every.group(2))
    if hours is None:
        return WatchCommand("set", None, bad="没看懂间隔，请写数字，例如“每 4 小时盯一下仓位”。")
    if hours < MIN_HOURS:
        return WatchCommand("set", hours, bad=f"最短每 {_span(MIN_HOURS)}一次。价格和止损程序本来就每几秒盯着，不用 AI 盯这么勤。")
    if hours > MAX_HOURS:
        return WatchCommand("set", hours, bad="最长每 24 小时一次。")
    return WatchCommand("set", hours)


def _span(hours: float) -> str:
    if hours < 1:
        return f"{round(hours * 60)} 分钟"
    return f"{hours:g} 小时"


def cost_usd(model: str, close: bool) -> float:
    """What one comment costs at list prices, roughly: the facts and market data in, a few sentences out
    (the stronger model also thinks, which counts as output)."""
    price_in, price_out, _ = next((price for prefix, price in PRICES.items() if model.startswith(prefix)), UNKNOWN_MODEL_PRICE)
    output = CLOSE_OUTPUT_TOKENS if close else WATCH_OUTPUT_TOKENS
    return (WATCH_INPUT_TOKENS * price_in + output * price_out) / 1_000_000


def cents(model: str, close: bool) -> str:
    value = cost_usd(model, close) * 100
    return f"{value:.1f} 美分" if value < 10 else f"{value:.0f} 美分"


def model_name(model: str) -> str:
    """claude-sonnet-5-5 -> Sonnet 5.5"""
    parts = model.removeprefix("claude-").split("-")
    if len(parts) >= 2 and all(part.isdigit() for part in parts[1:]):
        return f"{parts[0].title()} {'.'.join(parts[1:])}"
    return model


# --------------------------------------------------------------------- facts


def _num(value: float) -> str:
    return f"{value:,.2f}"


def _gap(value: float) -> str:
    """A distance between two prices: cents for gold or BTC, more places for a coin that costs less than 1."""
    return _num(value) if abs(value) >= 1 else fmt_price(value)


def _signed(value: float) -> str:
    return f"{value:+,.2f}"


def _key(position: Position) -> str:
    return "|".join(position.key)


def _side(position: Position) -> str:
    return "多单" if position.direction == "long" else "空单"


def _size(position: Position) -> str:
    return size_text(position)


def _atrs(gap: float, hour_atr: float | None) -> str:
    return f"，约 {gap / hour_atr:.1f} 个 1 小时 ATR" if hour_atr else ""


@dataclass
class PositionFacts:
    position: Position
    lines: list[str]
    reasons: list[str]  # why the stronger model should write the comment; empty when routine


def position_facts(
    position: Position, equity: float | None, hour_atr: float | None, last_mark: float | None, stops_known: bool, since: str
) -> PositionFacts:
    """The lines a card carries for one position, and anything about it that wants a closer look."""
    long = position.direction == "long"
    mark = position.mark_px
    ccy = position.pnl_ccy
    coins = position.coins
    reasons: list[str] = []
    head = f"**{position.inst_id} {_side(position)} {_size(position)}**"
    if position.avg_px is not None:
        head += f"，开仓均价 {fmt_price(position.avg_px)}"
    if mark is not None:
        head += f"，标记价 {fmt_price(mark)}"
    lines = [head]

    if position.upl is not None:
        share = f"（占权益 {position.upl / equity * 100:+.1f}%）" if equity and equity > 0 else ""
        lines.append(f"- 浮动盈亏 {_signed(position.upl)} {ccy}{share}")

    stop = position.stop_loss
    if stop is None:
        if stops_known:
            lines.append("- **没有止损**")
            reasons.append(f"{position.inst_id} 没有止损")
        else:
            lines.append("- 止损这次没读到")
    elif mark is not None:
        gap = mark - stop if long else stop - mark
        if gap <= 0:
            lines.append(f"- 止损 {fmt_price(stop)}：标记价已经在止损另一侧，止损随时会触发")
            reasons.append(f"{position.inst_id} 的标记价已经越过止损 {fmt_price(stop)}")
        else:
            text = f"- 止损 {fmt_price(stop)}：离标记价 {_gap(gap)}（{gap / mark * 100:.2f}%{_atrs(gap, hour_atr)}）"
            if coins is not None and position.avg_px is not None:
                result = (stop - position.avg_px if long else position.avg_px - stop) * abs(coins)
                share = f"，占权益 {abs(result) / equity * 100:.1f}%" if equity and equity > 0 else ""
                text += f"；打到止损这笔{'亏' if result < 0 else '赚'}约 {_num(abs(result))} {ccy}{share}"
            lines.append(text)
            near = hour_atr is not None and gap <= NEAR_STOP_ATR * hour_atr
            if position.avg_px is not None:
                full = position.avg_px - stop if long else stop - position.avg_px
                if full > 0 and gap <= NEAR_STOP_SHARE * full:
                    near = True
            if near:
                reasons.append(f"{position.inst_id} 离止损只剩 {_gap(gap)}{_atrs(gap, hour_atr)}")

    take = position.take_profit
    if take is None:
        lines.append("- 没有止盈")
    elif mark is not None:
        gap = take - mark if long else mark - take
        if gap <= 0:
            lines.append(f"- 止盈 {fmt_price(take)}：标记价已经到了止盈价")
        else:
            text = f"- 止盈 {fmt_price(take)}：离标记价 {_gap(gap)}（{gap / mark * 100:.2f}%{_atrs(gap, hour_atr)}）"
            if coins is not None and position.avg_px is not None:
                result = (take - position.avg_px if long else position.avg_px - take) * abs(coins)
                text += f"；到止盈这笔{'赚' if result >= 0 else '亏'}约 {_num(abs(result))} {ccy}"
            lines.append(text)
            if hour_atr is not None and gap <= NEAR_TAKE_PROFIT_ATR * hour_atr:
                reasons.append(f"{position.inst_id} 离止盈不到半个 1 小时 ATR")

    if position.liq_px and mark:
        gap = abs(mark - position.liq_px)
        lines.append(f"- 预估强平价 {fmt_price(position.liq_px)}，离标记价 {_gap(gap)}（{gap / mark * 100:.2f}%）")

    if mark is not None and last_mark is not None:
        move = mark - last_mark
        word = "涨" if move >= 0 else "跌"
        far = f"（约 {abs(move) / hour_atr:.1f} 个 1 小时 ATR）" if hour_atr else ""
        lines.append(f"- 比{since}的标记价 {fmt_price(last_mark)} {word}了 {_gap(abs(move))}{far}")
        if hour_atr and abs(move) >= BIG_MOVE_ATR * hour_atr:
            reasons.append(f"{position.inst_id} 比{since}{word}了 {abs(move) / hour_atr:.1f} 个 1 小时 ATR")
    return PositionFacts(position, lines, reasons)


def _ago(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1:
        return f"{max(1, round(seconds / 60))} 分钟前"
    return f"{hours:.1f}".rstrip("0").rstrip(".") + " 小时前"


# ---------------------------------------------------------------------- watch


class PositionWatch:
    """Reads the owner's watch commands, keeps the setting, and sends the cards when they are due."""

    def __init__(
        self,
        journal,
        okx,
        analyst,
        cfg,
        state: Callable[[], AccountState | None],
        exposure: Callable[[AccountState], list[str]],
        send: Callable[[str, bool, str, str, str], None],
        clock: Callable[[], float] = time.time,
    ):
        """`send(chat_id, private, title, markdown, template)` sends a card (Reporter.to_chat): chat_id "" is
        the owner's private chat."""
        self._journal = journal
        self._okx = okx
        self._analyst = analyst
        self._cfg = cfg
        self._state = state
        self._exposure = exposure
        self._send = send
        self._clock = clock
        self._lock = threading.Lock()  # a first card and the timer never send at once

    @property
    def _tz(self) -> tzinfo:
        return self._cfg.tz

    def watching(self) -> Watching | None:
        return Watching.from_json(self._journal.get_setting(SETTING, ""))

    def _save(self, watching: Watching | None) -> None:
        self._journal.set_setting(SETTING, watching.as_json() if watching is not None else "")

    # ------------------------------------------------------------- commands

    def handle(self, text: str, chat_id: str, private: bool, chat_shows_account: bool) -> WatchReply | None:
        command = read_command(text)
        if command is None:
            return None
        if command.kind == "status":
            return WatchReply(self.status_text())
        if command.kind == "stop":
            if self.watching() is None:
                return WatchReply("盯仓本来就没开。想开就发“每 4 小时盯一下仓位”（几小时你定）。")
            self._save(None)
            log.info("position watch: turned off by the owner")
            return WatchReply("好，定时盯仓停了。仓位有变动、止损没了、离强平近了，程序照样马上告诉你，这些不花 AI 的钱。")
        if command.bad:
            return WatchReply(command.bad)
        hours = command.hours or DEFAULT_HOURS
        now = self._clock()
        target = chat_id if chat_shows_account else ""
        self._save(Watching(hours, target, private, now, now))
        log.info("position watch: every %g hour(s)", hours)
        state = self._state()
        holding = state is not None and bool(state.positions)
        lines = [f"好，持仓期间每 {_span(hours)}盯一次仓位，发一张卡片：离止损止盈多远、浮动盈亏多少、账户总风险，再加几句点评。"]
        if command.hours is None:
            lines[0] += f"（你没写间隔，先按每 {_span(DEFAULT_HOURS)}；要改就发“每 2 小时盯一下仓位”。）"
        lines.append(
            f"例行点评用 {model_name(self._cfg.model_watch)}（{self._cfg.effort_watch} 档），一次大约 {cents(self._cfg.model_watch, False)}；"
            f"没止损、离止损或止盈很近、波动大、快公布重要数据时换 {model_name(self._cfg.model_daily)}，"
            f"一次大约 {cents(self._cfg.model_daily, True)}。"
        )
        if not chat_shows_account:
            lines.append("这个群看不到账户，卡片私聊发给你。")
        lines.append("只看不动，不会碰任何单。发“停止盯仓”关掉，发“盯仓”看设置。")
        if holding:
            lines.append("第一张现在就发。")
            return WatchReply("\n".join(lines), then=lambda: self.send_now(reason="first"))
        lines.append("现在没有持仓，有持仓了才发。")
        return WatchReply("\n".join(lines))

    def status_text(self) -> str:
        watching = self.watching()
        if watching is None:
            return "现在没开定时盯仓。发“每 4 小时盯一下仓位”开始（几小时你定，最短半小时），持仓期间按时发卡片，只看不动。"
        due = datetime.fromtimestamp(watching.last + watching.every, tz=self._tz).strftime("%H:%M")
        state = self._state()
        holding = state is not None and bool(state.positions)
        where = "私聊" if watching.private or not watching.chat_id else "群里"
        text = f"定时盯仓开着：每 {_span(watching.hours)}一次，发在{where}。"
        text += f"下一张大约 {due}。" if holding else "现在没有持仓，有持仓了才发。"
        text += (
            f"例行点评用 {model_name(self._cfg.model_watch)}，要留意时换 {model_name(self._cfg.model_daily)}。发“停止盯仓”关掉。"
        )
        return text

    # ---------------------------------------------------------------- timer

    def check_once(self) -> bool:
        """Sends a card when one is due. True when one went out."""
        watching = self.watching()
        if watching is None:
            return False
        now = self._clock()
        if now - watching.last < watching.every - CHECK_EVERY_SECONDS / 2:
            return False
        state = self._state()
        if state is None:
            return False  # the account is not read yet: next minute
        if not state.positions:
            watching.last = now  # keeps the rhythm; the first card comes one interval after a position opens
            watching.marks = {}
            self._save(watching)
            return False
        return self.send_now()

    def send_now(self, reason: str = "due") -> bool:
        with self._lock:
            watching = self.watching()
            state = self._state()
            if watching is None or state is None or not state.positions:
                return False
            now = self._clock()
            try:
                title, body, template, marks = self.card(state, watching, now)
                self._send(watching.chat_id, watching.private, title, body, template)
            except Exception:
                log.exception("position watch: the card could not be sent; trying again in %d minutes", RETRY_SECONDS // 60)
                watching.last = now - watching.every + RETRY_SECONDS
                self._save(watching)
                return False
            watching.last = now
            watching.marks = marks
            self._save(watching)
            self._remember(watching.chat_id, title, body, now)
            log.info("position watch: card sent (%s)", reason)
            return True

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.check_once()
            except Exception:
                log.exception("position watch failed")
            stop.wait(CHECK_EVERY_SECONDS)

    # ----------------------------------------------------------------- card

    def card(self, state: AccountState, watching: Watching, now: float) -> tuple[str, str, str, dict[str, float]]:
        """(title, markdown, template, the marks to remember)."""
        equity = state.snapshot.total_eq
        since = f"上次盯仓（{_ago(now - watching.last)}）"
        inst_ids = list(dict.fromkeys(p.inst_id for p in state.positions))
        candles = {inst_id: self._hour_candles(inst_id) for inst_id in inst_ids}
        hour_atrs = {inst_id: (atr([c for c in rows if c.confirmed] or rows) if rows else None) for inst_id, rows in candles.items()}
        facts = [
            position_facts(
                p, equity, hour_atrs.get(p.inst_id), watching.marks.get(_key(p)), state.stops_known, since
            )
            for p in state.positions
        ]
        reasons = [reason for item in facts for reason in item.reasons]
        release = self._next_release(now, watching)
        if release is not None:
            reasons.append(release)

        lines: list[str] = []
        for item in facts:
            lines.extend(item.lines)
        total = self._safe_exposure(state)
        if equity is not None:
            lines.append("")
            lines.append(f"**账户权益**：{_num(equity)} USD")
        if total:
            if equity is None:
                lines.append("")
            lines.extend(total)
        if release is not None:
            lines.append(f"**{release}**")

        comment, model = self._comment(lines, facts, candles, reasons, watching, now)
        lines.append("")
        lines.append(f"**点评**（{model_name(model)}）：{comment}" if model else comment)
        due = datetime.fromtimestamp(now + watching.every, tz=self._tz).strftime("%H:%M")
        lines.append("")
        lines.append(f"每 {_span(watching.hours)}盯一次，下一张大约 {due}。只看不动，没有碰任何单；有疑问直接问我，发“停止盯仓”关掉。")

        upl = [p.upl for p in state.positions if p.upl is not None]
        ccys = {p.pnl_ccy for p in state.positions}
        money = f" 浮动 {_signed(sum(upl))} {ccys.pop()}" if upl and len(ccys) == 1 else ""
        if len(state.positions) == 1:
            p = state.positions[0]
            subject = f"{p.inst_id.split('-')[0]} {_side(p)}"
        else:
            subject = f"{len(state.positions)} 个持仓"
        title = f"{'盯仓（要留意）' if reasons else '盯仓'}：{subject}{money}"
        marks = {_key(p): p.mark_px for p in state.positions if p.mark_px is not None}
        return title, "\n".join(lines), "orange" if reasons else "turquoise", marks

    def _comment(
        self,
        lines: list[str],
        facts: list[PositionFacts],
        candles: dict[str, list[Candle]],
        reasons: list[str],
        watching: Watching,
        now: float,
    ) -> tuple[str, str]:
        """(the comment, the model that wrote it; "" when none did)."""
        close = bool(reasons)
        model = self._cfg.model_daily if close else self._cfg.model_watch
        blocks = [
            f"盯仓间隔：每 {_span(watching.hours)}。交易员定的单笔风险：权益的 {self._cfg.risk_per_trade_pct:g}%。",
            "【程序算好、已经显示在卡片上的数字】\n" + "\n".join(line.replace("**", "") for line in lines if line),
        ]
        for inst_id in list(candles)[:MAX_INSTRUMENTS]:
            try:
                context = build_market_context(self._okx, inst_id)
                blocks.append("【OKX 实时行情数据】\n" + context.to_text())
            except Exception as exc:
                log.info("position watch: no market context for %s (%s)", inst_id, type(exc).__name__)
            rows = candles.get(inst_id) or []
            if rows:
                blocks.append("【最近的 1 小时 K 线】\n" + candles_text(inst_id, "1H", rows[-RECENT_CANDLES:], self._tz))
        if close:
            blocks.append("这次要多留意：" + "；".join(reasons) + "。")
        try:
            return self._analyst.watch_comment("\n\n".join(blocks), close=close), model
        except AnalystUnavailable as exc:
            return f"（这次 AI 点评没写出来：{exc}）", ""
        except Exception:
            log.exception("position watch: the comment failed")
            return "（这次 AI 点评没写出来，上面的数字是程序算的，照常有效。）", ""

    def _hour_candles(self, inst_id: str) -> list[Candle]:
        try:
            return self._okx.candles(inst_id, "1H", HOUR_CANDLES)
        except Exception as exc:
            log.info("position watch: no 1-hour candles for %s (%s)", inst_id, type(exc).__name__)
            return []

    def _safe_exposure(self, state: AccountState) -> list[str]:
        try:
            return self._exposure(state)
        except Exception:
            log.exception("position watch: the total risk could not be measured")
            return []

    def _next_release(self, now: float, watching: Watching) -> str | None:
        """A data release coming before the next card (and within a few hours), in words."""
        horizon = now + min(watching.every, RELEASE_HOURS * 3600)
        for release in econ.releases():
            if now < release.at <= horizon:
                local = datetime.fromtimestamp(release.at, tz=self._tz).strftime("%H:%M")
                hours = (release.at - now) / 3600
                left = f"{hours:.1f} 小时" if hours >= 1 else f"{max(1, round(hours * 60))} 分钟"
                return f"{release.name}在 {local} 公布，还有约 {left}，前后几分钟常大幅波动，止损可能滑点"
        return None

    def _remember(self, chat_id: str, title: str, body: str, now: float) -> None:
        """Kept with the chat's questions and answers, so "为什么" after a card makes sense."""
        if not chat_id:
            return
        text = f"[程序按设定定时发的盯仓卡片：{title}]\n{body.replace('**', '')}"
        if len(text) > DIALOG_CHARS:
            text = text[:DIALOG_CHARS] + "……（后面略）"
        try:
            self._journal.record_dialog(chat_id, "（定时盯仓）", text, now)
        except Exception:
            log.exception("position watch: the card could not be kept in the chat history")
