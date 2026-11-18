"""The track record: the levels the AI gave, and what the market did next.

An analysis that gives scenarios ends with a small block for the program,
<levels>{...}</levels>, which is cut off before the answer is shown. The
numbers in it go into the journal and are scored later against OKX's
15-minute candles, by fixed rules and without the AI:

  * A scenario counts once a 15-minute candle closes beyond its trigger level
    ("站稳" above it for a long, "跌破" for a short).
  * From the next candle on: touching the target first is a hit, touching the
    invalidation level first is a failure. Both inside one candle is counted
    as a failure, because the order of the two cannot be known.
  * Neither within the window (72 hours by default): expired, valued at the
    price at the end of the window.
  * Never triggered within the window: left out of the hit rate.

Results are given in R: the distance from the trigger to the invalidation
level is one R. A failure is -1R; a hit is the distance to the target in R.

The direction ("偏多" / "偏空") is compared with the price 24 and 72 hours
later, next to how often the market simply went up over the same samples.

This is a check of analysis against price. It is not a record of trades or
of returns: whether anyone traded these levels is not known here.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .journal import Journal
from .models import Candle
from .okx_client import OkxError, OkxNetworkError

log = logging.getLogger(__name__)

BAR = "15m"
BAR_SECONDS = 15 * 60
BARS = 300  # one request: 75 hours of 15-minute candles
MAX_WINDOW_HOURS = 72
MAX_SCENARIOS = 4
SCORE_EVERY_SECONDS = 15 * 60
MIN_SAMPLE = 20  # below this the percentages say very little

# Added to the AI's instructions when the track record is kept.
LEVELS_NOTE = """

