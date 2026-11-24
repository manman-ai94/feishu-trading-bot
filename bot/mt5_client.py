"""The account on a MetaTrader 5 broker (EC Markets), read and traded through the MT5 terminal.

MetaQuotes' MetaTrader5 package talks to a terminal running on the same Windows machine, logged in
to the account; the bot never sees or keeps the password. The owner logs in once in the terminal
with "save password", and switches on "Algo Trading" before the bot may trade.

`Mt5Client` stands where the OKX client stood, so the rest of the bot does not change:

  * the instruments the broker carries (gold: XAU) are read from MT5. Their id in the bot is
    "<BASE>-USD-SWAP" (XAU-USD-SWAP); any other id with the same base, such as XAU-USDT-SWAP from
    before, means the same MT5 symbol. One contract (张) in the bot is one MT5 lot (手).
  * everything else (other coins, the crowd's positioning) still comes from OKX's public data.
  * the tickets of one symbol and direction are one position, as OKX shows a position, with one
    stop: algo id "p:<symbol>:<direction>". Moving it moves the stop-loss / take-profit of every
    ticket. Pending stop orders (buy stop / sell stop) are the trigger orders (计划委托).
  * closed positions and the stops that closed them come from the deal history.

`Mt5TradeClient` adds the calls that change something, and is created only when trading is on.
It refuses every order when the account logged in is not the kind TRADING names (live: a real
account; demo: a demo account), and while Algo Trading is off in the terminal.

MT5 reports times in the trade server's clock, not UTC. The offset is learnt from the ticks while
the market moves; until then the usual one is assumed (UTC+3 while New York keeps summer time,
UTC+2 otherwise).
"""
from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .models import (
    AccountConfig,
    AccountSnapshot,
    AccountState,
    Candle,
    CurrencyBalance,
    InstrumentSpec,
    OrderInfo,
    Position,
    StopOrder,
    Ticker,
)
from .mt5_worker import CALL_TIMEOUT, Mt5Process
from .okx_client import OkxError, OkxNetworkError

log = logging.getLogger(__name__)


class Mt5Error(OkxError):
    """MT5 refused or failed. Caught wherever an OKX error is; it reads as its own words, without OKX's prefix."""

    def __str__(self) -> str:
        return self.message


# The MetaTrader5 package's constants. Spelt out, so the bot reads the same numbers on any machine.
TIMEFRAMES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1H": 1 | 0x4000, "2H": 2 | 0x4000, "4H": 4 | 0x4000, "6H": 6 | 0x4000, "12H": 12 | 0x4000,
    "1D": 24 | 0x4000, "1Dutc": 24 | 0x4000, "1W": 1 | 0x8000, "1M": 1 | 0xC000,
}  # fmt: skip
ORDER_BUY, ORDER_SELL, BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP = 0, 1, 2, 3, 4, 5
BUY_STOP_LIMIT, SELL_STOP_LIMIT = 6, 7
POSITION_BUY = 0
DEAL_BUY, DEAL_SELL = 0, 1
DEAL_IN, DEAL_OUT, DEAL_INOUT, DEAL_OUT_BY = 0, 1, 2, 3
DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO = 4, 5, 6
ACTION_DEAL, ACTION_PENDING, ACTION_SLTP, ACTION_MODIFY, ACTION_REMOVE = 1, 5, 6, 7, 8
FILLING_FOK, FILLING_IOC, FILLING_RETURN = 0, 1, 2
TIME_GTC = 0
MARGIN_HEDGING = 2
TRADE_MODE_REAL = 2
STOPOUT_MONEY = 1
SYMBOL_TRADE_FULL = 4
ORDER_STATES = {0: "live", 1: "live", 2: "canceled", 3: "partially_filled", 4: "filled", 5: "canceled", 6: "canceled"}

RETCODE_DONE = {10008, 10009, 10010}  # placed, done, done in part
RETCODE_NO_CHANGES = 10025
RETCODE_UNKNOWN = {10012}  # timed out: it may or may not have reached the server
RETCODE_TEXT = {
    10004: "报价变了（requote），请重新发",
    10006: "交易服务器拒绝了这张单",
    10013: "请求的内容不对",
    10014: "数量不对（不在最小手数和手数步长之内）",
    10015: "价格不对",
    10016: "止损或止盈不对：离现价太近，或者在错误的一边",
    10017: "这个品种现在不能交易",
    10018: "市场休市",
    10019: "保证金不够",
    10020: "价格变了，请重新发",
    10021: "现在没有报价",
    10024: "请求太频繁",
    10026: "交易服务器不允许这个账户自动交易",
    10027: "MT5 终端的“算法交易”（Algo Trading）按钮没打开",
    10030: "这个品种不支持这种成交方式",
    10031: "MT5 终端和交易服务器断开了",
    10033: "挂单数量到了上限",
    10034: "这个品种的持仓和挂单到了上限",
    10040: "持仓数量到了上限",
}
MAGIC = 20261123  # marks the bot's orders in MT5 (the "magic number")
BOT_PREFIX = "ftb"  # the bot's client order ids start with it; MT5 keeps them in the comment
HISTORY_DAYS = 90
MERGE_SECONDS = 2  # closing deals of one position this close together are one close
INIT_RETRY_SECONDS = 60.0
# Where MetaQuotes' installer puts the terminal. Used when MT5_PATH is empty: the package's own search looks in the
# registry of the user who installed it, and the bot may run as another user.
DEFAULT_TERMINALS = (r"C:\Program Files\MetaTrader 5\terminal64.exe",)
STOP_PREFIX = "p:"
OFFSET_TOLERANCE_SECONDS = 120
# Calls that return a list: None with no error means there is nothing to list.
LIST_CALLS = {"positions_get", "orders_get", "history_deals_get", "history_orders_get", "symbols_get"}


def mt5_id(base: str) -> str:
    """The bot's id of an instrument the broker carries: XAU -> XAU-USD-SWAP."""
    return f"{base.upper()}-USD-SWAP"


def _number(value) -> str:
    """A price or size as OKX writes it: no exponent, no trailing zeros."""
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text or "0"


def _dec(value: float) -> Decimal:
    return Decimal(repr(float(value)))


def _new_york_summer(now: float) -> bool:
    return bool(datetime.fromtimestamp(now, ZoneInfo("America/New_York")).dst())


