"""The account's total risk: what it would lose from here if every stop were hit.

Counted:
  * each position, from its mark price to its stop (the worst one when it has several); a part no
    stop covers, and a position with no stop at all, to its liquidation price;
  * each order waiting to open or add (limit orders, and trigger orders that would open), from its
    price to the stop that comes with it.
An order waiting without a stop, or a position with neither a stop nor a liquidation price, cannot
be measured; it is named, never guessed.

The owner caps the total at a share of the equity (TRADE_MAX_TOTAL_RISK_PCT). Several positions,
the tiers of a ladder and additions all come out of that one budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from .models import AccountState, OrderInfo, Position, size_text, to_float


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _px(value) -> str:
    return f"{float(value):,.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Piece:
    label: str  # "XAU-USDT-SWAP 多单 2 XAU，止损 3,820"
    loss: Decimal | None  # in the settlement currency; None when it cannot be measured
    inst_id: str = ""
    direction: str = ""  # long | short: the side the piece is, or would be once filled
    position: bool = False


@dataclass
class Exposure:
    pieces: list[Piece] = field(default_factory=list)

    @property
    def loss(self) -> Decimal:
        return sum((p.loss for p in self.pieces if p.loss is not None), Decimal(0))

    @property
    def unknown(self) -> list[Piece]:
        return [p for p in self.pieces if p.loss is None]

    def without(self, inst_id: str, direction: str) -> "Exposure":
        """Without the position on this side of this instrument (its stop is about to be replaced)."""
        return Exposure([p for p in self.pieces if not (p.position and p.inst_id == inst_id and p.direction == direction)])


def _side(direction: str) -> str:
    return "多" if direction == "long" else "空"


def position_piece(position: Position, coins_per_contract: Decimal) -> Piece:
    long = position.direction == "long"
    coins = _dec(position.qty) * coins_per_contract
    mark = _dec(position.mark_px or position.avg_px or 0)
    if position.unit == "手":
        head = f"{position.inst_id} {_side(position.direction)}单 {size_text(position)}"
    else:
        head = f"{position.inst_id} {_side(position.direction)}单 {float(coins):,.8g} {position.coin_ccy or ''}".rstrip()
    sl_stops = [s for s in position.stops if s.sl_trigger_px]
    if mark <= 0:
        return Piece(f"{head}，读不到价格", None, position.inst_id, position.direction, True)

    def lost(stop: Decimal, amount: Decimal) -> Decimal:
        return max(Decimal(0), (mark - stop) * amount if long else (stop - mark) * amount)

    if sl_stops:
        worst = min(_dec(s.sl_trigger_px) for s in sl_stops) if long else max(_dec(s.sl_trigger_px) for s in sl_stops)
        covered = coins
        if not any(s.whole for s in sl_stops):
            sized = [s.size for s in sl_stops if s.size]
            covered = min(coins, _dec(sum(sized)) * coins_per_contract) if len(sized) == len(sl_stops) else coins
        loss = lost(worst, covered)
        label = f"{head}，止损 {_px(worst)}"
        bare = coins - covered
        if bare > 0:
            if not position.liq_px:
                return Piece(f"{label}，另有 {float(bare):,.8g} 没有止损，也读不到强平价", None, position.inst_id, position.direction, True)
            loss += lost(_dec(position.liq_px), bare)
            label += f"，另有 {float(bare):,.8g} 没有止损（按到强平价 {_px(position.liq_px)} 算）"
        return Piece(label, loss, position.inst_id, position.direction, True)
    if position.liq_px:
        return Piece(
            f"{head}，没有止损（按到强平价 {_px(position.liq_px)} 算）", lost(_dec(position.liq_px), coins),
            position.inst_id, position.direction, True,
        )  # fmt: skip
    return Piece(f"{head}，没有止损，也读不到强平价", None, position.inst_id, position.direction, True)


def _opening_direction(side: str, pos_side: str) -> str:
    """The side an order opens or adds to; "" for an order that closes (hedge mode)."""
    if pos_side in ("long", "short"):
        opening = (side == "buy") == (pos_side == "long")
        return pos_side if opening else ""
    return "long" if side == "buy" else "short"


def order_piece(order: OrderInfo, coins_per_contract: Decimal) -> Piece | None:
    """A limit order waiting to open or add; None for one that only closes."""
    if order.reduce_only or order.px is None:
        return None
    direction = _opening_direction(order.side, order.pos_side)
    if not direction:
        return None
    coins = _dec(order.remaining) * coins_per_contract
    head = f"{order.inst_id} 挂单{'买' if order.side == 'buy' else '卖'} {_px(order.px)}（{float(coins):,.8g}）"
    if not order.sl_trigger_px:
        return Piece(f"{head}，没带止损", None, order.inst_id, direction)
    gap = abs(_dec(order.px) - _dec(order.sl_trigger_px))
    return Piece(f"{head}，止损 {_px(order.sl_trigger_px)}", gap * coins, order.inst_id, direction)


def trigger_piece(row: dict, coins_per_contract: Decimal) -> Piece | None:
    """A trigger order (计划委托) that would open or add; None for one that closes."""
    if str(row.get("reduceOnly", "")).lower() == "true":
        return None
    direction = _opening_direction(str(row.get("side", "")), str(row.get("posSide") or "net"))
    trigger = to_float(row.get("triggerPx"))
    size = to_float(row.get("sz"))
    if not direction or not trigger or not size:
        return None
    order_px = to_float(row.get("orderPx"))
    entry = _dec(order_px if order_px and order_px > 0 else trigger)
    coins = _dec(size) * coins_per_contract
    stop = None
    for attached in row.get("attachAlgoOrds") or [row]:
        if isinstance(attached, dict) and to_float(attached.get("slTriggerPx")):
            stop = _dec(to_float(attached.get("slTriggerPx")))
            break
    inst_id = str(row.get("instId", ""))
    head = f"{inst_id} 突破单{'买' if direction == 'long' else '卖'} {_px(trigger)}（{float(coins):,.8g}）"
    if stop is None:
        return Piece(f"{head}，没带止损", None, inst_id, direction)
    return Piece(f"{head}，止损 {_px(stop)}", abs(entry - stop) * coins, inst_id, direction)


def measure(
    state: AccountState, orders: list[OrderInfo], triggers: list[dict], coins_per_contract: Callable[[str], Decimal | None]
) -> Exposure:
    """Every piece of risk on the account. `coins_per_contract(inst_id)` is None for an instrument it cannot size."""
    pieces: list[Piece] = []
    for position in state.positions:
        per = coins_per_contract(position.inst_id)
        if per is None:
            pieces.append(Piece(f"{position.inst_id} {_side(position.direction)}单，读不到合约规格", None, position.inst_id, position.direction, True))
        else:
            pieces.append(position_piece(position, per))
    for order in orders:
        per = coins_per_contract(order.inst_id)
        piece = order_piece(order, per) if per is not None else Piece(f"{order.inst_id} 挂单，读不到合约规格", None, order.inst_id)
        if piece is not None:
            pieces.append(piece)
    for row in triggers:
        inst_id = str(row.get("instId", ""))
        per = coins_per_contract(inst_id)
        piece = trigger_piece(row, per) if per is not None else Piece(f"{inst_id} 突破单，读不到合约规格", None, inst_id)
        if piece is not None:
            pieces.append(piece)
    return Exposure(pieces)


def card_lines(exposure: Exposure, equity: Decimal, cap_pct: float | None, ccy: str = "USDT") -> list[str]:
    """For the 仓位 card and the AI: the total, what it is made of, and the cap."""
    if not exposure.pieces:
        return []
    total = f"约 {_px(exposure.loss)} {ccy}"
    if equity > 0:
        total += f"，占权益 {exposure.loss / equity * 100:.1f}%"
    cap = f"（你定的上限 {cap_pct:g}%）" if cap_pct else ""
    lines = [f"**账户总风险（所有止损都打到）**：{total}{cap}"]
    for piece in exposure.pieces:
        amount = f"约 {_px(piece.loss)}" if piece.loss is not None else "算不出，没算进总数"
        lines.append(f"- {piece.label}：{amount}")
    return lines