成绩记录：
回答正文里已经给出了某个品种的情景（有触发位、目标位和失效位）时，在全文的最后另起一行，附一段给程序读的记录。读者看不到这一段。这段记录只是把正文里已有的情景抄下来：不要为了写它而在正文里加情景，正文没有情景就不附。格式严格如下，写在一行里，不要放进代码块：
<levels>{"inst":"品种代码","bias":"long","scenarios":[{"side":"long","trigger":触发位,"target":目标位,"invalid":失效位,"conf":把握}]}</levels>
inst 写 OKX 的品种代码，例如 XAU-USDT-SWAP、BTC-USDT-SWAP；bias 是结论的方向，只能是 long、short 或 range；side 只能是 long 或 short。价位写成普通数字，不带逗号和引号，必须和正文里的数字完全一致。正文写了几个情景就写几个，目标位有多个时写第一个。一条回答只附一段，写最主要的那个品种。
把握程度：正文里每个情景顺带写一句你的把握，意思是“触发以后先到目标位、不先碰到失效位”的可能性，例如“把握约 55%”。凭数据给，不要每次都写同一个数；参考消息里你自己的成绩单，最近失效得多就写低一点。conf 写同一个数，是 1 到 99 的整数。"""

_BLOCK = re.compile(r"\s*<levels>(.*?)</levels>[ \t]*", re.DOTALL | re.IGNORECASE)
_DANGLING = re.compile(r"\s*<levels>.*?(?=\n\s*\n|\Z)", re.DOTALL | re.IGNORECASE)
_INSTRUMENT = re.compile(r"^[A-Z0-9]{1,12}(-[A-Z0-9]{1,12}){1,2}$")
BIASES = ("long", "short", "range")
STATUS_WORDS = {"hit": "命中", "failed": "失效", "expired": "到期", "untriggered": "没触发", "unscored": "没法评", "open": "观察中"}


@dataclass(frozen=True)
class Scenario:
    side: str  # "long" or "short"
    trigger: float
    target: float
    invalid: float
    confidence: int | None = None  # the AI's stated chance, in percent, that the target comes before the invalidation

    @property
    def risk(self) -> float:
        return abs(self.trigger - self.invalid)

    @property
    def reward_r(self) -> float:
        return abs(self.target - self.trigger) / self.risk


@dataclass(frozen=True)
class Levels:
    inst_id: str
    bias: str  # "long", "short", "range" or ""
    scenarios: tuple[Scenario, ...]


def extract_levels(text: str) -> tuple[str, Levels | None]:
    """Cuts the <levels> block out of an answer. Returns the text to show and what the block said.

    The block is removed even when it cannot be read, so the reader never sees it.
    """
    found = _BLOCK.search(text)
    if found is None:
        # A block that was cut short (the answer hit its length limit) is still not for the reader.
        return _tidy(_DANGLING.sub("", text)), None
    # One block is expected; any more are dropped unread.
    return _tidy(_BLOCK.sub("\n\n", text)), parse_levels(found.group(1))


def _tidy(text: str) -> str:
    text = re.sub(r"```[a-zA-Z]*\s*```", "", text)  # the fence it was (against instructions) wrapped in
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_levels(raw: str) -> Levels | None:
    try:
        data = json.loads(raw.strip())
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    inst_id = str(data.get("inst", "")).strip().upper()
    if not _INSTRUMENT.match(inst_id):
        return None
    bias = str(data.get("bias", "")).strip().lower()
    if bias not in BIASES:
        bias = ""
    scenarios: list[Scenario] = []
    rows = data.get("scenarios")
    for row in rows if isinstance(rows, list) else ():
        scenario = _scenario(row)
        if scenario is not None and scenario not in scenarios:
            scenarios.append(scenario)
    scenarios = scenarios[:MAX_SCENARIOS]
    if not scenarios and bias not in ("long", "short"):
        return None  # nothing that could be checked later
    return Levels(inst_id, bias, tuple(scenarios))


def _scenario(row) -> Scenario | None:
    if not isinstance(row, dict):
        return None
    side = str(row.get("side", "")).strip().lower()
    try:
        trigger, target, invalid = (float(row.get(name)) for name in ("trigger", "target", "invalid"))
    except (TypeError, ValueError):
        return None
    if not all(value > 0 and value == value and value != float("inf") for value in (trigger, target, invalid)):
        return None
    confidence = _confidence(row.get("conf"))
    # The three levels must be in the order the side implies; anything else was misread or mistyped.
    if side == "long" and invalid < trigger < target:
        return Scenario(side, trigger, target, invalid, confidence)
    if side == "short" and target < trigger < invalid:
        return Scenario(side, trigger, target, invalid, confidence)
    return None


def _confidence(raw) -> int | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if 0 < value < 1:
        value *= 100  # written as a fraction
    return int(round(value)) if 1 <= value <= 99 else None


# --------------------------------------------------------------------- scoring


def score_call(call: dict, candles: list[Candle], now: float, window_hours: float) -> dict:
    """Moves one scenario forward over the closed candles it has not seen yet.

    `call` is a row of the journal's `calls` table. Returns the fields to write
    back; an empty dict when there is nothing new.
    """
    start = int(call["ts"])
    window_end = start + int(window_hours * 3600)
    checked = int(call["checked_ts"] or 0)
    resume = max(start, checked)
    long = call["side"] == "long"
    trigger, target, invalid = call["trigger_px"], call["target_px"], call["invalid_px"]
    risk = abs(trigger - invalid)
    triggered_ts = call["triggered_ts"]

    # Whole candles only: one that began before the analysis also holds the price from before it.
    usable = [c for c in candles if c.confirmed and c.ts // 1000 >= resume and c.ts // 1000 < window_end and c.ts // 1000 + BAR_SECONDS <= now]
    if usable and usable[0].ts // 1000 - resume > 2 * BAR_SECONDS:
        # Candles are missing between what was checked and what OKX still has (the bot was off for days).
        return {"status": "unscored", "closed_ts": int(now), "checked_ts": resume}
    update: dict = {}
    for candle in usable:
        opened = candle.ts // 1000
        update["checked_ts"] = opened + BAR_SECONDS
        if triggered_ts is None:
            if candle.close >= trigger if long else candle.close <= trigger:
                triggered_ts = opened + BAR_SECONDS
                update["triggered_ts"] = triggered_ts
                if candle.close >= target if long else candle.close <= target:
                    # The close that triggered it is already past the target: the move the analysis described happened.
                    return {**update, "status": "hit", "closed_ts": triggered_ts, "result_r": round(abs(target - trigger) / risk, 3)}
            continue
        if candle.low <= invalid if long else candle.high >= invalid:
            return {**update, "status": "failed", "closed_ts": opened + BAR_SECONDS, "result_r": -1.0}
        if candle.high >= target if long else candle.low <= target:
            return {**update, "status": "hit", "closed_ts": opened + BAR_SECONDS, "result_r": round(abs(target - trigger) / risk, 3)}

    if now >= window_end + BAR_SECONDS:
        seen_to = int(update.get("checked_ts", resume))
        if window_end - seen_to > 2 * BAR_SECONDS:
            return {**update, "status": "unscored", "closed_ts": int(now)}  # the end of the window was never seen
        if triggered_ts is None:
            return {**update, "status": "untriggered", "closed_ts": window_end}
        close = price_at(candles, window_end - 1)
        result = None if close is None else round(((close - trigger) if long else (trigger - close)) / risk, 3)
        return {**update, "status": "expired", "closed_ts": window_end, "result_r": result}
    return update


def price_at(candles: list[Candle], moment: float) -> float | None:
    """The close of the closed candle that contains `moment`, or None when OKX no longer (or not yet) has it."""
    for candle in candles:
        opened = candle.ts // 1000
        if opened <= moment < opened + BAR_SECONDS:
            return candle.close if candle.confirmed else None
    return None


class Scorer:
    """Scores what is waiting in the journal. Reads public market data only."""

    def __init__(self, journal: Journal, okx, window_hours: float = MAX_WINDOW_HOURS, clock: Callable[[], float] = time.time):
        self._journal = journal
        self._okx = okx
        self._window_hours = min(float(window_hours), float(MAX_WINDOW_HOURS))
        self._clock = clock

    def run_once(self) -> int:
        """Returns how many scenarios got their final result."""
        calls = self._journal.open_calls()
        analyses = self._journal.unscored_analyses()
        now = self._clock()
        finished = 0
        candles: dict[str, list[Candle] | None] = {}

        def bars(inst_id: str) -> list[Candle] | None:
            if inst_id not in candles:
                try:
                    candles[inst_id] = self._okx.candles(inst_id, BAR, BARS)
                except (OkxError, OkxNetworkError) as exc:
                    log.warning("track record: no candles for %s: %s", inst_id, exc)
                    candles[inst_id] = None
            return candles[inst_id]

        for call in calls:
            data = bars(call["inst_id"])
            if data is None:
                continue
            update = score_call(call, data, now, self._window_hours)
            if update:
                self._journal.update_call(call["id"], **update)
                finished += "status" in update
        for analysis in analyses:
            data = bars(analysis["inst_id"])
            if data is None:
                continue
            update = self._score_direction(analysis, data, now)
            if update:
                self._journal.update_analysis(analysis["id"], **update)
        return finished

    @staticmethod
    def _score_direction(analysis: dict, candles: list[Candle], now: float) -> dict:
        update: dict = {}
        oldest = candles[0].ts // 1000 if candles else None
        pending = False
        for hours, name in ((24, "px_24h"), (72, "px_72h")):
            if analysis[name] is not None:
                continue
            moment = analysis["ts"] + hours * 3600
            if now < moment + BAR_SECONDS:
                pending = True
                continue
            price = price_at(candles, moment)
            if price is not None:
                update[name] = price
            elif oldest is None or moment >= oldest:
                pending = True  # the candle is not closed yet, or was missing in this answer
        if not pending:
            update["scored"] = 1  # filled in, or too old for OKX's candles: nothing more to wait for
        return update

    def run_forever(self, stop: threading.Event, first_delay: float = 60.0) -> None:
        stop.wait(first_delay)
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("track record scoring crashed")
            stop.wait(SCORE_EVERY_SECONDS)


# ------------------------------------------------------------------- the card


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "—"


def _level(value: float) -> str:
    return f"{value:,.8g}"


def scorecard_markdown(journal: Journal, now: float, tz, days: int = 30, inst_id: str | None = None, window_hours: float = MAX_WINDOW_HOURS) -> str:
    """The track record of the last `days` days, in plain words, for a Feishu card."""
    start = now - days * 86400
    analyses = journal.analyses_between(start, now + 1, inst_id)
    calls = journal.calls_between(start, now + 1, inst_id)
    scope = f"最近 {days} 天" + (f" · {inst_id}" if inst_id else "")
    if not analyses:
        return (
            f"**{scope}**\n还没有记录到带点位的分析。\n"
            "问一次某个品种的走势（例如“黄金现在怎么看”），我给出的触发位、目标位和失效位就会被记下来，"
            f"之后 {window_hours:g} 小时里自动对照行情，结果在这里看。"
        )

    by_status: dict[str, list[dict]] = {}
    for call in calls:
        by_status.setdefault(call["status"], []).append(call)
    hits, fails = by_status.get("hit", []), by_status.get("failed", [])
    expired, untriggered = by_status.get("expired", []), by_status.get("untriggered", [])
    still_open, unscored = by_status.get("open", []), by_status.get("unscored", [])
    decided = len(hits) + len(fails)

    lines = [f"**{scope}**", f"记录了 {len(analyses)} 次带点位的分析，共 {len(calls)} 个情景。", "**情景**"]
    if decided:
        lines.append(f"- 有结果的 {decided} 个：命中 {len(hits)} 个，失效 {len(fails)} 个，命中率 {_pct(len(hits), decided)}")
        valued = [c["result_r"] for c in hits + fails + expired if c["result_r"] is not None]
        if valued:
            average = sum(valued) / len(valued)
            detail = f"命中时平均 {sum(c['result_r'] for c in hits) / len(hits):+.2f}R，" if hits else ""
            lines.append(f"- 平均每个触发的情景 {average:+.2f}R（{detail}失效按 -1R，到期的按到期时的价格算）")
    else:
        lines.append("- 还没有走出结果的情景")
    rest = []
    if expired:
        rest.append(f"到期没到目标也没到失效位 {len(expired)} 个")
    if untriggered:
        rest.append(f"没触发 {len(untriggered)} 个")
    if still_open:
        rest.append(f"还在观察 {len(still_open)} 个")
    if unscored:
        rest.append(f"缺行情数据没法评 {len(unscored)} 个")
    if rest:
        lines.append("- " + "；".join(rest))

    calibration = calibration_lines(hits + fails)
    if calibration:
        lines.append("**把握程度准不准 (calibration)**")
        lines.extend(calibration)

    direction = _direction_lines(analyses)
    if direction:
        lines.append("**方向判断**")
        lines.extend(direction)

    closed = sorted((c for c in calls if c["status"] in ("hit", "failed", "expired")), key=lambda c: c["closed_ts"] or 0, reverse=True)
    if closed:
        lines.append("**最近的结果**")
        for call in closed[:5]:
            when = datetime.fromtimestamp(call["ts"], tz=tz).strftime("%m-%d %H:%M")
            side = "多头" if call["side"] == "long" else "空头"
            result = "" if call["result_r"] is None else f"（{call['result_r']:+.2f}R）"
            lines.append(
                f"- {when} {call['inst_id']} {side}：触发 {_level(call['trigger_px'])}，目标 {_level(call['target_px'])}，"
                f"失效 {_level(call['invalid_px'])}，结果{STATUS_WORDS[call['status']]}{result}"
            )

    lines.append(
        f"规则：15 分钟 K 线收盘站上（空头是跌破）触发位才算触发；之后先碰到目标位算命中，先碰到失效位算失效，"
        f"{window_hours:g} 小时内都没到算到期。同一根 K 线里目标位和失效位都碰到，按失效算。"
        "R 是把“触发位到失效位的距离”当作 1 个单位。"
    )
    if decided < MIN_SAMPLE:
        lines.append(f"有结果的情景还不到 {MIN_SAMPLE} 个，这些比例现在说明不了什么，攒多了再看。")
    lines.append("这是把分析的点位和事后的行情做核对，不是交易记录，也不代表收益。")
    return "\n".join(lines)


CONFIDENCE_BANDS = ((1, 49, "50% 以下"), (50, 64, "50% 到 64%"), (65, 99, "65% 以上"))


def calibration_lines(decided: list[dict]) -> list[str]:
    """For scenarios that reached the target or the invalidation: how often each band of stated confidence was right."""
    lines = []
    for low, high, name in CONFIDENCE_BANDS:
        band = [c for c in decided if c.get("confidence") is not None and low <= c["confidence"] <= high]
        if not band:
            continue
        hits = sum(1 for c in band if c["status"] == "hit")
        stated = sum(c["confidence"] for c in band) / len(band)
        lines.append(f"- 说把握{name}的 {len(band)} 个：平均说 {stated:.0f}%，实际命中 {hits} 个（{_pct(hits, len(band))}）")
    return lines


def track_text_for_ai(journal: Journal, now: float, tz, days: int = 30) -> str:
    """The AI's own track record in a few lines, read before it answers. Figures only; empty when nothing is recorded."""
    start = now - days * 86400
    analyses = journal.analyses_between(start, now + 1)
    if not analyses:
        return ""
    calls = journal.calls_between(start, now + 1)
    decided = [c for c in calls if c["status"] in ("hit", "failed")]
    hits = sum(1 for c in decided if c["status"] == "hit")
    lines = [f"你自己最近 {days} 天的成绩单（程序按 15 分钟 K 线自动核对，不是你自己打的分）："]
    if decided:
        valued = [c["result_r"] for c in calls if c["status"] in ("hit", "failed", "expired") and c["result_r"] is not None]
        average = f"，触发过的情景平均 {sum(valued) / len(valued):+.2f}R" if valued else ""
        lines.append(f"- 有结果的情景 {len(decided)} 个：命中 {hits} 个，失效 {len(decided) - hits} 个（{_pct(hits, len(decided))}）{average}")
    else:
        lines.append(f"- 记了 {len(calls)} 个情景，还没有走出结果的")
    lines.extend(calibration_lines(decided))
    lines.extend(_direction_lines(analyses))
    settled = sorted((c for c in calls if c["status"] in ("hit", "failed", "expired")), key=lambda c: c["closed_ts"] or 0, reverse=True)
    for call in settled[:3]:
        when = datetime.fromtimestamp(call["ts"], tz=tz).strftime("%m-%d")
        side = "多头" if call["side"] == "long" else "空头"
        result = "" if call["result_r"] is None else f" {call['result_r']:+.2f}R"
        conf = f"，当时说把握 {call['confidence']}%" if call.get("confidence") is not None else ""
        lines.append(
            f"- 最近结算：{when} {call['inst_id']} {side} 触发 {_level(call['trigger_px'])} 目标 {_level(call['target_px'])} "
            f"失效 {_level(call['invalid_px'])}，{STATUS_WORDS[call['status']]}{result}{conf}"
        )
    if len(decided) < MIN_SAMPLE:
        lines.append(f"- 有结果的还不到 {MIN_SAMPLE} 个，比例只作提醒，说明不了准不准。")
    lines.append("用法：这些数字不改变你对眼下行情的判断，只用来写把握程度：最近失效得多，把握就写低一点、失效位写清楚。回答里不用提成绩单，除非他问。")
    return "\n".join(lines)


def _direction_lines(analyses: list[dict]) -> list[str]:
    lines = []
    for hours, name in ((24, "px_24h"), (72, "px_72h")):
        sample = [a for a in analyses if a["bias"] in ("long", "short") and a["ref_px"] and a[name] is not None]
        if not sample:
            continue
        right = sum(1 for a in sample if (a[name] > a["ref_px"]) == (a["bias"] == "long") and a[name] != a["ref_px"])
        rose = sum(1 for a in sample if a[name] > a["ref_px"])
        lines.append(
            f"- {hours} 小时后：给过方向的 {len(sample)} 次里对了 {right} 次（{_pct(right, len(sample))}）。"
            f"同样这 {len(sample)} 次里价格上涨的有 {rose} 次（{_pct(rose, len(sample))}），一直看多就是这个数"
        )
    return lines
