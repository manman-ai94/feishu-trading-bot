"""Plain data objects shared by the rest of the bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


def to_decimal(value) -> Decimal | None:
    """Exact numbers for order sizes and prices, where a float's rounding would matter."""
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def to_float(value) -> float | None:
    """OKX sends every number as a string, and "" when a field does not apply."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class StopOrder:
    """A take-profit / stop-loss attached to a position."""

    algo_id: str
    sl_trigger_px: float | None = None
    tp_trigger_px: float | None = None


@dataclass(frozen=True)
class Position:
    inst_id: str
    inst_type: str
    pos_side: str  # "long", "short" or "net"
    mgn_mode: str  # "cross" or "isolated"
    qty: float  # contracts; always positive, see `direction`
    direction: str  # "long" or "short"
    avg_px: float | None = None
    mark_px: float | None = None
    upl: float | None = None
    upl_ratio: float | None = None
    lever: float | None = None
    liq_px: float | None = None
    notional_usd: float | None = None
    mgn_ratio: float | None = None
    pos_id: str = ""
    u_time: int = 0
    stops: tuple[StopOrder, ...] = ()
    ccy: str = ""  # the currency the margin and the PnL are counted in, e.g. USDT
    coins: float | None = None  # what the contracts add up to, e.g. 10 (XAU); filled in from the contract size
    coin_ccy: str = ""

    @property
    def pnl_ccy(self) -> str:
        """The unit of `upl`. OKX counts a USDT-settled contract's PnL in USDT, not USD."""
        if self.ccy:
            return self.ccy
        parts = self.inst_id.split("-")
        return parts[1] if len(parts) >= 2 and parts[1] in ("USDT", "USDC") else "USD"

    @property
    def key(self) -> tuple[str, str, str]:
        """Identifies "the same position" across two polls."""
        return (self.inst_id, self.mgn_mode, self.direction)

    @property
    def liq_distance_pct(self) -> float | None:
        """How far the mark price is from the liquidation price, in percent."""
        if not self.liq_px or not self.mark_px:
            return None
        return abs(self.mark_px - self.liq_px) / self.mark_px * 100.0

    @property
    def stop_loss(self) -> float | None:
        prices = [s.sl_trigger_px for s in self.stops if s.sl_trigger_px]
        if not prices:
            return None
        # The stop that would trigger first: highest for a long, lowest for a short.
        return max(prices) if self.direction == "long" else min(prices)

    @property
    def take_profit(self) -> float | None:
        prices = [s.tp_trigger_px for s in self.stops if s.tp_trigger_px]
        if not prices:
            return None
        return min(prices) if self.direction == "long" else max(prices)

    @classmethod
    def from_okx(cls, row: dict) -> "Position | None":
        raw_qty = to_float(row.get("pos"))
        if not raw_qty:
            return None  # OKX can return empty rows for closed positions
        pos_side = row.get("posSide") or "net"
        if pos_side in ("long", "short"):
            direction = pos_side
        else:
            direction = "long" if raw_qty > 0 else "short"
        stops = tuple(
            StopOrder(
                algo_id=str(item.get("algoId", "")),
                sl_trigger_px=to_float(item.get("slTriggerPx")),
                tp_trigger_px=to_float(item.get("tpTriggerPx")),
            )
            for item in (row.get("closeOrderAlgo") or [])
        )
        return cls(
            inst_id=row.get("instId", ""),
            inst_type=row.get("instType", ""),
            pos_side=pos_side,
            mgn_mode=row.get("mgnMode", ""),
            qty=abs(raw_qty),
            direction=direction,
            avg_px=to_float(row.get("avgPx")),
            mark_px=to_float(row.get("markPx")),
            upl=to_float(row.get("upl")),
            upl_ratio=to_float(row.get("uplRatio")),
            lever=to_float(row.get("lever")),
            liq_px=to_float(row.get("liqPx")),
            notional_usd=to_float(row.get("notionalUsd")),
            mgn_ratio=to_float(row.get("mgnRatio")),
            pos_id=str(row.get("posId", "")),
            u_time=int(to_float(row.get("uTime")) or 0),
            stops=stops,
            ccy=str(row.get("ccy", "") or ""),
        )


