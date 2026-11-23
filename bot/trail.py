"""Trailing stops: at 1R in profit the stop goes to break-even, then it follows the best price.

R is how far the entry is from the stop the position started with. Then, rules only:

  * Once the price has been 1R in profit, the stop goes to the entry plus BREAK_EVEN_FEES of the price,
    which pays the fees of getting in and out: "保本".
  * From there the stop follows the best price since the position was first seen, at the larger of
    TRAIL_ATR times the 1-hour ATR and 1R. The larger of the two keeps a 4-hour trade, whose R is wide,
    from being shaken out by an hour's noise.
  * Only ever towards profit, and by at least STEP_ATR of the 1-hour ATR at a time, so the group is not
    told about every dollar; never closer to the price than MIN_GAP_ATR of it.
  * A position without a stop is left alone. Someone moving the stop back, further from the price,
    stops the trailing of that position until it is closed: the owners' own call wins.
  * "停止移动止损" stops all of it and "开启移动止损" starts it again (kept across restarts).

Every move goes to OKX through the trade desk, like "止损 XAU 价格", but without a confirmation code:
the owner agreed to this one exception to "动仓位先经他同意" on 2026-11-23. Each move is told in the private
chat and in the group, with why.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

from .indicators import atr
from .okx_client import OkxError, OkxNetworkError

log = logging.getLogger(__name__)

BREAK_EVEN_R = 1.0
BREAK_EVEN_FEES = 0.001  # 0.05% to get in and 0.05% to get out, at OKX's taker rate
TRAIL_ATR = 2.0
STEP_ATR = 0.5
MIN_GAP_ATR = 0.5
ATR_BAR = "1H"
ATR_TTL_SECONDS = 600.0
CHECK_EVERY_SECONDS = 30
ERROR_QUIET_SECONDS = 3600  # a move that failed is told once an hour at most
SETTING = "trail_stop"  # "on" | "off"; empty: TRAIL_STOP in .env
STATE_SETTING = "trail_state"
ON_WORDS = ("开启移动止损", "打开移动止损", "开移动止损", "恢复移动止损", "/trail on")
OFF_WORDS = ("停止移动止损", "关闭移动止损", "关掉移动止损", "关移动止损", "暂停移动止损", "/trail off")
STATUS_WORDS = ("移动止损", "/trail")


def _num(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


class TrailingStops:
    def __init__(
        self,
        cfg,
        journal,
        okx,
        desk,
        tell: Callable[[str, str], None],
        expect: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        """`okx` reads the account and the candles (the account orders go to). `desk` moves a stop
        (TradeDesk.move_stop). `tell(title, text)` reaches the owners and the group. `expect(inst_id, line)`
        puts why into the card the position monitor sends when it sees the stop change."""
        self._cfg = cfg
        self._journal = journal
        self._okx = okx
        self._desk = desk
        self._tell = tell
        self._expect = expect
        self._clock = clock
        self._atrs: dict[str, tuple[float, float | None]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ switch

    def enabled(self) -> bool:
        value = self._journal.get_setting(SETTING, "")
        return value == "on" if value else bool(self._cfg.trail_stop)

    def handle(self, text: str) -> str | None:
        """ "停止移动止损", "开启移动止损", "移动止损": the reply, or None when the message is none of them."""
        said = text.strip().strip(" 。.!！~").lower()
        if said in ON_WORDS:
            self._journal.set_setting(SETTING, "on")
            log.info("trailing stops switched on")
            return "移动止损开了：浮盈到 1R 止损移到保本，之后按最好价减 2 倍 1 小时 ATR（不小于 1R）跟着走，只往有利方向移，每移一次都告诉你和群里。发“停止移动止损”关掉。"
        if said in OFF_WORDS:
            self._journal.set_setting(SETTING, "off")
            log.info("trailing stops switched off")
            return "移动止损关了，止损不会再自动移动，已经移过的不变。发“开启移动止损”再打开。"
        if said in STATUS_WORDS:
            return self.status_text()
        return None

    def status_text(self) -> str:
        if not self.enabled():
            return "移动止损现在关着。发“开启移动止损”打开。"
        lines = ["移动止损开着：浮盈到 1R 止损移到保本，之后按最好价减 2 倍 1 小时 ATR（不小于 1R）跟着走。发“停止移动止损”关掉。"]
        for key, item in sorted(self._load().items()):
            inst, _, direction = key.split("|")
            side = "多" if direction == "long" else "空"
            if item.get("off"):
                lines.append(f"- {inst.split('-')[0]} {side}单：止损被手动改回过，这一单不再自动移")
            elif item.get("set") is not None:
                lines.append(f"- {inst.split('-')[0]} {side}单：已经移到 {_num(item['set'])}")
            elif item.get("r"):
                lines.append(f"- {inst.split('-')[0]} {side}单：1R = {_num(item['r'])}，浮盈到 1R 再动")
        return "\n".join(lines)

    # ------------------------------------------------------------------- state

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._journal.get_setting(STATE_SETTING, "") or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, states: dict[str, dict]) -> None:
        self._journal.set_setting(STATE_SETTING, json.dumps(states, ensure_ascii=False))

    def _atr(self, inst_id: str) -> float | None:
        now = self._clock()
        known = self._atrs.get(inst_id)
        if known and now - known[0] < ATR_TTL_SECONDS:
            return known[1]
        try:
            candles = [candle for candle in self._okx.candles(inst_id, ATR_BAR, 60) if candle.confirmed]
            value = atr(candles, 14)
        except (OkxError, OkxNetworkError) as exc:
            log.info("1-hour ATR of %s not read: %s", inst_id, exc)
            return known[1] if known else None
        self._atrs[inst_id] = (now, value)
        return value

    # -------------------------------------------------------------------- rule

    @staticmethod
    def target(item: dict, long: bool, mark: float, stop: float, hour_atr: float) -> tuple[float | None, str]:
        """Where the stop should be now, and why; (None, "") when it should stay. Pure arithmetic."""
        sign = 1 if long else -1
        avg, best, r = item["avg"], item["best"], item.get("r")
        candidates: list[tuple[float, str]] = []
        if r:
            if (best - avg) * sign >= BREAK_EVEN_R * r:
                even = avg * (1 + sign * BREAK_EVEN_FEES)
                candidates.append((even, f"浮盈到过 1R（{_num(r)}），止损移到保本：开仓均价 {_num(avg)} {'加' if long else '减'}手续费"))
                gap = max(TRAIL_ATR * hour_atr, r)
                follow = best - sign * gap
                if (follow - even) * sign > 0:
                    candidates.append((follow, f"跟随最好价 {_num(best)}，距离 {_num(gap)}（2 倍 1 小时 ATR 和 1R 取大的）"))
        else:  # the stop was already past the entry when the position was first seen: follow only
            gap = TRAIL_ATR * hour_atr
            candidates.append((best - sign * gap, f"跟随最好价 {_num(best)}，距离 {_num(gap)}（2 倍 1 小时 ATR）"))
        if not candidates:
            return None, ""
        price, why = max(candidates, key=lambda item: item[0] * sign)
        if (price - stop) * sign < STEP_ATR * hour_atr:
            return None, ""  # not worth a move yet
        if (mark - price) * sign < MIN_GAP_ATR * hour_atr:
            return None, ""  # too close to the price: wait until it is clear
        return price, why

    # -------------------------------------------------------------------- loop

    def tick(self) -> list[str]:
        """One look at every position. Returns what was moved, in words."""
        if not self.enabled() or not self._desk.can_move_stops():
            return []
        with self._lock:
            state = self._okx.account_state()
            states = self._load()
            seen: dict[str, dict] = {}
            moved: list[str] = []
            for position in state.positions:
                if position.inst_id not in self._cfg.tradable_instruments:
                    continue
                key = "|".join(position.key)
                item = self._follow(position, states.get(key))
                if item is None:
                    continue
                seen[key] = item
                said = self._consider(position, item)
                if said:
                    moved.append(said)
            if seen != states:
                self._save(seen)  # positions that are gone are forgotten
            return moved

    def _follow(self, position, item: dict | None) -> dict | None:
        """The position's record, updated with this look. None when it has nothing to follow yet."""
        avg, mark, stop = position.avg_px, position.mark_px, position.stop_loss
        if not avg or not mark:
            return item
        long = position.direction == "long"
        if item is None or abs(float(item.get("avg") or 0) - avg) > avg * 1e-6:
            # A new position, or one added to (a new average price, and the owner set its stop): start afresh,
            # R from the stop it has now while that is on the losing side.
            if stop is None:
                return None
            losing = (avg - stop) * (1 if long else -1) > 0
            item = {"avg": avg, "r": abs(avg - stop) if losing else None, "best": mark, "set": None, "off": False}
        best = float(item.get("best") or mark)
        return {**item, "best": max(best, mark) if long else min(best, mark)}

    def _consider(self, position, item: dict) -> str:
        stop = position.stop_loss
        if stop is None or item.get("off"):
            return ""
        long = position.direction == "long"
        sign = 1 if long else -1
        name = f"{position.inst_id.split('-')[0]} {'多' if long else '空'}单"
        if item.get("set") is not None and (item["set"] - stop) * sign > item["set"] * 1e-5:
            item["off"] = True
            log.info("trailing: the stop of %s was moved back by hand; this position is no longer trailed", position.inst_id)
            self._safely_tell(
                f"移动止损：{name}不再自动移",
                f"止损从我移到的 {_num(item['set'])} 被改回了 {_num(stop)}。按你们自己的改动来：这个仓位平掉之前，我不再自动移它的止损。",
            )
            return ""
        hour_atr = self._atr(position.inst_id)
        if not hour_atr:
            return ""
        price, why = self.target(item, long, position.mark_px, stop, hour_atr)
        if price is None:
            return ""
        self._note(position.inst_id, f"为什么：{why}。发“停止移动止损”可以关掉")
        try:
            result, lines, price = self._desk.move_stop(position.inst_id, position.direction, price)
        except Exception as exc:  # Refused, or OKX could not be read
            self._note(position.inst_id, "")
            self._failed(item, name, str(exc))
            return ""
        if result != "ok":
            self._note(position.inst_id, "")
            self._failed(item, name, "；".join(lines))
            return ""
        item["set"] = price
        item.pop("error_at", None)
        log.info("trailing: the stop of %s moved to %s", position.inst_id, _num(price))
        return f"{name} 止损 {_num(stop)} → {_num(price)}"

    def _note(self, inst_id: str, line: str) -> None:
        if self._expect is not None:
            try:
                self._expect(inst_id, line)
            except Exception:
                log.exception("could not leave a note for the stop-change card")

    def _failed(self, item: dict, name: str, why: str) -> None:
        now = self._clock()
        log.warning("trailing: a stop could not be moved: %s", why)
        if now - float(item.get("error_at") or 0) < ERROR_QUIET_SECONDS:
            return
        item["error_at"] = now
        self._safely_tell(f"移动止损没移成：{name}", f"{why}\n止损还在原来的位置。请发“仓位”看一下，需要的话自己发“止损 品种 价格”。")

    def _safely_tell(self, title: str, text: str) -> None:
        try:
            self._tell(title, text)
        except Exception:
            log.exception("could not tell about a trailing stop")

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except (OkxError, OkxNetworkError) as exc:
                log.info("trailing: the account could not be read (%s)", exc)
            except Exception:
                log.exception("trailing stops: a round failed")
            stop.wait(CHECK_EVERY_SECONDS)
