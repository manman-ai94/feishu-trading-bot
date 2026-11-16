"""A small OKX v5 REST client.

This class only reads. There is deliberately no method here that places,
changes or cancels an order, and none that moves funds. The few calls that
do change something live in okx_trade.py, in a class the bot creates only
when the owner has switched trading on.

Signing follows the OKX docs: Base64(HMAC-SHA256(secret,
timestamp + METHOD + requestPath + body)), where requestPath includes the
query string for GET requests.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from .models import (
    AccountConfig,
    AccountSnapshot,
    AccountState,
    Candle,
    InstrumentSpec,
    OpenInterest,
    OrderInfo,
    Position,
    StopOrder,
    Ticker,
    to_float,
)

log = logging.getLogger(__name__)

TIMESTAMP_ERROR_CODES = {"50102", "50112"}  # request timestamp out of range / invalid
SPEC_TTL_SECONDS = 6 * 3600


class OkxError(Exception):
    """OKX answered, but with an error code (or an HTTP error)."""

    def __init__(self, code: str, message: str, http_status: int | None = None):
        super().__init__(f"OKX error {code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


class OkxNetworkError(Exception):
    """OKX could not be reached at all."""


def sign(secret_key: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def iso_timestamp(epoch_seconds: float) -> str:
    """ISO 8601 UTC with milliseconds, e.g. 2020-12-08T09:08:57.715Z"""
    moment = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class OkxClient:
    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        base_url: str = "https://www.okx.com",
        demo: bool = False,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ):
        self._api_key = api_key
        self._secret_key = secret_key
        self._passphrase = passphrase
        self._base_url = base_url.rstrip("/")
        self._demo = demo
        self._session = session or requests.Session()
        self._timeout = timeout
        self._clock_offset = 0.0  # server time minus local time, seconds
        self._specs: dict[str, tuple[float, InstrumentSpec]] = {}  # contract rules, kept for a few hours

    # ------------------------------------------------------------------ core

    def _headers(self, method: str, request_path: str, auth: bool, body: str = "") -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._demo:
            headers["x-simulated-trading"] = "1"
        if auth:
            if not (self._api_key and self._secret_key and self._passphrase):
                raise OkxError("no_keys", "OKX API key 还没有配置")
            timestamp = iso_timestamp(time.time() + self._clock_offset)
            headers.update(
                {
                    "OK-ACCESS-KEY": self._api_key,
                    "OK-ACCESS-SIGN": sign(self._secret_key, timestamp, method, request_path, body),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self._passphrase,
                }
            )
        return headers

    def _get(self, path: str, params: dict | None = None, auth: bool = False, _retried: bool = False) -> list:
        query = urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        request_path = f"{path}?{query}" if query else path
        try:
            response = self._session.get(
                self._base_url + request_path,
                headers=self._headers("GET", request_path, auth),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise OkxNetworkError(f"连不上 OKX：{type(exc).__name__}") from exc

        try:
            payload = response.json()
        except ValueError:
            raise OkxError("bad_response", f"OKX 返回了无法解析的内容（HTTP {response.status_code}）", response.status_code)

        code = str(payload.get("code", ""))
        if code != "0":
            if auth and code in TIMESTAMP_ERROR_CODES and not _retried:
                # Local clock is off: measure the difference and try once more.
                self.sync_clock()
                return self._get(path, params, auth, _retried=True)
            raise OkxError(code, str(payload.get("msg", "")), response.status_code)
        return payload.get("data") or []

    # ---------------------------------------------------------------- public

    def server_time_ms(self) -> int:
        data = self._get("/api/v5/public/time")
        return int(data[0]["ts"])

    def sync_clock(self) -> float:
        """Measure server-minus-local clock difference (seconds) and remember it."""
        before = time.time()
        server_ms = self.server_time_ms()
        after = time.time()
        self._clock_offset = server_ms / 1000.0 - (before + after) / 2.0
        return self._clock_offset

    def ticker(self, inst_id: str) -> Ticker:
        data = self._get("/api/v5/market/ticker", {"instId": inst_id})
        if not data:
            raise OkxError("no_data", f"OKX 没有返回 {inst_id} 的行情")
        row = data[0]
        last = to_float(row.get("last"))
        if last is None:
            raise OkxError("no_data", f"OKX 返回的 {inst_id} 行情里没有最新价")
        return Ticker(
            inst_id=row.get("instId", inst_id),
            last=last,
            bid=to_float(row.get("bidPx")),
            ask=to_float(row.get("askPx")),
            open_24h=to_float(row.get("open24h")),
            high_24h=to_float(row.get("high24h")),
            low_24h=to_float(row.get("low24h")),
            ts=int(to_float(row.get("ts")) or 0),
        )

    def candles(self, inst_id: str, bar: str = "1H", limit: int = 200) -> list[Candle]:
        """Candles ordered oldest first (OKX sends newest first)."""
        data = self._get("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": min(limit, 300)})
        candles: list[Candle] = []
        for row in data:
            if len(row) < 5:
                continue
            values = [to_float(item) for item in row[:6]] + [None] * (6 - len(row[:6]))
            ts, open_, high, low, close, volume = values
            if None in (ts, open_, high, low, close):
                continue
            confirmed = str(row[8]) == "1" if len(row) > 8 else True
            candles.append(Candle(int(ts), open_, high, low, close, volume or 0.0, confirmed))
        candles.sort(key=lambda candle: candle.ts)
        return candles

    def funding_rate(self, inst_id: str) -> float | None:
        data = self._get("/api/v5/public/funding-rate", {"instId": inst_id})
        return to_float(data[0].get("fundingRate")) if data else None

    def funding(self, inst_id: str) -> tuple[float, float] | None:
        """(the rate of the current funding period, the length of a period in hours). None when OKX has none."""
        data = self._get("/api/v5/public/funding-rate", {"instId": inst_id})
        if not data:
            return None
        rate = to_float(data[0].get("fundingRate"))
        if rate is None:
            return None
        this, following = to_float(data[0].get("fundingTime")), to_float(data[0].get("nextFundingTime"))
        hours = (following - this) / 3_600_000 if this and following and following > this else 8.0
        return rate, hours

    def instruments(self, inst_type: str) -> list[dict]:
        """Every instrument of one type (SPOT, SWAP, ...), as OKX lists them."""
        return self._get("/api/v5/public/instruments", {"instType": inst_type})

    def open_interest(self, inst_id: str) -> OpenInterest | None:
        """Open interest of a perpetual or futures contract. None for spot."""
        parts = inst_id.split("-")
        if len(parts) < 3:
            return None
        inst_type = "SWAP" if parts[-1] == "SWAP" else "FUTURES"
        data = self._get("/api/v5/public/open-interest", {"instType": inst_type, "instId": inst_id})
        if not data:
            return None
        row = data[0]
        return OpenInterest(coins=to_float(row.get("oiCcy")), usd=to_float(row.get("oiUsd")))

    def top_trader_ratio(self, inst_id: str, by: str = "account", period: str = "1H") -> list[tuple[int, float]]:
        """Long/short ratio of OKX's top traders in one contract, newest first: (time in ms, ratio).

        by="account" counts accounts that are net long against those net short;
        by="position" compares the size of their long and short positions.
        """
        kind = "position" if by == "position" else "account"
        data = self._get(
            f"/api/v5/rubik/stat/contracts/long-short-{kind}-ratio-contract-top-trader",
            {"instId": inst_id, "period": period},
        )
        rows = []
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            ts, ratio = to_float(row[0]), to_float(row[1])
            if ts is not None and ratio is not None:
                rows.append((int(ts), ratio))
        rows.sort(reverse=True)
        return rows

    # --------------------------------------------------------------- private

    def balance(self) -> AccountSnapshot:
        data = self._get("/api/v5/account/balance", auth=True)
        return AccountSnapshot.from_okx(data[0]) if data else AccountSnapshot()

    def positions(self, inst_type: str | None = None) -> list[Position]:
        data = self._get("/api/v5/account/positions", {"instType": inst_type}, auth=True)
        positions = [Position.from_okx(row) for row in data]
        return [position for position in positions if position is not None]

    def pending_stops(self) -> list[dict]:
        """Open take-profit / stop-loss orders (conditional and OCO)."""
        return self._get("/api/v5/trade/orders-algo-pending", {"ordType": "conditional,oco"}, auth=True)

    def algo_history(self, ord_type: str) -> list[dict]:
        """Take-profit / stop-loss orders of one type ("conditional" or "oco") that went off, newest first:
        the last 100 of the past three months. `actualSide` says which side fired ("sl" or "tp")."""
        return self._get(
            "/api/v5/trade/orders-algo-history", {"ordType": ord_type, "state": "effective", "limit": "100"}, auth=True
        )

    def position_history(self) -> list[dict]:
        """Closed and partly closed positions, newest first: the last 100 of the past three months, each with
        its open and close price, realised P&L with fees and funding, and how it closed (`type`)."""
        return self._get("/api/v5/account/positions-history", {"limit": "100"}, auth=True)

    def account_config(self) -> AccountConfig:
        """What this API key is allowed to do, and the account's position mode."""
        data = self._get("/api/v5/account/config", auth=True)
        return AccountConfig.from_okx(data[0]) if data else AccountConfig()

    def instrument_spec(self, inst_id: str) -> InstrumentSpec:
        """Contract size, size step and price step of one instrument."""
        known = self._specs.get(inst_id)
        if known is not None and time.time() - known[0] < SPEC_TTL_SECONDS:
            return known[1]
        parts = inst_id.split("-")
        inst_type = "SPOT" if len(parts) == 2 else ("SWAP" if parts[-1] == "SWAP" else "FUTURES")
        data = self._get("/api/v5/public/instruments", {"instType": inst_type, "instId": inst_id})
        spec = InstrumentSpec.from_okx(data[0]) if data else None
        if spec is None:
            raise OkxError("no_data", f"OKX 没有返回 {inst_id} 的合约规格")
        self._specs[inst_id] = (time.time(), spec)
        return spec

    def leverage(self, inst_id: str, mgn_mode: str) -> float | None:
        """The leverage set on OKX for this instrument and margin mode."""
        data = self._get("/api/v5/account/leverage-info", {"instId": inst_id, "mgnMode": mgn_mode}, auth=True)
        return to_float(data[0].get("lever")) if data else None

    def pending_orders(self, inst_id: str | None = None) -> list[OrderInfo]:
        """Open orders on perpetual contracts that are waiting to be filled (not take-profit / stop-loss)."""
        data = self._get("/api/v5/trade/orders-pending", {"instType": "SWAP", "instId": inst_id}, auth=True)
        return [OrderInfo.from_okx(row) for row in data]

    def order(self, inst_id: str, ord_id: str = "", cl_ord_id: str = "") -> OrderInfo:
        """One order, by OKX's id or by the id the bot gave it."""
        data = self._get("/api/v5/trade/order", {"instId": inst_id, "ordId": ord_id, "clOrdId": cl_ord_id}, auth=True)
        if not data:
            raise OkxError("no_data", "OKX 没有返回这张订单")
        return OrderInfo.from_okx(data[0])

    def account_state(self) -> AccountState:
        """Balance + positions, with stop orders attached to their positions."""
        snapshot = self.balance()
        positions = self.positions()
        stops_known = True
        if positions:
            try:
                positions = attach_stops(positions, self.pending_stops())
            except (OkxError, OkxNetworkError) as exc:
                # Never lose the positions because of the stops; say that the stops are not known.
                stops_known = False
                log.warning("could not read pending stop orders: %s", exc)
        if snapshot.mgn_ratio is None:
            # Still nothing at account or currency level: a cross position carries the ratio of its margin pool.
            ratios = [p.mgn_ratio for p in positions if p.mgn_mode == "cross" and p.mgn_ratio is not None]
            if ratios:
                snapshot = replace(snapshot, mgn_ratio=min(ratios))
        return AccountState(
            snapshot=snapshot, positions=[self._with_coins(position) for position in positions], stops_known=stops_known
        )

    def _with_coins(self, position: Position) -> Position:
        """Adds what the contracts add up to (10000 contracts of 0.001 XAU are 10 XAU)."""
        if position.inst_type not in ("SWAP", "FUTURES"):
            return position
        try:
            spec = self.instrument_spec(position.inst_id)
        except (OkxError, OkxNetworkError) as exc:
            log.warning("could not read the contract size of %s: %s", position.inst_id, exc)
            return position
        return replace(position, coins=float(position.qty * float(spec.coins_per_contract)), coin_ccy=spec.ct_val_ccy)


def attach_stops(positions: list[Position], algo_orders: list[dict]) -> list[Position]:
    """Match pending TP/SL orders to the positions they would close."""
    result: list[Position] = []
    for position in positions:
        closing_side = "sell" if position.direction == "long" else "buy"
        known = {stop.algo_id for stop in position.stops}
        extra: list[StopOrder] = []
        for order in algo_orders:
            if order.get("instId") != position.inst_id:
                continue
            order_pos_side = order.get("posSide") or "net"
            if position.pos_side in ("long", "short"):
                if order_pos_side != position.pos_side:
                    continue
            elif order.get("side") != closing_side:
                continue
            algo_id = str(order.get("algoId", ""))
            if algo_id in known:
                continue
            stop = StopOrder(
                algo_id=algo_id,
                sl_trigger_px=to_float(order.get("slTriggerPx")),
                tp_trigger_px=to_float(order.get("tpTriggerPx")),
            )
            if stop.sl_trigger_px or stop.tp_trigger_px:
                extra.append(stop)
        result.append(replace(position, stops=position.stops + tuple(extra)) if extra else position)
    return result