class Mt5Link:
    """The connection to the terminal. Every call goes through one lock: the package is not made for
    several threads at once."""

    def __init__(self, path: str = "", module=None, clock=time.time, connect_ms: int = 20_000, terminals=DEFAULT_TERMINALS):
        self._path = path or next((where for where in terminals if os.path.isfile(where)), "")
        self._connect_ms = connect_ms
        self._module = module
        self._clock = clock
        self._lock = threading.RLock()
        self._ready = False
        self._failed_at: float | None = None
        self._failure = ""
        self._offset: float | None = None  # server clock minus UTC, seconds, once learnt
        self._last_ticks: dict[str, int] = {}

    @property
    def mt5(self):
        """The MetaTrader5 package, run in a child process (see mt5_worker): a terminal that does not
        answer holds the package for a minute, and must not hold the rest of the bot with it."""
        if self._module is None:
            if importlib.util.find_spec("MetaTrader5") is None:  # Windows only; installed on the server
                raise Mt5Error("mt5_missing", "服务器上还没装 MetaTrader5 这个 Python 包")
            self._module = Mt5Process(timeout=CALL_TIMEOUT, connect_timeout=self._connect_ms / 1000 + CALL_TIMEOUT)
        return self._module

    def _connect(self) -> None:
        now = self._clock()
        if self._failed_at is not None and now - self._failed_at < INIT_RETRY_SECONDS:
            raise Mt5Error("mt5_down", self._failure)
        kwargs = {"path": self._path} if self._path else {}
        if self.mt5.initialize(timeout=self._connect_ms, **kwargs):
            self._ready, self._failed_at = True, None
            log.info("mt5: connected to the terminal")
            return
        code, message = self._error()
        # From when the attempt ended: an attempt on a terminal that does not answer can itself take a minute.
        self._failed_at = self._clock()
        self._failure = f"连不上 MT5 终端（{code} {message}）。请确认服务器上的 MT5 开着并且登录了账户"
        raise Mt5Error("mt5_down", self._failure)

    def _error(self) -> tuple[int, str]:
        try:
            code, message = self.mt5.last_error()
            return int(code), str(message)
        except Exception:  # noqa: BLE001  only for the words of an error
            return 0, "原因不明"

    def call(self, name: str, *args, **kwargs):
        """One call of the package; connects first when needed. A lost terminal is reconnected next time."""
        with self._lock:
            if not self._ready:
                self._connect()
            result = getattr(self.mt5, name)(*args, **kwargs)
            if result is None and name not in ("order_send", "order_check", "symbol_info", "symbol_info_tick"):
                code, message = self._error()
                if name in LIST_CALLS and code in (0, 1):
                    return ()
                if code <= -10000:  # IPC errors: the terminal is gone or stuck; wait before trying it again
                    self._ready = False
                    self._failed_at = self._clock()
                    self._failure = f"MT5 终端没有回应（{code} {message}）。请确认服务器上的 MT5 开着并且登录了账户"
                    if name == "account_info":
                        raise Mt5Error("mt5_down", self._failure)
                if name == "account_info":
                    raise Mt5Error(
                        "mt5_login", "MT5 终端开着，但还没登录交易账户。请远程桌面进服务器，在 MT5 里登录并勾上“保存密码”"
                    )
                raise Mt5Error("mt5_error", f"MT5 没有返回数据（{name}：{code} {message}）")
            return result

    def last_error(self) -> tuple[int, str]:
        with self._lock:
            return self._error()

    # ------------------------------------------------------------------ time

    def offset(self) -> float:
        """The trade server's clock minus UTC, in seconds."""
        if self._offset is not None:
            return self._offset
        return 3 * 3600.0 if _new_york_summer(self._clock()) else 2 * 3600.0

    def learn_offset(self, symbol: str, tick) -> None:
        """A tick that is new since the last look was made just now: its time gives the server's offset."""
        stamp = int(getattr(tick, "time_msc", 0) or 0)
        seen = self._last_ticks.get(symbol)
        self._last_ticks[symbol] = stamp
        if not stamp or seen is None or stamp == seen:
            return
        diff = stamp / 1000.0 - self._clock()
        hours = round(diff / 3600.0)
        if abs(diff - hours * 3600.0) <= OFFSET_TOLERANCE_SECONDS and -12 <= hours <= 14:
            if self._offset != hours * 3600.0:
                log.info("mt5: the trade server's clock is UTC%+d", hours)
            self._offset = hours * 3600.0

    def ms(self, server_seconds: float) -> int:
        """A server time in seconds -> UTC milliseconds."""
        return int((float(server_seconds) - self.offset()) * 1000)

    def ms_from_msc(self, server_ms: float) -> int:
        return int(float(server_ms) - self.offset() * 1000)

    def server_now(self) -> int:
        return int(self._clock() + self.offset())


