"""How positions closed, from OKX's own records, and the circuit breaker built on them.

Two read-only sources:

  /api/v5/account/positions-history   each closed or partly closed position: open and close price,
                                      realised P&L with fees and funding, and how it closed
                                      (by an order, liquidated, auto-deleveraged).
  /api/v5/trade/orders-algo-history   take-profit / stop-loss orders that went off, and which side
                                      fired (`actualSide`: "sl" or "tp").

The close alert uses them to report what a close really made, instead of the floating P&L of the
last poll. The circuit breaker (BREAKER_STOPS, BREAKER_HOURS) counts losing stop-outs and
liquidations: after that many within that many hours, new positions are refused until the owner
lifts it with a confirmation code. The breaker never touches a position or an order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import to_float

log = logging.getLogger(__name__)

CLOSE_TYPES = {"1": "部分平仓", "2": "全部平仓", "3": "强平", "4": "部分强平", "5": "部分自动减仓 (ADL)", "6": "自动减仓 (ADL)"}
LIQUIDATION_TYPES = ("3", "4")
ADL_TYPES = ("5", "6")
# The record OKX leaves when a position is gone: closed in full, liquidated in full, or deleveraged in full.
FULL_CLOSE_TYPES = ("2", "3", "6")
# A stop that went off and the close it caused are recorded this close together, in seconds.
BEFORE_TRIGGER_SECONDS = 60
AFTER_TRIGGER_SECONDS = 300


def _seconds(value) -> float | None:
    ms = to_float(value)
    return ms / 1000 if ms else None


@dataclass(frozen=True)
class Closed:
    """One row of OKX's position history."""

    inst_id: str
    direction: str  # long | short
    close_type: str  # OKX's code, see CLOSE_TYPES
    open_px: float | None
    close_px: float | None
    closed_size: float | None  # contracts
    pnl: float | None  # from the price alone
    fee: float | None  # negative when paid
    funding: float | None  # negative when paid
    liq_penalty: float | None
    realized: float | None  # pnl + fee + funding + liq_penalty, as OKX adds it up
    ccy: str
    closed_at: float  # seconds

    @classmethod
    def from_okx(cls, row: dict) -> Closed | None:
        closed_at = _seconds(row.get("uTime"))
        inst_id = str(row.get("instId", ""))
        if not inst_id or closed_at is None:
            return None
        direction = str(row.get("direction", "")) or ("short" if row.get("posSide") == "short" else "long")
        return cls(
            inst_id=inst_id,
            direction=direction,
            close_type=str(row.get("type", "")),
            open_px=to_float(row.get("openAvgPx")),
            close_px=to_float(row.get("closeAvgPx")),
            closed_size=to_float(row.get("closeTotalPos")),
            pnl=to_float(row.get("pnl")),
            fee=to_float(row.get("fee")),
            funding=to_float(row.get("fundingFee")),
            liq_penalty=to_float(row.get("liqPenalty")),
            realized=to_float(row.get("realizedPnl")),
            ccy=str(row.get("ccy", "")),
            closed_at=closed_at,
        )


@dataclass(frozen=True)
class Trigger:
    """A take-profit / stop-loss order that went off."""

    inst_id: str
    side: str  # sl | tp
    at: float  # seconds
    trigger_px: float | None

    @classmethod
    def from_okx(cls, row: dict) -> Trigger | None:
        side = str(row.get("actualSide", ""))
        at = _seconds(row.get("triggerTime")) or _seconds(row.get("uTime"))
        inst_id = str(row.get("instId", ""))
        if side not in ("sl", "tp") or at is None or not inst_id or row.get("state", "effective") != "effective":
            return None
        return cls(inst_id, side, at, to_float(row.get("slTriggerPx" if side == "sl" else "tpTriggerPx")))


@dataclass(frozen=True)
class Hit:
    """A losing stop-out or a liquidation: what the circuit breaker counts."""

    inst_id: str
    kind: str  # 止损 | 强平
    at: float
    realized: float | None
    ccy: str


def read_triggers(reader) -> list[Trigger]:
    rows = [*reader.algo_history("conditional"), *reader.algo_history("oco")]
    triggers = [Trigger.from_okx(row) for row in rows if isinstance(row, dict)]
    return sorted((t for t in triggers if t is not None), key=lambda t: t.at)


