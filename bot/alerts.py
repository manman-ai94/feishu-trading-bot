"""Price alerts: "跌破 3800 提醒我".

The owner sets an alert in Feishu, in his private chat or in the registered
group. "1小时收盘跌破 3605 提醒我" waits for a candle to close beyond the level
instead of a touch. It is kept in the journal, so a restart forgets nothing. While any
alert is active a thread reads OKX's last price and the one-minute candles
every few seconds. The first time the price reaches the level the owner is
told (and the group, when he shares the account there), and the alert is
done. Candles count only from the minute after the alert was set, so a wick
from before it never fires it, and a wick between two reads is not missed.

An alert only tells. It never places, changes or cancels an order. The same
watcher also runs the jobs other parts of the bot keep in the table (`action`):
a cancellation the owner confirmed with his code when he set it ("周五 13:30
撤单 XAU"), and a one-off second look at the market ("15 分钟后再看一下").
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import tzinfo
from typing import Callable

from .okx_client import OkxError, OkxNetworkError
from .times import both

log = logging.getLogger(__name__)

CHECK_EVERY_SECONDS = 10.0
KEEP_DAYS = 30  # an alert that has not fired by then is dropped, and the owner told
MAX_ACTIVE = 20
MAX_TEXT_CHARS = 60
FAR_PCT = 50.0  # a level this far from the market is taken for a typo
MAX_CANDLES = 300  # OKX gives at most this many one-minute candles in one request
TROUBLE_LOG_SECONDS = 600
BAR_READ_SECONDS = 30  # how often the candles of a close alert are read
BARS = {"15m": ("15分钟", 900), "30m": ("30分钟", 1800), "1H": ("1小时", 3600), "4H": ("4小时", 14400)}
_BAR = re.compile(
    r"(?:(15|30)\s*(?:分钟|min|m)|([14])\s*(?:个)?\s*(?:小时|h))\s*(?:级别)?\s*(?:的)?\s*(?:k\s*线|线)?\s*(?:的)?\s*(?:收盘|收线|收)",
    re.I,
)
ACTION_NAMES = {"cancel": "撤单", "recheck": "再看一次"}

ALERT_WORDS = (
    "提醒", "通知我", "通知一下", "通知下", "告诉我", "叫我", "喊我", "提示我", "报警", "叫一声", "说一声", "吱一声",
)  # fmt: skip
BELOW_WORDS = (
    "跌破", "跌穿", "跌到", "跌至", "跌回", "掉到", "掉破", "低于", "下破", "下穿", "回落到", "回落至", "回调到",
    "回调至", "小于",
)  # fmt: skip
ABOVE_WORDS = (
    "涨破", "涨穿", "涨到", "涨至", "涨回", "突破", "高于", "站上", "上破", "上穿", "冲到", "冲上", "冲破", "升到",
    "反弹到", "反弹至", "大于",
)  # fmt: skip
EITHER_WORDS = ("到达", "到了", "触及", "碰到", "摸到", "回到", "回踩", "到", "至")
BELOW_AFTER = ("以下", "下方", "之下")
ABOVE_AFTER = ("以上", "上方", "之上")
# "为什么没提醒我" asks about an alert; "不用提醒" is not a request for one.
NOT_A_REQUEST = (
    "不用", "别", "不要", "不必", "没提醒", "为什么", "为啥", "怎么没", "刚才", "上次", "提醒过", "提醒了吗", "有没有提醒",
    "什么", "怎么办", "怎么看", "如何", "哪些", "要不要", "该不该",
)  # fmt: skip
# Without a word for the direction, a lone number next to these is not an alert level: "提醒我止损 3800".
ORDER_HINTS = ("止损", "止盈", "平仓", "开仓", "做多", "做空", "挂单", "撤单", "下单", "加仓", "减仓")
LIST_WORDS = ("提醒", "提醒列表", "提醒清单", "我的提醒", "价格提醒", "查提醒", "看提醒", "所有提醒", "全部提醒", "/alerts")
EVERYTHING_WORDS = ("全部", "所有", "都")

_NUM = r"\d+(?:\.\d+)?"
_WORDS = "|".join(re.escape(word) for word in sorted(BELOW_WORDS + ABOVE_WORDS + EITHER_WORDS, key=len, reverse=True))
_AFTER = r"点钟|点半|点前|点后|点|以下|下方|之下|以上|上方|之上|%|％|手|张|个|倍|分钟|小时|天|秒|号|日|u(?![a-z])"
_PAIR = re.compile(rf"({_WORDS})\s*(?:价格|价位|的位置|位置|了)?\s*({_NUM})(?![\d.])\s*({_AFTER})?", re.I)
_BARE = re.compile(rf"(?<![\d.])({_NUM})(?![\d.])\s*(以下|下方|之下|以上|上方|之上)")
_LONE = re.compile(rf"(?<![\d.])({_NUM})(?![\d.])\s*({_AFTER})?", re.I)
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_CANCEL = re.compile(r"取消|删除|撤销|删掉|去掉|撤掉|关掉|关闭")
_NO_MORE = re.compile(r"(?:不要|不用)再?提醒(?:我)?了?$")
_TIME_UNITS = ("点钟", "点半", "点前", "点后", "%", "％", "手", "张", "个", "倍", "分钟", "小时", "天", "秒", "号", "日", "u")


@dataclass(frozen=True)
class AlertReply:
    text: str
    title: str = ""  # with a title the reply is a card


def price_text(value: float) -> str:
    """3800 -> "3,800"; 3799.5 -> "3,799.5"; 0.1234 -> "0.1234"."""
    text = f"{value:,.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _bare(value: float) -> str:
    """A number as it is typed in a command: 3800, 123456.7. No separators."""
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _base(inst_id: str) -> str:
    return inst_id.split("-")[0]


def describe(alert: dict, tz: tzinfo | None = None) -> str:
    """"XAU 跌破 3,800", "XAU 1小时收盘在 3,605 下方", "到 11-21 13:30（北京 11-22 05:30）撤单：XAU 3,588 那张"."""
    bar = alert.get("bar") or ""
    action = alert.get("action") or ""
    if alert["direction"] == "at":
        when = f"到 {both(alert['price'], tz)}"
    elif bar in BARS:
        side = "下方" if alert["direction"] == "below" else "上方"
        when = f"{_base(alert['inst_id'])} {BARS[bar][0]}收盘在 {price_text(alert['price'])} {side}"
    else:
        word = "跌破" if alert["direction"] == "below" else "涨破"
        when = f"{_base(alert['inst_id'])} {word} {price_text(alert['price'])}"
    if action:
        return f"{when} {ACTION_NAMES.get(action, action)}：{alert.get('said') or ''}"
    return when


def bar_in(text: str) -> tuple[str, str]:
    """ "1小时收盘跌破 3605" -> ("1H", the text without "1小时收盘"). ("", text) when no candle close is named."""
    found = _BAR.search(text)
    if not found:
        return "", text
    bar = f"{found.group(1)}m" if found.group(1) else f"{found.group(2)}H"
    return bar, text[: found.start()] + " " + text[found.end() :]


def _levels(text: str) -> list[tuple[str, float]]:
    """The (direction, price) pairs a request names. The direction is "" when only the price decides it."""
    plain = _LATIN.sub(" ", text)  # "XAU-USDT-SWAP 跌破 3800": tickers never carry a level
    found: list[tuple[int, str, float]] = []
    taken: list[tuple[int, int]] = []
    for match in _PAIR.finditer(plain):
        word, number, after = match.group(1), float(match.group(2)), (match.group(3) or "").lower()
        if after in _TIME_UNITS or (after == "点" and number <= 24):
            continue  # "到 8 点提醒我" is a time; "跌到 5%" is not a price
        direction = "below" if word in BELOW_WORDS else "above" if word in ABOVE_WORDS else ""
        if after in BELOW_AFTER:
            if direction == "above":
                continue
            direction = "below"
        elif after in ABOVE_AFTER:
            if direction == "below":
                continue
            direction = "above"
        found.append((match.start(), direction, number))
        taken.append(match.span())
    for match in _BARE.finditer(plain):
        if any(start <= match.start() < end for start, end in taken):
            continue
        direction = "below" if match.group(2) in BELOW_AFTER else "above"
        found.append((match.start(), direction, float(match.group(1))))
    if found:
        return [(direction, number) for _, direction, number in sorted(found)]
    if any(word in text for word in ORDER_HINTS):
        return []
    lone = [
        match for match in _LONE.finditer(plain)
        if (match.group(2) or "").lower() not in _TIME_UNITS and not (match.group(2) == "点" and float(match.group(1)) <= 24)
    ]  # fmt: skip
    if len(lone) == 1:  # "3800 提醒我"
        return [("", float(lone[0].group(1)))]
    return []


class PriceAlerts:
    """Reads the owner's alert commands and answers them. `AlertWatcher` fires the alerts."""

    def __init__(
        self,
        journal,
        okx,
        resolver,
        default_inst: Callable[[], str],
        shares_with_group: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
        tz: tzinfo | None = None,
    ):
        self._journal = journal
        self._okx = okx
        self._resolver = resolver
        self._default_inst = default_inst
        self._shares_with_group = shares_with_group
        self._clock = clock
        self._tz = tz

    def handle(self, text: str, chat_id: str = "") -> AlertReply | None:
        """The reply to an alert command; None when the message is not one."""
        text = _THOUSANDS.sub("", unicodedata.normalize("NFKC", text)).strip()  # "3,900" is one number
        if not text or len(text) > MAX_TEXT_CHARS:
            return None
        lowered = text.lower()
        if lowered.strip(" 。.!！?？") in LIST_WORDS:
            return self._list()
        if "提醒" in text and len(text) <= 30 and (_CANCEL.search(text) or _NO_MORE.search(text.rstrip(" 。.!！~"))):
            return self._cancel(text)
        if not any(word in text for word in ALERT_WORDS) or any(word in text for word in NOT_A_REQUEST):
            return None
        bar, rest = bar_in(text)
        levels = _levels(rest)
        if not levels:
            return None
        return self._set(text, levels, chat_id, bar)

    # ------------------------------------------------------------------- set

    def _set(self, text: str, levels: list[tuple[str, float]], chat_id: str, bar: str = "") -> AlertReply:
        named = self._resolver.find_in_text(text, limit=2)
        if len(named) > 1:
            return AlertReply("一条只能设一个品种的提醒，请分开发，例如“XAU 跌破 3800 提醒我”。")
        inst_id = named[0] if named else self._default_inst()
        if not inst_id:
            return AlertReply("要写品种，例如：XAU 跌破 3800 提醒我。")
        base = _base(inst_id)
        last: float | None = None
        try:
            last = float(self._okx.ticker(inst_id).last)
        except (OkxError, OkxNetworkError) as exc:
            log.info("no price for a new alert on %s: %s", inst_id, exc)
        active = self._journal.alerts("active")
        if len(active) + len(levels) > MAX_ACTIVE:
            return AlertReply(f"价格提醒最多同时 {MAX_ACTIVE} 条。发“提醒”看现有的，取消几条再设。")

        lines: list[str] = []
        made: list[tuple[int, str, float]] = []
        for direction, price in levels:
            if price <= 0:
                lines.append("价格要大于 0。")
                continue
            if last is not None and abs(price - last) / last * 100 > FAR_PCT:
                lines.append(f"{price_text(price)} 离 {base} 现价 {price_text(last)} 太远了，像是写错了，这条没有设。")
                continue
            if not direction:
                if last is None:
                    lines.append(
                        f"现在读不到 {base} 的现价，分不清 {price_text(price)} 在上面还是下面。"
                        f"请写明“跌破 {_bare(price)}”还是“涨到 {_bare(price)}”。"
                    )
                    continue
                if price == last:
                    lines.append(f"{base} 现价就是 {price_text(price)}，这条没有设。")
                    continue
                direction = "below" if price < last else "above"
            twin = next(
                (alert for alert in active if not alert.get("action") and alert["inst_id"] == inst_id
                 and alert["direction"] == direction and alert["price"] == price and (alert.get("bar") or "") == bar),
                None,
            )  # fmt: skip
            if twin is not None:
                lines.append(f"#{twin['id']} {describe(twin)} 已经设过了，不再重复设。")
                continue
            if bar:
                pass  # a close beyond the level is what is asked, wherever the price is now
            elif last is not None and (last <= price if direction == "below" else last >= price):
                side, other = ("下面", "涨到") if direction == "below" else ("上面", "跌到")
                lines.append(
                    f"{base} 现价 {price_text(last)}，已经在 {price_text(price)} {side}了，这条设了会马上响，所以没设。"
                    f"想等它回到 {price_text(price)} 时提醒，发“{other} {_bare(price)} 提醒我”。"
                )
                continue
            alert_id = self._journal.add_alert(inst_id, direction, price, said=text, chat=chat_id, ts=self._clock(), bar=bar)
            made.append((alert_id, direction, price))
        if made:
            log.info("%d price alert(s) set on %s", len(made), inst_id)
        if not made:
            return AlertReply("\n".join(lines))
        where = "私聊和群里各发一条" if self._shares_with_group() else "在私聊里告诉你"
        now = f"（现价 {price_text(last)}）" if last is not None else "（现价暂时读不到，我会接着读）"
        if len(made) == 1:
            alert_id, direction, price = made[0]
            head = f"好，{describe({'inst_id': inst_id, 'direction': direction, 'price': price, 'bar': bar})} 时提醒你{now}。"
            cancel = f"发“取消提醒 {alert_id}”取消这条"
        else:
            listed = "\n".join(
                f"- #{alert_id} {describe({'inst_id': inst_id, 'direction': direction, 'price': price, 'bar': bar})}"
                for alert_id, direction, price in made
            )
            head = f"好，设了 {len(made)} 条{now}：\n{listed}"
            cancel = f"发“取消提醒 {made[0][0]}”取消其中一条"
        judged = f"按 {BARS[bar][0]} K 线收盘价判断，收盘后半分钟内发；" if bar else ""
        body = [head, f"{judged}到了我{where}，每条只提醒一次，不会下单。发“提醒”看全部，{cancel}。", *lines]
        return AlertReply("\n".join(body))

    # ------------------------------------------------------------- list, cancel

    def _list(self) -> AlertReply:
        active = self._journal.alerts("active")
        if not active:
            return AlertReply("现在没有价格提醒。要设的话发“跌破 3800 提醒我”或“XAU 涨到 3900 提醒我”。")
        prices: dict[str, float | None] = {}
        lines = []
        for alert in active:
            inst_id = alert["inst_id"]
            if alert["direction"] == "at":
                lines.append(f"- #{alert['id']} {describe(alert, self._tz)}（{self._when(alert['ts'])} 设）")
                continue
            if inst_id not in prices:
                try:
                    prices[inst_id] = float(self._okx.ticker(inst_id).last)
                except (OkxError, OkxNetworkError):
                    prices[inst_id] = None
            last = prices[inst_id]
            gap = f"，现价 {price_text(last)}，还差 {price_text(abs(alert['price'] - last))}" if last is not None else ""
            lines.append(f"- #{alert['id']} {describe(alert, self._tz)}（{self._when(alert['ts'])} 设{gap}）")
        body = "\n".join(lines) + "\n\n发“取消提醒 编号”取消一条，“取消提醒 全部”全部取消。每条到了只提醒一次；只有你设时回过确认码的撤单会去撤，别的都不会下单。"
        return AlertReply(body, title=f"价格提醒（{len(active)} 条）")

    def _cancel(self, text: str) -> AlertReply:
        active = self._journal.alerts("active")
        if not active:
            return AlertReply("现在没有价格提醒，不用取消。")
        if any(word in text for word in EVERYTHING_WORDS):
            chosen = active
        else:
            numbers = [float(number) for number in re.findall(_NUM, _LATIN.sub(" ", text))]
            if numbers:
                chosen = [
                    alert for alert in active
                    if any(alert["id"] == number or (alert["direction"] != "at" and alert["price"] == number) for number in numbers)
                ]  # fmt: skip
                if not chosen:
                    return AlertReply(f"没有找到这条提醒。{self._listing(active)}")
            elif len(active) == 1:
                chosen = active
            else:
                return AlertReply(f"现在有 {len(active)} 条提醒，要取消哪条？发“取消提醒 编号”，或“取消提醒 全部”。{self._listing(active)}")
        now = self._clock()
        done = [alert for alert in chosen if self._journal.close_alert(alert["id"], "cancelled", ts=now)]
        if not done:
            return AlertReply("这条提醒已经不在了（可能刚刚响过）。")
        log.info("%d price alert(s) cancelled", len(done))
        names = "、".join(f"#{alert['id']} {describe(alert, self._tz)}" for alert in done)
        left = len(active) - len(done)
        return AlertReply(f"已取消：{names}。" + (f"还有 {left} 条，发“提醒”看。" if left else "现在没有别的价格提醒了。"))

    def _listing(self, active: list[dict]) -> str:
        return "\n" + "\n".join(f"- #{alert['id']} {describe(alert, self._tz)}" for alert in active)

    def _when(self, ts: float) -> str:
        return both(ts, self._tz)