class Mt5Client:
    """Reads the MT5 account and the broker's prices; anything else from OKX's public data (`market`)."""

    def __init__(self, market, link: Mt5Link, symbols: dict[str, str], clock=time.time):
        """`symbols`: base -> the broker's symbol, e.g. {"XAU": "XAUUSD"}."""
        self._market = market
        self._link = link
        self._clock = clock
        self._wanted = {base.upper(): symbol for base, symbol in symbols.items()}
        self._found: dict[str, str] = {}  # base -> the symbol as the broker names it (it may carry a suffix)
        self._bases: dict[str, str] = {}  # broker symbol -> base

    @property
    def market(self):
        """OKX's public data."""
        return self._market

    @property
    def link(self) -> Mt5Link:
        return self._link

    # -------------------------------------------------------------- symbols

    def carries(self, inst_id: str) -> bool:
        """Whether this instrument is read from MT5."""
        return "-" not in inst_id or inst_id.split("-")[0].upper() in self._wanted

    def symbol(self, inst_id: str) -> str:
        """The broker's symbol of an instrument the bot names."""
        if "-" not in inst_id:
            return inst_id
        base = inst_id.split("-")[0].upper()
        if base in self._found:
            return self._found[base]
        wanted = self._wanted[base]
        info = self._link.call("symbol_info", wanted)
        name = wanted
        if info is None:
            # Brokers add a suffix to their names (XAUUSD.r, XAUUSDm): the first tradable one that starts the same.
            candidates = self._link.call("symbols_get", f"{wanted}*") or ()
            names = [str(item.name) for item in candidates if str(item.name).upper().startswith(wanted.upper())]
            if not names:
                raise Mt5Error("no_data", f"MT5 里找不到 {wanted} 这个品种")
            name = sorted(names, key=len)[0]
            info = self._link.call("symbol_info", name)
        if info is not None and not getattr(info, "visible", True):
            self._link.call("symbol_select", name, True)
        self._found[base] = name
        self._bases[name] = base
        return name

    def inst_id_of(self, symbol: str) -> str:
        """The bot's id of a broker symbol: XAUUSD -> XAU-USD-SWAP. A symbol the bot was not told of keeps its name."""
        if symbol in self._bases:
            return mt5_id(self._bases[symbol])
        for base, wanted in self._wanted.items():
            if symbol.upper().startswith(wanted.upper()):
                self._found.setdefault(base, symbol)
                self._bases[symbol] = base
                return mt5_id(base)
        return symbol

    def _info(self, inst_id: str):
        info = self._link.call("symbol_info", self.symbol(inst_id))
        if info is None:
            raise Mt5Error("no_data", f"MT5 没有返回 {inst_id} 的品种信息")
        return info

    def _tick(self, symbol: str):
        tick = self._link.call("symbol_info_tick", symbol)
        if tick is None or not (getattr(tick, "bid", 0) or getattr(tick, "ask", 0)):
            raise Mt5Error("no_data", f"MT5 没有 {symbol} 的报价")
        self._link.learn_offset(symbol, tick)
        return tick

    # --------------------------------------------------------------- market

    @property
    def demo(self) -> bool:
        try:
            return int(self._account().trade_mode) != TRADE_MODE_REAL
        except OkxError:
            return False

    def server_time_ms(self) -> int:
        return self._market.server_time_ms()

    def sync_clock(self) -> float:
        return self._market.sync_clock()

    def ticker(self, inst_id: str) -> Ticker:
        if not self.carries(inst_id):
            return self._market.ticker(inst_id)
        symbol = self.symbol(inst_id)
        tick = self._tick(symbol)
        bid = float(tick.bid) or None
        ask = float(tick.ask) or None
        last = bid or ask  # the price MT5 draws its charts with
        day = self._rates(symbol, "1H", 25)
        open_24h = float(day[0]["open"]) if len(day) else None
        high = max((float(row["high"]) for row in day), default=None)
        low = min((float(row["low"]) for row in day), default=None)
        return Ticker(inst_id, last, bid, ask, open_24h, high, low, self._link.ms_from_msc(tick.time_msc) if tick.time_msc else 0)

    def _rates(self, symbol: str, bar: str, limit: int):
        frame = TIMEFRAMES.get(bar)
        if frame is None:
            raise Mt5Error("bad_bar", f"MT5 没有 {bar} 这种 K 线")
        rates = self._link.call("copy_rates_from_pos", symbol, frame, 0, max(1, int(limit)))
        return rates if rates is not None else []

    def candles(self, inst_id: str, bar: str = "1H", limit: int = 200) -> list[Candle]:
        """Oldest first. The last one is still forming."""
        if not self.carries(inst_id):
            return self._market.candles(inst_id, bar, limit)
        rates = self._rates(self.symbol(inst_id), bar, limit)
        count = len(rates)
        return [
            Candle(
                self._link.ms(row["time"]), float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), float(row["tick_volume"]), index < count - 1,
            )
            for index, row in enumerate(rates)
        ]  # fmt: skip

    def funding_rate(self, inst_id: str) -> float | None:
        return None if self.carries(inst_id) else self._market.funding_rate(inst_id)

    def funding(self, inst_id: str) -> tuple[float, float] | None:
        return None if self.carries(inst_id) else self._market.funding(inst_id)

    def open_interest(self, inst_id: str):
        return None if self.carries(inst_id) else self._market.open_interest(inst_id)

    def top_trader_ratio(self, inst_id: str, by: str = "account", period: str = "1H") -> list[tuple[int, float]]:
        """OKX's big traders in the same coin: the broker has no such figure."""
        if self.carries(inst_id):
            inst_id = f"{inst_id.split('-')[0].upper()}-USDT-SWAP"
        return self._market.top_trader_ratio(inst_id, by, period)

    def instruments(self, inst_type: str) -> list[dict]:
        """OKX's list, with the broker's instruments in place of OKX's of the same coin."""
        rows = [row for row in self._market.instruments(inst_type) if str(row.get("instId", "")).split("-")[0].upper() not in self._wanted]
        if inst_type == "SWAP":
            rows.extend({"instId": mt5_id(base), "instType": "SWAP", "state": "live"} for base in self._wanted)
        return rows

    def instrument_spec(self, inst_id: str) -> InstrumentSpec:
        if not self.carries(inst_id):
            return self._market.instrument_spec(inst_id)
        info = self._info(inst_id)
        base = inst_id.split("-")[0].upper() if "-" in inst_id else str(getattr(info, "currency_base", "") or inst_id)
        tick = float(getattr(info, "trade_tick_size", 0) or 0) or float(info.point)
        return InstrumentSpec(
            inst_id=inst_id,
            inst_type="SWAP",
            ct_val=_dec(info.trade_contract_size),
            ct_mult=Decimal(1),
            ct_val_ccy=base,
            settle_ccy=self._currency(),
            ct_type="linear",
            lot_sz=_dec(info.volume_step),
            min_sz=_dec(info.volume_min),
            tick_sz=_dec(tick),
            state="live" if int(getattr(info, "trade_mode", SYMBOL_TRADE_FULL)) == SYMBOL_TRADE_FULL else "suspend",
            unit="手",
        )

    # -------------------------------------------------------------- account

    def _account(self):
        return self._link.call("account_info")

    def _currency(self) -> str:
        try:
            return str(self._account().currency or "USD")
        except OkxError:
            return "USD"

    def account_currency(self) -> str:
        """The currency the account is kept in (USD)."""
        return self._currency()

    def _hedging(self, account=None) -> bool:
        account = account or self._account()
        return int(getattr(account, "margin_mode", MARGIN_HEDGING)) == MARGIN_HEDGING

    def _tickets(self, symbol: str | None = None) -> list:
        rows = self._link.call("positions_get", symbol=symbol) if symbol else self._link.call("positions_get")
        return list(rows or ())

    def balance(self) -> AccountSnapshot:
        return self._snapshot(self._account(), [])

    def _snapshot(self, account, positions: list[Position]) -> AccountSnapshot:
        currency = str(account.currency or "USD")
        margin = float(account.margin or 0)
        ratio = None
        if margin > 0 and account.margin_so_so:
            if int(getattr(account, "margin_so_mode", 0)) == STOPOUT_MONEY:
                ratio = float(account.equity) / float(account.margin_so_so)
            elif account.margin_level:
                ratio = float(account.margin_level) / float(account.margin_so_so)
        maintenance = None
        if margin > 0 and account.margin_so_so and int(getattr(account, "margin_so_mode", 0)) != STOPOUT_MONEY:
            maintenance = margin * float(account.margin_so_so) / 100
        notional = sum(p.notional_usd or 0.0 for p in positions) or None
        equity = float(account.equity)
        return AccountSnapshot(
            total_eq=equity,
            adj_eq=equity,
            avail_eq=float(account.margin_free),
            upl=float(account.profit),
            mgn_ratio=ratio,
            imr=margin,
            mmr=maintenance,
            notional_usd=notional,
            u_time=int(self._clock() * 1000),
            currencies=(
                CurrencyBalance(
                    ccy=currency, eq=equity, eq_usd=equity if currency in ("USD", "USDT", "USDC") else None,
                    avail_eq=float(account.margin_free), upl=float(account.profit), mgn_ratio=ratio,
                    avail_bal=float(account.margin_free),
                ),
            ),
        )  # fmt: skip

    def positions(self, inst_type: str | None = None) -> list[Position]:
        return self._positions(self._account(), self._tickets())

    def _positions(self, account, tickets: list) -> list[Position]:
        hedging = self._hedging(account)
        groups: dict[tuple[str, str], list] = {}
        for row in tickets:
            direction = "long" if int(row.type) == POSITION_BUY else "short"
            groups.setdefault((str(row.symbol), direction), []).append(row)
        net: dict[str, float] = {}
        for (symbol, direction), rows in groups.items():
            volume = sum(float(row.volume) for row in rows)
            net[symbol] = net.get(symbol, 0.0) + (volume if direction == "long" else -volume)
        positions = []
        currency = str(account.currency or "USD")
        for (symbol, direction), rows in sorted(groups.items()):
            info = self._link.call("symbol_info", symbol)
            size = float(getattr(info, "trade_contract_size", 0) or 0) if info is not None else 0.0
            volume = sum(float(row.volume) for row in rows)
            avg = sum(float(row.volume) * float(row.price_open) for row in rows) / volume if volume else None
            mark = float(rows[0].price_current) or None
            inst_id = self.inst_id_of(symbol)
            base = inst_id.split("-")[0] if "-" in inst_id else str(getattr(info, "currency_base", "") or symbol)
            positions.append(
                Position(
                    inst_id=inst_id,
                    inst_type="SWAP",
                    pos_side=direction if hedging else "net",
                    mgn_mode="cross",
                    qty=volume,
                    direction=direction,
                    avg_px=avg,
                    mark_px=mark,
                    upl=sum(float(row.profit) + float(row.swap or 0) for row in rows),
                    lever=float(account.leverage) if account.leverage else None,
                    liq_px=self._liq_px(account, mark, net[symbol] * size, info, currency),
                    notional_usd=volume * size * mark if mark and size and currency.startswith("USD") else None,
                    pos_id=f"{symbol}:{direction}",
                    u_time=max(self._link.ms_from_msc(row.time_update_msc) for row in rows),
                    c_time=min(self._link.ms_from_msc(row.time_msc) for row in rows),
                    stops=self._stops(symbol, direction, rows),
                    ccy=currency,
                    coins=volume * size if size else None,
                    coin_ccy=base if size else "",
                )
            )
        return positions

    @staticmethod
    def _stops(symbol: str, direction: str, rows: list) -> tuple[StopOrder, ...]:
        """One stop for all the tickets. A ticket without a stop-loss leaves the position without one; tickets
        whose stop-losses differ show the one furthest away (the most the position can lose)."""
        stops = [float(row.sl or 0) for row in rows]
        takes = [float(row.tp or 0) for row in rows]
        sl = None
        if all(stops):
            sl = min(stops) if direction == "long" else max(stops)
        tp = takes[0] if takes[0] and all(take == takes[0] for take in takes) else None
        if sl is None and tp is None:
            return ()
        return (StopOrder(f"{STOP_PREFIX}{symbol}:{direction}", sl, tp),)

    @staticmethod
    def _liq_px(account, price: float | None, units: float, info, currency: str) -> float | None:
        """Where the broker would start closing the account (stop out), with everything else as it is now.
        `units`: the symbol's net position in ounces (or coins), + for long."""
        if not price or not units or not account.margin or not account.margin_so_so or info is None:
            return None
        if str(getattr(info, "currency_profit", currency) or currency) != currency:
            return None
        if int(getattr(account, "margin_so_mode", 0)) == STOPOUT_MONEY:
            floor = float(account.margin_so_so)
        else:
            floor = float(account.margin) * float(account.margin_so_so) / 100
        liq = price - (float(account.equity) - floor) / units
        return liq if liq > 0 else None

    def pending_stops(self) -> list[dict]:
        return self.pending_algos("conditional,oco")

    def account_state(self) -> AccountState:
        account = self._account()
        positions = self._positions(account, self._tickets())
        return AccountState(snapshot=self._snapshot(account, positions), positions=positions, stops_known=True)

    def account_config(self) -> AccountConfig:
        account = self._account()
        terminal = self._link.call("terminal_info")
        trade = bool(account.trade_allowed) and bool(getattr(account, "trade_expert", True)) and bool(terminal.trade_allowed)
        return AccountConfig(
            pos_mode="long_short_mode" if self._hedging(account) else "net_mode",
            permissions=("read_only", "trade") if trade else ("read_only",),
        )

    def leverage(self, inst_id: str, mgn_mode: str) -> float | None:
        if not self.carries(inst_id):
            return None
        return float(self._account().leverage or 0) or None

    # --------------------------------------------------------------- orders

    def _pending(self) -> list:
        return [row for row in (self._link.call("orders_get") or ()) if int(row.type) in (BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP)]

    def _order_side(self, row) -> str:
        return "buy" if int(row.type) in (ORDER_BUY, BUY_LIMIT, BUY_STOP, BUY_STOP_LIMIT) else "sell"

    def _order_info(self, row, hedging: bool, deals: list | None = None) -> OrderInfo:
        side = self._order_side(row)
        kind = int(row.type)
        filled = float(row.volume_initial) - float(row.volume_current)
        avg = pnl = fee = None
        if deals:
            volume = sum(float(deal.volume) for deal in deals)
            filled = volume
            avg = sum(float(deal.volume) * float(deal.price) for deal in deals) / volume if volume else None
            pnl = sum(float(deal.profit) for deal in deals)
            fee = sum(float(deal.commission or 0) + float(getattr(deal, "fee", 0) or 0) for deal in deals)
        return OrderInfo(
            ord_id=str(row.ticket),
            inst_id=self.inst_id_of(str(row.symbol)),
            side=side,
            pos_side=("long" if side == "buy" else "short") if hedging else "net",
            ord_type="market" if kind in (ORDER_BUY, ORDER_SELL) else ("trigger" if kind in (BUY_STOP, SELL_STOP) else "limit"),
            px=float(row.price_open) or None,
            sz=float(row.volume_initial),
            filled_sz=filled,
            avg_px=avg,
            state=ORDER_STATES.get(int(getattr(row, "state", 1)), ""),
            cl_ord_id=str(row.comment or ""),
            c_time=self._link.ms_from_msc(row.time_setup_msc) if getattr(row, "time_setup_msc", 0) else 0,
            pnl=pnl or None,
            fee=fee,
            sl_trigger_px=float(row.sl) or None,
            tp_trigger_px=float(row.tp) or None,
        )

    def pending_orders(self, inst_id: str | None = None) -> list[OrderInfo]:
        """Limit orders waiting to be filled."""
        hedging = self._hedging()
        rows = [row for row in self._pending() if int(row.type) in (BUY_LIMIT, SELL_LIMIT)]
        orders = [self._order_info(row, hedging) for row in rows]
        return [order for order in orders if not inst_id or order.inst_id == inst_id]

    def _raw(self, row, hedging: bool) -> dict:
        side = self._order_side(row)
        comment = str(row.comment or "")
        attached = {}
        if float(row.sl):
            attached["slTriggerPx"] = _number(row.sl)
        if float(row.tp):
            attached["tpTriggerPx"] = _number(row.tp)
        return {
            "instId": self.inst_id_of(str(row.symbol)),
            "instType": "SWAP",
            "side": side,
            "posSide": ("long" if side == "buy" else "short") if hedging else "net",
            "sz": _number(row.volume_current),
            "tag": "ftbot" if comment.startswith(BOT_PREFIX) else "",
            "cTime": str(self._link.ms_from_msc(row.time_setup_msc)) if getattr(row, "time_setup_msc", 0) else "",
            "attachAlgoOrds": [attached] if attached else [],
            "reduceOnly": "false",
        }

    def pending_orders_raw(self) -> list[dict]:
        hedging = self._hedging()
        rows = []
        for row in self._pending():
            if int(row.type) not in (BUY_LIMIT, SELL_LIMIT):
                continue
            raw = self._raw(row, hedging)
            raw.update(
                ordId=str(row.ticket), clOrdId=str(row.comment or ""), ordType="limit", px=_number(row.price_open),
                accFillSz=_number(float(row.volume_initial) - float(row.volume_current)),
            )
            rows.append(raw)
        return rows

    def pending_algos(self, ord_type: str) -> list[dict]:
        """Stop orders (buy stop / sell stop) as trigger orders; the stops on positions as take-profit / stop-loss."""
        kinds = {kind.strip() for kind in ord_type.split(",")}
        rows: list[dict] = []
        hedging = self._hedging()
        if "trigger" in kinds:
            for row in self._pending():
                if int(row.type) not in (BUY_STOP, SELL_STOP):
                    continue
                raw = self._raw(row, hedging)
                raw.update(
                    algoId=str(row.ticket), algoClOrdId=str(row.comment or ""), ordType="trigger",
                    triggerPx=_number(row.price_open), orderPx="-1", triggerPxType="last",
                )
                rows.append(raw)
        if kinds & {"conditional", "oco"}:
            for position in self.positions():
                for stop in position.stops:
                    both = bool(stop.sl_trigger_px and stop.tp_trigger_px)
                    if ("oco" if both else "conditional") not in kinds:
                        continue
                    row = {
                        "algoId": stop.algo_id, "instId": position.inst_id, "instType": "SWAP",
                        "side": "sell" if position.direction == "long" else "buy", "posSide": position.pos_side,
                        "ordType": "oco" if both else "conditional", "closeFraction": "1", "tag": "",
                    }  # fmt: skip
                    if stop.sl_trigger_px:
                        row.update(slTriggerPx=_number(stop.sl_trigger_px), slOrdPx="-1", slTriggerPxType="last")
                    if stop.tp_trigger_px:
                        row.update(tpTriggerPx=_number(stop.tp_trigger_px), tpOrdPx="-1", tpTriggerPxType="last")
                    rows.append(row)
        return rows

    def _history_range(self, days: float) -> tuple[int, int]:
        now = self._link.server_now()
        return now - int(days * 86400), now + 86400

    def _deals(self, days: float = HISTORY_DAYS) -> list:
        start, end = self._history_range(days)
        rows = self._link.call("history_deals_get", start, end) or ()
        return sorted((row for row in rows if int(row.type) in (DEAL_BUY, DEAL_SELL)), key=lambda row: (int(row.time_msc), int(row.ticket)))

    def order(self, inst_id: str, ord_id: str = "", cl_ord_id: str = "") -> OrderInfo:
        """One order, by its ticket or by the id the bot gave it (kept in the comment). An order the bot sent as
        several (a close of several tickets) is read as one."""
        hedging = self._hedging()
        pending = self._pending()
        if ord_id:
            for row in pending:
                if str(row.ticket) == str(ord_id):
                    return self._order_info(row, hedging)
            found = list(self._link.call("history_orders_get", ticket=int(ord_id)) or ())
            comment = str(found[0].comment or "") if found else ""
            if comment.startswith(BOT_PREFIX):
                cl_ord_id = comment
        else:
            for row in pending:
                if str(row.comment or "") == cl_ord_id:
                    return self._order_info(row, hedging)
            found = []
        if cl_ord_id:
            start, end = self._history_range(2)
            found = [row for row in (self._link.call("history_orders_get", start, end) or ()) if str(row.comment or "") == cl_ord_id]
        if not found:
            raise Mt5Error("51603", "MT5 里查不到这张单")
        tickets = {int(row.ticket) for row in found}
        deals = [deal for deal in self._deals(2) if int(deal.order) in tickets]
        first = min(found, key=lambda row: int(row.ticket))
        info = self._order_info(first, hedging, deals)
        states = {ORDER_STATES.get(int(row.state), "") for row in found}
        state = "filled" if states == {"filled"} else ("partially_filled" if "filled" in states or "partially_filled" in states else info.state)
        return replace(info, sz=sum(float(row.volume_initial) for row in found), state=state)

    # -------------------------------------------------------------- history

    def _closes(self) -> tuple[list[dict], list[dict]]:
        """Closed positions as OKX's position history, newest first; and the stops and take-profits that closed them."""
        account = self._account()
        hedging = self._hedging(account)
        currency = str(account.currency or "USD")
        open_: dict[tuple[str, str], dict] = {}
        rows: list[dict] = []
        triggers: list[dict] = []
        pending_close: dict | None = None

        def flush() -> None:
            nonlocal pending_close
            if pending_close is None:
                return
            c = pending_close
            pending_close = None
            campaign = open_.get(c["key"])
            left = campaign["volume"] if campaign else 0.0
            stop_out = c["reason"] == DEAL_REASON_SO
            full = left <= 1e-9
            kind = ("3" if full else "4") if stop_out else ("2" if full else "1")
            fee = c["fee"] + (campaign["fee"] if campaign else 0.0)
            if campaign:
                campaign["fee"] = 0.0
            realized = c["profit"] + fee + c["swap"]
            symbol, direction = c["key"]
            rows.append({
                "instId": self.inst_id_of(symbol), "instType": "SWAP", "mgnMode": "cross",
                "posSide": direction if hedging else "net", "direction": direction, "type": kind,
                "openAvgPx": _number(c["open_avg"]), "closeAvgPx": _number(c["value"] / c["volume"]),
                "closeTotalPos": _number(c["volume"]), "pnl": _number(c["profit"]), "fee": _number(fee),
                "fundingFee": _number(c["swap"]), "liqPenalty": "0", "realizedPnl": _number(realized), "ccy": currency,
                "cTime": str(c["started"]), "uTime": str(c["at"]), "posId": f"{symbol}:{direction}:{c['started']}",
            })  # fmt: skip
            if c["reason"] in (DEAL_REASON_SL, DEAL_REASON_TP):
                side = "sl" if c["reason"] == DEAL_REASON_SL else "tp"
                price = _number(c["value"] / c["volume"])
                triggers.append({
                    "instId": self.inst_id_of(symbol), "actualSide": side, "triggerTime": str(c["at"]), "uTime": str(c["at"]),
                    "state": "effective", "ordType": "conditional", f"{side}TriggerPx": price,
                })  # fmt: skip
            if campaign and full:
                del open_[c["key"]]

        def opening(symbol: str, direction: str, deal, volume: float, at: int) -> None:
            campaign = open_.setdefault((symbol, direction), {"volume": 0.0, "value": 0.0, "fee": 0.0, "started": at})
            campaign["volume"] += volume
            campaign["value"] += volume * float(deal.price)
            campaign["fee"] += float(deal.commission or 0) + float(getattr(deal, "fee", 0) or 0)

        def closing(symbol: str, direction: str, deal, volume: float, at: int, share: float) -> None:
            nonlocal pending_close
            key = (symbol, direction)
            campaign = open_.get(key)
            if pending_close is not None and (pending_close["key"] != key or at - pending_close["at"] > MERGE_SECONDS * 1000):
                flush()
                campaign = open_.get(key)
            open_avg = campaign["value"] / campaign["volume"] if campaign and campaign["volume"] else float(deal.price)
            if campaign:
                taken = min(volume, campaign["volume"])
                campaign["value"] -= taken * open_avg
                campaign["volume"] -= taken
            if pending_close is None:
                pending_close = {
                    "key": key, "at": at, "volume": 0.0, "value": 0.0, "profit": 0.0, "fee": 0.0, "swap": 0.0,
                    "reason": int(getattr(deal, "reason", 0)), "open_avg": open_avg,
                    "started": campaign["started"] if campaign else at,
                }  # fmt: skip
            pending_close["volume"] += volume
            pending_close["value"] += volume * float(deal.price)
            pending_close["profit"] += float(deal.profit) * share
            pending_close["fee"] += (float(deal.commission or 0) + float(getattr(deal, "fee", 0) or 0)) * share
            pending_close["swap"] += float(deal.swap or 0) * share
            pending_close["at"] = at
            if int(getattr(deal, "reason", 0)) in (DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO):
                pending_close["reason"] = int(deal.reason)

        for deal in self._deals():
            symbol = str(deal.symbol)
            at = self._link.ms_from_msc(deal.time_msc)
            buy = int(deal.type) == DEAL_BUY
            entry = int(deal.entry)
            volume = float(deal.volume)
            if entry == DEAL_IN:
                flush()  # only closes are merged: an opening ends the close before it
                opening(symbol, "long" if buy else "short", deal, volume, at)
            elif entry in (DEAL_OUT, DEAL_OUT_BY):
                closing(symbol, "short" if buy else "long", deal, volume, at, 1.0)
            elif entry == DEAL_INOUT:  # netting: closes what was open and opens the rest the other way
                held = open_.get((symbol, "short" if buy else "long"), {}).get("volume", 0.0)
                closed = min(held, volume)
                if closed:
                    closing(symbol, "short" if buy else "long", deal, closed, at, 1.0)
                    flush()
                if volume - closed > 1e-9:
                    opening(symbol, "long" if buy else "short", deal, volume - closed, at)
        flush()
        rows.reverse()
        triggers.reverse()
        return rows, triggers

    def position_history(self) -> list[dict]:
        return self._closes()[0]

    def algo_history(self, ord_type: str) -> list[dict]:
        return self._closes()[1] if ord_type == "conditional" else []