def read_closed(reader) -> list[Closed]:
    closed = [Closed.from_okx(row) for row in reader.position_history() if isinstance(row, dict)]
    return sorted((c for c in closed if c is not None), key=lambda c: c.closed_at)


def _caused_by(closed: Closed, trigger: Trigger) -> bool:
    return (
        closed.inst_id == trigger.inst_id
        and -BEFORE_TRIGGER_SECONDS <= closed.closed_at - trigger.at <= AFTER_TRIGGER_SECONDS
    )


def trigger_for(closed: Closed, triggers: list[Trigger]) -> Trigger | None:
    """The stop or take-profit that closed this position, if one did."""
    candidates = [t for t in triggers if _caused_by(closed, t)]
    return min(candidates, key=lambda t: abs(closed.closed_at - t.at)) if candidates else None


def how_it_closed(closed: Closed, triggers: list[Trigger]) -> str:
    if closed.close_type in LIQUIDATION_TYPES:
        return "强平"
    if closed.close_type in ADL_TYPES:
        return "自动减仓 (ADL)"
    trigger = trigger_for(closed, triggers)
    if trigger is not None:
        return "止损单触发" if trigger.side == "sl" else "止盈单触发"
    return "平仓单（不是止盈止损单触发的）"


def stop_hits(closed: list[Closed], triggers: list[Trigger], since: float) -> list[Hit]:
    """Losing stop-outs and liquidations at or after `since`, oldest first.

    A stop that went off counts unless the close it caused is known to have made money on the price
    (a stop moved into profit is not a loss). The price alone decides, not the realised P&L: that
    takes in the fees and the funding paid over the whole holding, so a stop moved to break-even on a
    position held for days would look like a loss. A stop whose close cannot be found still counts:
    when in doubt the breaker errs towards caution."""
    hits: list[Hit] = []
    matched: set[int] = set()
    for trigger in triggers:
        if trigger.side != "sl" or trigger.at < since:
            continue
        candidates = [(index, c) for index, c in enumerate(closed) if index not in matched and _caused_by(c, trigger)]
        match = min(candidates, key=lambda pair: abs(pair[1].closed_at - trigger.at)) if candidates else None
        if match is not None:
            matched.add(match[0])
            on_price = match[1].pnl if match[1].pnl is not None else match[1].realized
            if on_price is not None and on_price >= 0:
                continue
        hits.append(
            Hit(trigger.inst_id, "止损", trigger.at, match[1].realized if match else None, match[1].ccy if match else "")
        )
    for index, c in enumerate(closed):
        if c.close_type in LIQUIDATION_TYPES and c.closed_at >= since and index not in matched:
            hits.append(Hit(c.inst_id, "强平", c.closed_at, c.realized, c.ccy))
    return sorted(hits, key=lambda hit: hit.at)


def _amount(value: float, ccy: str) -> str:
    return f"{value:+,.2f} {ccy}" if ccy else f"{value:+,.2f}"


def hit_line(hit: Hit, tz: ZoneInfo) -> str:
    when = datetime.fromtimestamp(hit.at, tz).strftime("%m-%d %H:%M")
    money = f"，已实现 {_amount(hit.realized, hit.ccy)}" if hit.realized is not None else ""
    return f"{when} {hit.inst_id} {hit.kind}{money}"


def close_lines(closed: Closed, triggers: list[Trigger]) -> tuple[str, ...]:
    """What a close really made, for the owner's alert. Amounts in the position's settlement currency."""
    lines = [f"平仓方式：{how_it_closed(closed, triggers)}（{CLOSE_TYPES.get(closed.close_type, '平仓')}）"]
    prices = []
    if closed.open_px is not None:
        prices.append(f"开仓均价 {closed.open_px:,.2f}")
    if closed.close_px is not None:
        prices.append(f"平仓均价 {closed.close_px:,.2f}")
    if prices:
        lines.append("，".join(prices))
    if closed.realized is not None:
        parts = []
        for label, value in (("价差", closed.pnl), ("手续费", closed.fee), ("资金费", closed.funding), ("强平罚金", closed.liq_penalty)):
            if value:
                parts.append(f"{label} {value:+,.2f}")
        detail = f"（{'，'.join(parts)}）" if parts else ""
        lines.append(f"已实现盈亏 {_amount(closed.realized, closed.ccy)}{detail}")
    return tuple(lines)