class AlertWatcher:
    """Fires the active alerts. `send(title, markdown)` tells the owner (and the group, when he shares
    the account there); when it raises, the alert stays active and is tried again on the next read."""

    def __init__(
        self,
        journal,
        okx,
        send: Callable[[str, str], None],
        clock: Callable[[], float] = time.time,
        tz: tzinfo | None = None,
        act: Callable[[dict], None] | None = None,
    ):
        """`act(alert)` carries out a job (an alert with an `action`); it does its own telling."""
        self._journal = journal
        self._okx = okx
        self._send = send
        self._clock = clock
        self._tz = tz
        self._act = act
        self._read_at: dict[str, float] = {}  # instrument -> when its candles were last read
        self._bar_read_at: dict[tuple[str, str], float] = {}  # (instrument, bar) -> when its candles were last read
        self._trouble_logged: dict[str, float] = {}

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.check_once()
            except Exception:
                log.exception("checking the price alerts crashed")
            stop.wait(CHECK_EVERY_SECONDS)

    def check_once(self) -> int:
        """Reads the price of every instrument with an active alert and fires those it reached. Returns how many fired."""
        active = self._journal.alerts("active")
        if not active:
            return 0
        now = self._clock()
        by_inst: dict[str, list[dict]] = {}
        by_bar: dict[tuple[str, str], list[dict]] = {}
        fired = 0
        for alert in active:
            if now - alert["ts"] > KEEP_DAYS * 86400:
                self._expire(alert, now)
            elif alert["direction"] == "at":
                if now >= alert["price"] and self._run_job(alert, now):
                    fired += 1
            elif (alert.get("bar") or "") in BARS:
                by_bar.setdefault((alert["inst_id"], alert["bar"]), []).append(alert)
            else:
                by_inst.setdefault(alert["inst_id"], []).append(alert)
        for (inst_id, bar), alerts in by_bar.items():
            fired += self._check_closes(inst_id, bar, alerts, now)
        for inst_id, alerts in by_inst.items():
            since = self._read_at.get(inst_id, min(alert["ts"] for alert in alerts))
            minutes = min(MAX_CANDLES, max(3, math.ceil((now - since) / 60) + 2))
            try:
                last = float(self._okx.ticker(inst_id).last)
                candles = self._okx.candles(inst_id, "1m", minutes)
            except (OkxError, OkxNetworkError) as exc:
                self._trouble(inst_id, exc, now)
                continue
            self._read_at[inst_id] = now
            for alert in alerts:
                start_ms = (int(alert["ts"]) // 60 + 1) * 60_000  # candles that began after the alert was set
                seen = [candle for candle in candles if candle.ts >= start_ms]
                if alert["direction"] == "below":
                    reached = min([last] + [candle.low for candle in seen])
                    hit = reached <= alert["price"]
                else:
                    reached = max([last] + [candle.high for candle in seen])
                    hit = reached >= alert["price"]
                if hit and (self._run_job(alert, now) if alert.get("action") else self._fire(alert, reached, last, now)):
                    fired += 1
        return fired

    def _check_closes(self, inst_id: str, bar: str, alerts: list[dict], now: float) -> int:
        """Close alerts: the first candle of `bar` that closed after the alert was set, beyond its level."""
        if now - self._bar_read_at.get((inst_id, bar), 0.0) < BAR_READ_SECONDS:
            return 0
        try:
            candles = self._okx.candles(inst_id, bar, 5)
            last = float(self._okx.ticker(inst_id).last)
        except (OkxError, OkxNetworkError) as exc:
            self._trouble(inst_id, exc, now)
            return 0
        self._bar_read_at[(inst_id, bar)] = now
        length_ms = BARS[bar][1] * 1000
        fired = 0
        for alert in alerts:
            closed = [
                candle for candle in candles
                if candle.confirmed and candle.ts + length_ms > alert["ts"] * 1000 and candle.ts + length_ms <= now * 1000
            ]  # fmt: skip
            beyond = next(
                (c for c in closed if (c.close <= alert["price"] if alert["direction"] == "below" else c.close >= alert["price"])),
                None,
            )
            if beyond is None:
                continue
            if alert.get("action"):
                fired += self._run_job(alert, now)
            elif self._fire_close(alert, beyond, last, now):
                fired += 1
        return fired

    def _fire_close(self, alert: dict, candle, last: float, now: float) -> bool:
        what = describe(alert, self._tz)
        span = BARS[alert["bar"]]
        start = both(candle.ts / 1000, self._tz, "%m-%d %H:%M")
        lines = [
            f"**{what}** 了：{start} 开始的那根 {span[0]} K 线收在 {price_text(candle.close)}，现价 {price_text(last)}。",
            f"这是你 {self._when(alert['ts'])} 设的提醒（#{alert['id']}），只提醒这一次，没有下任何单。",
        ]
        try:
            self._send(f"收盘提醒：{what}", "\n".join(lines))
        except Exception:
            log.exception("could not send close alert #%s; trying again on the next read", alert["id"])
            self._bar_read_at.pop((alert["inst_id"], alert["bar"]), None)
            return False
        self._journal.close_alert(alert["id"], "fired", candle.close, now)
        log.info("close alert #%s fired (%s %s)", alert["id"], alert["inst_id"], alert["bar"])
        return True

    def _run_job(self, alert: dict, now: float) -> bool:
        """A job's time or price has come: it is marked done first, so that it never runs twice, then run."""
        if not self._journal.close_alert(alert["id"], "fired", ts=now):
            return False
        log.info("job #%s (%s) is due", alert["id"], alert.get("action") or "-")
        if self._act is None:
            log.warning("job #%s has nothing to run it", alert["id"])
            return True
        try:
            self._act(alert)
        except Exception:
            log.exception("job #%s failed", alert["id"])
        return True

    def _fire(self, alert: dict, reached: float, last: float, now: float) -> bool:
        what = describe(alert, self._tz)
        extreme = "最低" if alert["direction"] == "below" else "最高"
        lines = [f"**{what}** 了：{extreme}到过 {price_text(reached)}，现价 {price_text(last)}。"]
        lines.append(f"这是你 {self._when(alert['ts'])} 设的提醒（#{alert['id']}），只提醒这一次，没有下任何单。")
        try:
            self._send(f"价格提醒：{what}", "\n".join(lines))
        except Exception:
            log.exception("could not send price alert #%s; trying again on the next read", alert["id"])
            return False
        self._journal.close_alert(alert["id"], "fired", reached, now)
        log.info("price alert #%s fired (%s)", alert["id"], alert["inst_id"])
        return True

    def _expire(self, alert: dict, now: float) -> None:
        if not self._journal.close_alert(alert["id"], "expired", ts=now):
            return
        try:
            self._send(
                "价格提醒已过期",
                f"#{alert['id']} {describe(alert, self._tz)}（{self._when(alert['ts'])} 设）{KEEP_DAYS} 天里没到，已经自动取消。还要的话再设一次。",
            )
        except Exception:
            log.exception("could not tell the owner that price alert #%s expired", alert["id"])

    def _trouble(self, inst_id: str, exc: Exception, now: float) -> None:
        if now - self._trouble_logged.get(inst_id, 0) >= TROUBLE_LOG_SECONDS:
            self._trouble_logged[inst_id] = now
            log.warning("could not read %s for the price alerts: %s", inst_id, exc)

    def _when(self, ts: float) -> str:
        return both(ts, self._tz)
