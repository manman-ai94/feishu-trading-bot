"""A small OKX v5 REST client.

This version only reads. There is deliberately no method that places,
changes or cancels an order, and none that moves funds.

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

from .models import AccountSnapshot, AccountState, Candle, Position, StopOrder, Ticker, to_float

log = logging.getLogger(__name__)

TIMESTAMP_ERROR_CODES = {"50102", "50112"}  # request timestamp out of range / invalid


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

    # ------------------------------------------------------------------ core

    def _headers(self, method: str, request_path: str, auth: bool) -> dict[str, str]:
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
                    "OK-ACCESS-SIGN": sign(self._secret_key, timestamp, method, request_path),
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

    def account_state(self) -> AccountState:
        """Balance + positions, with stop orders attached to their positions."""
        snapshot = self.balance()
        positions = self.positions()
        if positions:
            try:
                positions = attach_stops(positions, self.pending_stops())
            except (OkxError, OkxNetworkError) as exc:
                # Stops are a nice-to-have; never lose the positions because of them.
                log.warning("could not read pending stop orders: %s", exc)
        return AccountState(snapshot=snapshot, positions=positions)


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