class Mt5TradeClient(Mt5Client):
    """The calls that change something on the MT5 account."""

    trigger_px_type = "last"  # not used: MT5's stops fire on the bid (longs) and the ask (shorts)

    def __init__(self, market, link: Mt5Link, symbols: dict[str, str], real: bool, slippage_pct: float = 0.02, clock=time.time):
        """`real`: orders go only to a real account (TRADING=live); otherwise only to a demo account."""
        super().__init__(market, link, symbols, clock)
        self._real = real
        self._slippage_pct = slippage_pct

    def _check(self) -> None:
        account = self._account()
        real = int(account.trade_mode) == TRADE_MODE_REAL
        if self._real and not real:
            raise Mt5Error("mt5_account", "TRADING=live，但 MT5 现在登录的是模拟账户，不下单")
        if not self._real and real:
            raise Mt5Error("mt5_account", "TRADING=demo，但 MT5 现在登录的是实盘账户，不下单")
        terminal = self._link.call("terminal_info")
        if not terminal.trade_allowed:
            raise Mt5Error("10027", RETCODE_TEXT[10027] + "，机器人下不了单。请在服务器的 MT5 工具栏上点开它")
        if not account.trade_allowed or not getattr(account, "trade_expert", True):
            raise Mt5Error("10026", RETCODE_TEXT[10026])

    def _send(self, request: dict, what: str):
        self._check()
        request = {"magic": MAGIC, **request}
        result = self._link.call("order_send", request)
        if result is None:
            code, message = self._link.last_error()
            raise OkxNetworkError(f"MT5 没有回应（{code} {message}），不确定{what}有没有生效")
        retcode = int(result.retcode)
        if retcode in RETCODE_DONE or (retcode == RETCODE_NO_CHANGES and request.get("action") == ACTION_SLTP):
            return result
        if retcode in RETCODE_UNKNOWN:
            raise OkxNetworkError(f"MT5 等交易服务器回应超时，不确定{what}有没有生效")
        words = RETCODE_TEXT.get(retcode) or str(getattr(result, "comment", "") or "交易服务器拒绝了")
        raise Mt5Error(str(retcode), words)

    def _deviation(self, info, price: float) -> int:
        point = float(info.point) or 0.01
        return max(10, int(price * self._slippage_pct / 100 / point))

    @staticmethod
    def _filling(info) -> int:
        mode = int(getattr(info, "filling_mode", 0) or 0)
        if mode & 1:
            return FILLING_FOK
        if mode & 2:
            return FILLING_IOC
        return FILLING_RETURN

    @staticmethod
    def _price(info, value) -> float:
        return round(float(value), int(info.digits))

    def _volume(self, info, sz) -> float:
        step = _dec(info.volume_step)
        lots = (Decimal(str(sz)) / step).to_integral_value(rounding="ROUND_DOWN") * step
        if lots < _dec(info.volume_min):
            raise Mt5Error("10014", f"数量 {sz} 手小于最小手数 {_number(info.volume_min)}")
        return float(lots)

    def _market_order(self, symbol: str, info, buy: bool, volume: float, comment: str, ticket: int | None = None, sl=None, tp=None):
        tick = self._tick(symbol)
        price = float(tick.ask if buy else tick.bid)
        request = {
            "action": ACTION_DEAL, "symbol": symbol, "volume": volume, "type": ORDER_BUY if buy else ORDER_SELL,
            "price": price, "deviation": self._deviation(info, price), "type_time": TIME_GTC,
            "type_filling": self._filling(info), "comment": comment[:31],
        }  # fmt: skip
        if ticket is not None:
            request["position"] = ticket
        if sl:
            request["sl"] = self._price(info, sl)
        if tp:
            request["tp"] = self._price(info, tp)
        return self._send(request, "这张单")

    def _direction_tickets(self, symbol: str, direction: str | None) -> list:
        tickets = self._tickets(symbol)
        if direction in ("long", "short"):
            tickets = [row for row in tickets if (int(row.type) == POSITION_BUY) == (direction == "long")]
        return sorted(tickets, key=lambda row: (int(row.time_msc), int(row.ticket)))

    def place_order(
        self, *, inst_id: str, td_mode: str, side: str, ord_type: str, sz: str, cl_ord_id: str, px: str | None = None,
        pos_side: str | None = None, reduce_only: bool = False, sl_trigger_px: str | None = None, tp_trigger_px: str | None = None,
    ) -> str:  # fmt: skip
        """Market or limit. A reduce-only market order closes that much of the opposite tickets, oldest first."""
        symbol = self.symbol(inst_id)
        info = self._info(inst_id)
        buy = side == "buy"
        volume = self._volume(info, sz)
        if reduce_only:
            if ord_type != "market":
                raise Mt5Error("mt5_reduce", "MT5 没有只减仓的限价单。要在某个价位平仓，请用止盈价")
            return self._reduce(symbol, info, "short" if buy else "long", volume, cl_ord_id)
        if ord_type == "market":
            return str(self._market_order(symbol, info, buy, volume, cl_ord_id, sl=sl_trigger_px, tp=tp_trigger_px).order)
        if px is None:
            raise Mt5Error("mt5_price", "限价单要有价格")
        request = {
            "action": ACTION_PENDING, "symbol": symbol, "volume": volume, "type": BUY_LIMIT if buy else SELL_LIMIT,
            "price": self._price(info, px), "type_time": TIME_GTC, "type_filling": FILLING_RETURN, "comment": cl_ord_id[:31],
        }  # fmt: skip
        if sl_trigger_px:
            request["sl"] = self._price(info, sl_trigger_px)
        if tp_trigger_px:
            request["tp"] = self._price(info, tp_trigger_px)
        return str(self._send(request, "这张挂单").order)

    def _reduce(self, symbol: str, info, direction: str, volume: float, comment: str) -> str:
        tickets = self._direction_tickets(symbol, direction)
        held = sum(float(row.volume) for row in tickets)
        if held + 1e-9 < volume:
            raise Mt5Error("mt5_reduce", f"要平 {_number(volume)} 手，可{_side(direction)}单只有 {_number(held)} 手")
        left = volume
        last = ""
        for done, row in enumerate(tickets):
            if left <= 1e-9:
                break
            part = min(float(row.volume), left)
            try:
                last = str(self._market_order(symbol, info, direction == "short", part, comment, ticket=int(row.ticket)).order)
            except OkxError as exc:
                if done:
                    raise Mt5Error(exc.code, f"{exc}（前面 {done} 张单已经平掉）") from None
                raise
            left -= part
        return last

    def cancel_order(self, inst_id: str, ord_id: str) -> None:
        self._send({"action": ACTION_REMOVE, "order": int(ord_id)}, "撤单")

    def close_position(self, inst_id: str, mgn_mode: str, pos_side: str | None = None, cl_ord_id: str = "") -> None:
        """Closes every ticket of the position at market. Their stops go with them."""
        symbol = self.symbol(inst_id)
        info = self._info(inst_id)
        direction = pos_side if pos_side in ("long", "short") else None
        tickets = self._direction_tickets(symbol, direction)
        if not tickets:
            raise Mt5Error("mt5_no_position", "MT5 里没有这个仓位")
        for done, row in enumerate(tickets):
            long = int(row.type) == POSITION_BUY
            try:
                self._market_order(symbol, info, not long, float(row.volume), cl_ord_id, ticket=int(row.ticket))
            except OkxError as exc:
                if done:
                    raise Mt5Error(exc.code, f"{exc}（前面 {done} 张单已经平掉）") from None
                raise

    # ------------------------------------------------ take-profit / stop-loss

    def _set_stops(self, symbol: str, direction: str, sl, tp, clear: bool = False) -> None:
        """Sets the stop-loss and/or take-profit of every ticket of one position. None keeps a ticket's own."""
        info = self._link.call("symbol_info", symbol)
        tickets = self._direction_tickets(symbol, direction)
        if not tickets:
            raise Mt5Error("mt5_no_position", "MT5 里没有这个仓位，止损止盈挂不上")
        for done, row in enumerate(tickets):
            new_sl = 0.0 if clear else (self._price(info, sl) if sl else float(row.sl or 0))
            new_tp = 0.0 if clear else (self._price(info, tp) if tp else float(row.tp or 0))
            request = {"action": ACTION_SLTP, "symbol": symbol, "position": int(row.ticket), "sl": new_sl, "tp": new_tp}
            try:
                self._send(request, "改止损止盈")
            except OkxError as exc:
                if done:
                    raise Mt5Error(exc.code, f"{exc}（前面 {done} 张单已经改好）") from None
                raise

    def _stop_target(self, algo_id: str) -> tuple[str, str] | None:
        if not algo_id.startswith(STOP_PREFIX):
            return None
        symbol, _, direction = algo_id[len(STOP_PREFIX) :].rpartition(":")
        return symbol, direction

    def place_stop(
        self, *, inst_id: str, td_mode: str, side: str, cl_ord_id: str, pos_side: str | None = None,
        sl_trigger_px: str | None = None, tp_trigger_px: str | None = None,
    ) -> str:  # fmt: skip
        if not sl_trigger_px and not tp_trigger_px:
            raise ValueError("a stop needs a stop-loss or a take-profit price")
        symbol = self.symbol(inst_id)
        direction = "long" if side == "sell" else "short"
        self._set_stops(symbol, direction, sl_trigger_px, tp_trigger_px)
        return f"{STOP_PREFIX}{symbol}:{direction}"

    def place_trigger(
        self, *, inst_id: str, td_mode: str, side: str, sz: str, trigger_px: str, cl_ord_id: str,
        pos_side: str | None = None, sl_trigger_px: str | None = None, tp_trigger_px: str | None = None,
    ) -> str:  # fmt: skip
        """A buy stop above the price or a sell stop below it; the other way round it is a limit order."""
        symbol = self.symbol(inst_id)
        info = self._info(inst_id)
        tick = self._tick(symbol)
        buy = side == "buy"
        price = self._price(info, trigger_px)
        if buy:
            kind = BUY_STOP if price > float(tick.ask) else BUY_LIMIT
        else:
            kind = SELL_STOP if price < float(tick.bid) else SELL_LIMIT
        request = {
            "action": ACTION_PENDING, "symbol": symbol, "volume": self._volume(info, sz), "type": kind, "price": price,
            "type_time": TIME_GTC, "type_filling": FILLING_RETURN, "comment": cl_ord_id[:31],
        }  # fmt: skip
        if sl_trigger_px:
            request["sl"] = self._price(info, sl_trigger_px)
        if tp_trigger_px:
            request["tp"] = self._price(info, tp_trigger_px)
        return str(self._send(request, "突破单").order)

    def amend_stop(self, inst_id: str, algo_id: str, sl_trigger_px: str | None = None, tp_trigger_px: str | None = None) -> None:
        target = self._stop_target(algo_id)
        if target is not None:
            self._set_stops(target[0], target[1], sl_trigger_px, tp_trigger_px)
            return
        # The stop-loss / take-profit that comes with a pending order.
        rows = [row for row in self._pending() if str(row.ticket) == str(algo_id)]
        if not rows:
            raise Mt5Error("51603", "MT5 里查不到这张单")
        row = rows[0]
        info = self._link.call("symbol_info", str(row.symbol))
        request = {
            "action": ACTION_MODIFY, "order": int(row.ticket), "symbol": str(row.symbol), "price": float(row.price_open),
            "sl": self._price(info, sl_trigger_px) if sl_trigger_px else float(row.sl or 0),
            "tp": self._price(info, tp_trigger_px) if tp_trigger_px else float(row.tp or 0), "type_time": TIME_GTC,
        }  # fmt: skip
        self._send(request, "改挂单的止损止盈")

    def cancel_stops(self, items: list[tuple[str, str]]) -> None:
        """Removes the stops of positions ("p:" ids) and cancels pending stop orders (trigger orders)."""
        for inst_id, algo_id in items:
            target = self._stop_target(str(algo_id))
            if target is not None:
                self._set_stops(target[0], target[1], None, None, clear=True)
            else:
                self._send({"action": ACTION_REMOVE, "order": int(algo_id)}, "撤单")


def _side(direction: str) -> str:
    return "多" if direction == "long" else "空"