@dataclass(frozen=True)
class CurrencyBalance:
    """One currency of the account. Every amount is in that currency, except eq_usd."""

    ccy: str
    eq: float | None = None
    eq_usd: float | None = None
    avail_eq: float | None = None  # free to open new positions with
    upl: float | None = None
    mgn_ratio: float | None = None
    avail_bal: float | None = None  # free to trade with; the only "free" figure of a spot-mode account

    @classmethod
    def from_okx(cls, row: dict) -> "CurrencyBalance":
        return cls(
            ccy=str(row.get("ccy", "")),
            eq=to_float(row.get("eq")),
            eq_usd=to_float(row.get("eqUsd")),
            avail_eq=to_float(row.get("availEq")),
            upl=to_float(row.get("upl")),
            mgn_ratio=to_float(row.get("mgnRatio")),
            avail_bal=to_float(row.get("availBal")),
        )


@dataclass(frozen=True)
class AccountSnapshot:
    total_eq: float | None = None  # total equity in USD
    adj_eq: float | None = None
    avail_eq: float | None = None
    upl: float | None = None  # in USD; OKX leaves it empty for accounts that keep margin per currency
    mgn_ratio: float | None = None  # liquidation starts at <= 1.0 (OKX: adjEq / mmr)
    imr: float | None = None
    mmr: float | None = None
    notional_usd: float | None = None
    u_time: int = 0
    currencies: tuple[CurrencyBalance, ...] = ()  # largest first

    @classmethod
    def from_okx(cls, row: dict) -> "AccountSnapshot":
        currencies = sorted(
            (CurrencyBalance.from_okx(item) for item in (row.get("details") or []) if item.get("ccy")),
            key=lambda item: -(item.eq_usd or 0.0),
        )
        mgn_ratio = to_float(row.get("mgnRatio"))
        if mgn_ratio is None:
            # "Futures mode" accounts (one margin pool per currency) report the ratio
            # per currency only. The lowest one is the one closest to liquidation.
            ratios = [item.mgn_ratio for item in currencies if item.mgn_ratio is not None]
            mgn_ratio = min(ratios) if ratios else None
        return cls(
            total_eq=to_float(row.get("totalEq")),
            adj_eq=to_float(row.get("adjEq")),
            avail_eq=to_float(row.get("availEq")),
            upl=to_float(row.get("upl")),
            mgn_ratio=mgn_ratio,
            imr=to_float(row.get("imr")),
            mmr=to_float(row.get("mmr")),
            notional_usd=to_float(row.get("notionalUsd")),
            u_time=int(to_float(row.get("uTime")) or 0),
            currencies=tuple(currencies),
        )


@dataclass(frozen=True)
class Candle:
    ts: int  # open time, milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    confirmed: bool = True


@dataclass(frozen=True)
class Ticker:
    inst_id: str
    last: float
    bid: float | None = None
    ask: float | None = None
    open_24h: float | None = None
    high_24h: float | None = None
    low_24h: float | None = None
    ts: int = 0

    @property
    def change_24h_pct(self) -> float | None:
        if not self.open_24h:
            return None
        return (self.last - self.open_24h) / self.open_24h * 100.0


@dataclass(frozen=True)
class OpenInterest:
    """Open contracts of a perpetual or futures market, in coins and in USD."""

    coins: float | None = None
    usd: float | None = None


@dataclass
class AccountState:
    """One consistent read of the account, as used by the monitor and reports."""

    snapshot: AccountSnapshot
    positions: list[Position] = field(default_factory=list)


ACCOUNT_LEVELS = {"1": "现货模式", "2": "现货和合约模式", "3": "跨币种保证金模式", "4": "组合保证金模式"}


