"""Orders waiting on OKX, whoever placed them, as plain text for the AI.

Read-only. Two sources:

  /api/v5/trade/orders-pending        limit orders and the like, on every kind of instrument
  /api/v5/trade/orders-algo-pending   take-profit / stop-loss orders, trigger orders (计划委托)
                                      and trailing stops (移动止盈止损)

The owner and his friend place orders in the OKX app as well as through the bot. The AI is
shown all of them, says which were placed by hand, and sees for each one how far it is from the
market, what it adds to the position, and what its stop would cost against the equity. Without
this the AI saw the positions only, and a question about the orders could not be answered.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import to_float
from .okx_client import OkxError, OkxNetworkError
from .okx_trade import ORDER_TAG

log = logging.getLogger(__name__)

# OKX takes one algo order type per request, except take-profit/stop-loss and OCO, which go together.
ALGO_TYPES = ("conditional,oco", "trigger", "move_order_stop")
ALGO_NAMES = {"conditional": "止盈止损单", "oco": "止盈止损单（二选一）", "trigger": "计划委托", "move_order_stop": "移动止盈止损"}
ORDER_NAMES = {
    "limit": "限价单", "post_only": "只做 maker 的限价单", "fok": "FOK 限价单", "ioc": "IOC 限价单",
    "market": "市价单", "optimal_limit_ioc": "市价单",
}  # fmt: skip
PRICE_NAMES = {"last": "最新价", "mark": "标记价", "index": "指数价"}
MAX_SHOWN = 30


@dataclass(frozen=True)
class Book:
    orders: list[dict]  # rows of orders-pending
    algos: list[dict]  # rows of orders-algo-pending
    missing: tuple[str, ...] = ()  # what could not be read, in words


def read_book(okx) -> Book:
    """Every order waiting on the account. A part that cannot be read is named in `missing`, never guessed."""
    missing: list[str] = []
    try:
        orders = [row for row in okx.pending_orders_raw() if isinstance(row, dict)]
    except (OkxError, OkxNetworkError) as exc:
        log.warning("could not read the open orders: %s", exc)
        orders, missing = [], ["普通委托（限价单等）"]
    algos: list[dict] = []
    for ord_type in ALGO_TYPES:
        try:
            algos.extend(row for row in okx.pending_algos(ord_type) if isinstance(row, dict))
        except (OkxError, OkxNetworkError) as exc:
            log.warning("could not read the pending %s orders: %s", ord_type, exc)
            missing.append("、".join(ALGO_NAMES.get(kind, kind) for kind in ord_type.split(",")))
    return Book(orders, algos, tuple(dict.fromkeys(missing)))


class _Market:
    """Contract sizes and last prices, read once per instrument while the text is written."""

    def __init__(self, okx):
        self._okx = okx
        self._specs: dict[str, object] = {}
        self._last: dict[str, float | None] = {}

    def coins(self, inst_id: str, inst_type: str, contracts: float | None) -> tuple[float | None, str]:
        """What `contracts` add up to, and the unit: (10.0, "XAU"). Spot sizes are coins already."""
        if contracts is None:
            return None, ""
        if inst_type in ("SPOT", "MARGIN"):
            return contracts, inst_id.split("-")[0]
        if inst_id not in self._specs:
            try:
                self._specs[inst_id] = self._okx.instrument_spec(inst_id)
            except Exception as exc:  # noqa: BLE001  only for display: the size is then given in contracts
                log.info("no contract size for %s: %s", inst_id, exc)
                self._specs[inst_id] = None
        spec = self._specs[inst_id]
        if spec is None:
            return None, ""
        return contracts * float(spec.coins_per_contract), spec.ct_val_ccy

    def last(self, inst_id: str) -> float | None:
        if inst_id not in self._last:
            try:
                self._last[inst_id] = self._okx.ticker(inst_id).last
            except Exception as exc:  # noqa: BLE001  only for display: the distance is then left out
                log.info("no price for %s: %s", inst_id, exc)
                self._last[inst_id] = None
        return self._last[inst_id]


def _px(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:,.2f}" if value >= 100 else f"{value:,.6g}"


def _plain(value: float) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _distance(price: float | None, last: float | None) -> str:
    if price is None or not last:
        return ""
    gap = price - last
    return f"，离现价 {_px(last)} {'高' if gap > 0 else '低'} {abs(gap):,.2f}（{abs(gap) / last * 100:.2f}%）"


def _who(row: dict) -> str:
    tag = str(row.get("tag", ""))
    client_id = str(row.get("clOrdId") or row.get("algoClOrdId") or "")
    return "机器人下的" if tag == ORDER_TAG or client_id.startswith("ftb") else "在 OKX 里手动下的"


def _when(row: dict, tz: ZoneInfo) -> str:
    ms = to_float(row.get("cTime"))
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%m-%d %H:%M") if ms else ""


def _direction(row: dict) -> str:
    """买入开多 / 卖出平多 in hedge mode; 买入 / 卖出 (只减仓) in one-way mode."""
    side = "买入" if row.get("side") == "buy" else "卖出"
    pos_side = str(row.get("posSide") or "net")
    if pos_side in ("long", "short"):
        opening = (row.get("side") == "buy") == (pos_side == "long")
        return f"{side}{'开' if opening else '平'}{'多' if pos_side == 'long' else '空'}"
    return side + ("（只减仓）" if str(row.get("reduceOnly", "")).lower() == "true" else "")


def _opens(row: dict) -> bool:
    """Whether the order adds to a position (or starts one) rather than closing it."""
    if str(row.get("reduceOnly", "")).lower() == "true":
        return False
    pos_side = str(row.get("posSide") or "net")
    if pos_side in ("long", "short"):
        return (row.get("side") == "buy") == (pos_side == "long")
    return True


def _trigger(kind: str, row: dict, prefix: str) -> str:
    """"止损 3,816.00（标记价触发，市价）"."""
    trigger = to_float(row.get(f"{prefix}TriggerPx"))
    if trigger is None:
        return ""
    order_px = to_float(row.get(f"{prefix}OrdPx"))
    price_type = PRICE_NAMES.get(str(row.get(f"{prefix}TriggerPxType") or ""), "")
    how = "市价" if order_px is None or order_px == -1 else f"限价 {_px(order_px)}"
    return f"{kind} {_px(trigger)}（{price_type + '触发，' if price_type else ''}{how}）"


def _attached(row: dict) -> str:
    """The take-profit / stop-loss that comes with an order once it fills."""
    parts: list[str] = []
    for attached in row.get("attachAlgoOrds") or [row]:
        if not isinstance(attached, dict):
            continue
        for kind, prefix in (("止损", "sl"), ("止盈", "tp")):
            text = _trigger(kind, attached, prefix)
            if text and text not in parts:
                parts.append(text)
    return "，成交后带" + "、".join(parts) if parts else ""


def _stop_cost(row: dict, entry: float | None, coins: float | None, equity: float | None) -> str:
    """What the stop that comes with an opening order would cost, against the equity."""
    if entry is None or not coins:
        return ""
    for attached in row.get("attachAlgoOrds") or [row]:
        if not isinstance(attached, dict):
            continue
        stop = to_float(attached.get("slTriggerPx"))
        if stop is not None:
            loss = abs(entry - stop) * coins
            share = f"，占权益 {loss / equity * 100:.1f}%" if equity else ""
            return f"；成交后打到止损约亏 {loss:,.2f}{share}"
    return ""


def _order_line(row: dict, market: _Market, equity: float | None, tz: ZoneInfo) -> str:
    inst_id = str(row.get("instId", ""))
    inst_type = str(row.get("instType", ""))
    contracts = to_float(row.get("sz"))
    filled = to_float(row.get("accFillSz")) or 0.0
    coins, unit = market.coins(inst_id, inst_type, contracts)
    px = to_float(row.get("px"))
    name = ORDER_NAMES.get(str(row.get("ordType", "")), str(row.get("ordType", "")) or "委托")
    if inst_type in ("SPOT", "MARGIN"):
        size = f"{_plain(contracts or 0)} {inst_id.split('-')[0]}"
    elif coins is not None:
        size = f"{_plain(coins)} {unit}（{_plain(contracts or 0)} 张）"
    else:
        size = f"{_plain(contracts or 0)} 张"
    text = f"{inst_id} {name} {_direction(row)} {size}，价格 {_px(px)}{_distance(px, market.last(inst_id))}"
    if filled:
        text += f"，已成交 {_plain(filled)} 张"
    if coins is not None and px:
        notional = coins * px
        text += f"，名义价值约 {notional:,.2f}"
        lever = to_float(row.get("lever"))
        if lever:
            text += f"，按 {lever:g} 倍约占保证金 {notional / lever:,.2f}"
    text += _attached(row)
    if _opens(row):
        text += _stop_cost(row, px, coins, equity)
    when = _when(row, tz)
    return f"{text}；{_who(row)}" + (f"，{when} 下单" if when else "")


def _algo_line(row: dict, market: _Market, tz: ZoneInfo) -> str:
    inst_id = str(row.get("instId", ""))
    kind = str(row.get("ordType", ""))
    name = ALGO_NAMES.get(kind, kind or "策略委托")
    last = market.last(inst_id)
    whole = str(row.get("closeFraction", "")) == "1"
    contracts = to_float(row.get("sz"))
    coins, unit = market.coins(inst_id, str(row.get("instType", "")), contracts)
    size = "整个仓位" if whole or not contracts else (f"{_plain(coins)} {unit}" if coins is not None else f"{_plain(contracts)} 张")
    head = f"{inst_id} {name} {_direction(row)} {size}"
    if kind == "trigger":
        trigger = to_float(row.get("triggerPx"))
        price_type = PRICE_NAMES.get(str(row.get("triggerPxType") or ""), "")
        order_px = to_float(row.get("orderPx"))
        how = "按市价" if order_px is None or order_px == -1 else f"挂限价 {_px(order_px)}"
        text = f"{head}：{price_type or '价格'}到 {_px(trigger)} 时{how}{_distance(trigger, last)}{_attached(row)}"
    elif kind == "move_order_stop":
        ratio = to_float(row.get("callbackRatio"))
        spread = to_float(row.get("callbackSpread"))
        back = f"回撤 {ratio * 100:g}%" if ratio else f"回撤 {_px(spread)}" if spread else "回撤（幅度没读到）"
        active = to_float(row.get("activePx"))
        text = f"{head}：{back} 触发" + (f"，激活价 {_px(active)}{_distance(active, last)}" if active else "")
    else:
        parts = [part for part in (_trigger("止损", row, "sl"), _trigger("止盈", row, "tp")) if part]
        prices = [to_float(row.get("slTriggerPx")), to_float(row.get("tpTriggerPx"))]
        far = "；".join(
            f"{label}{_distance(price, last).lstrip('，')}" for label, price in zip(("止损", "止盈"), prices) if price is not None and last
        )
        text = f"{head}：{'，'.join(parts) or '没有触发价'}" + (f"（{far}）" if far else "")
    when = _when(row, tz)
    return f"{text}；{_who(row)}" + (f"，{when} 下单" if when else "")


def _raw_line(row: dict, exc: Exception) -> str:
    """An order whose fields did not read as expected, given as OKX wrote it so nothing is hidden."""
    log.warning("could not write out order %s: %s", row.get("ordId") or row.get("algoId"), exc)
    keys = ("instId", "ordType", "side", "posSide", "sz", "px", "triggerPx", "slTriggerPx", "tpTriggerPx", "tag")
    return "OKX 原样：" + "，".join(f"{key}={row[key]}" for key in keys if row.get(key) not in (None, ""))


def _safe(write, row: dict, *args) -> str:
    try:
        return write(row, *args)
    except Exception as exc:  # noqa: BLE001  one odd row must not hide the others
        return _raw_line(row, exc)


def book_text(book: Book, okx, equity: float | None, tz: ZoneInfo) -> str:
    """The orders as the AI reads them."""
    market = _Market(okx)
    lines: list[str] = []
    if not book.orders and not book.algos:
        lines.append("挂着的委托：没有。" if not book.missing else "挂着的委托：没读到。")
    if book.orders:
        lines.append(f"挂着的普通委托（{len(book.orders)} 张，还没成交或只成交了一部分）：")
        lines.extend(f"- {_safe(_order_line, row, market, equity, tz)}" for row in book.orders[:MAX_SHOWN])
    if book.algos:
        lines.append(f"挂着的止盈止损、计划委托和移动止损（{len(book.algos)} 张，触发前不占仓位）：")
        lines.extend(f"- {_safe(_algo_line, row, market, tz)}" for row in book.algos[:MAX_SHOWN])
    if len(book.orders) > MAX_SHOWN or len(book.algos) > MAX_SHOWN:
        lines.append(f"（太多了，每类只列了前 {MAX_SHOWN} 张。）")
    if book.missing:
        lines.append(f"这次没读到：{'、'.join(book.missing)}。不要猜它们，直接说没读到。")
    return "\n".join(lines)