@dataclass(frozen=True)
class AccountConfig:
    """What the API key may do and how the account is set up."""

    pos_mode: str = ""  # "net_mode" (one position per instrument) or "long_short_mode" (hedge)
    permissions: tuple[str, ...] = ()  # of "read_only", "trade", "withdraw"
    ip: str = ""  # the addresses the key is bound to; empty when it is not bound
    acct_lv: str = ""

    @property
    def hedge_mode(self) -> bool:
        return self.pos_mode == "long_short_mode"

    @property
    def can_trade(self) -> bool:
        return "trade" in self.permissions

    @property
    def can_withdraw(self) -> bool:
        return "withdraw" in self.permissions

    @property
    def spot_only(self) -> bool:
        """Spot mode: OKX refuses every contract order of such an account (its error 51010)."""
        return self.acct_lv == "1"

    @property
    def level_name(self) -> str:
        return ACCOUNT_LEVELS.get(self.acct_lv, "")

    @classmethod
    def from_okx(cls, row: dict) -> "AccountConfig":
        permissions = tuple(item.strip() for item in str(row.get("perm", "")).split(",") if item.strip())
        return cls(
            pos_mode=str(row.get("posMode", "")),
            permissions=permissions,
            ip=str(row.get("ip", "") or ""),
            acct_lv=str(row.get("acctLv", "")),
        )


@dataclass(frozen=True)
class InstrumentSpec:
    """The trading rules of one instrument: contract size, size step and price step."""

    inst_id: str
    inst_type: str
    ct_val: Decimal  # coins per contract (before the multiplier)
    ct_mult: Decimal
    ct_val_ccy: str
    settle_ccy: str
    ct_type: str  # "linear" (settled in USDT/USDC) or "inverse"
    lot_sz: Decimal  # size step, in contracts
    min_sz: Decimal
    tick_sz: Decimal  # price step
    state: str = "live"

    @property
    def coins_per_contract(self) -> Decimal:
        return self.ct_val * self.ct_mult

    @classmethod
    def from_okx(cls, row: dict) -> "InstrumentSpec | None":
        numbers = {name: to_decimal(row.get(name)) for name in ("ctVal", "lotSz", "minSz", "tickSz")}
        if any(value is None or value <= 0 for value in numbers.values()):
            return None
        return cls(
            inst_id=str(row.get("instId", "")),
            inst_type=str(row.get("instType", "")),
            ct_val=numbers["ctVal"],
            ct_mult=to_decimal(row.get("ctMult")) or Decimal(1),
            ct_val_ccy=str(row.get("ctValCcy", "")),
            settle_ccy=str(row.get("settleCcy", "")),
            ct_type=str(row.get("ctType", "")),
            lot_sz=numbers["lotSz"],
            min_sz=numbers["minSz"],
            tick_sz=numbers["tickSz"],
            state=str(row.get("state", "live")),
        )


@dataclass(frozen=True)
class OrderInfo:
    """One order as OKX reports it."""

    ord_id: str
    inst_id: str
    side: str  # "buy" or "sell"
    pos_side: str = "net"
    ord_type: str = ""
    px: float | None = None
    sz: float = 0.0
    filled_sz: float = 0.0
    avg_px: float | None = None
    state: str = ""  # live | partially_filled | filled | canceled
    reduce_only: bool = False
    cl_ord_id: str = ""
    c_time: int = 0
    pnl: float | None = None  # what a closing order made or lost, before fees; in the settlement currency
    fee: float | None = None  # negative when a fee was charged

    @property
    def remaining(self) -> float:
        return max(0.0, self.sz - self.filled_sz)

    @classmethod
    def from_okx(cls, row: dict) -> "OrderInfo":
        return cls(
            ord_id=str(row.get("ordId", "")),
            inst_id=str(row.get("instId", "")),
            side=str(row.get("side", "")),
            pos_side=str(row.get("posSide") or "net"),
            ord_type=str(row.get("ordType", "")),
            px=to_float(row.get("px")),
            sz=to_float(row.get("sz")) or 0.0,
            filled_sz=to_float(row.get("accFillSz")) or 0.0,
            avg_px=to_float(row.get("avgPx")) or None,
            state=str(row.get("state", "")),
            reduce_only=str(row.get("reduceOnly", "")).lower() == "true",
            cl_ord_id=str(row.get("clOrdId", "")),
            c_time=int(to_float(row.get("cTime")) or 0),
            pnl=to_float(row.get("pnl")),
            fee=to_float(row.get("fee")),
        )
