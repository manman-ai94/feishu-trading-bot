"""OKX's private WebSockets, used only as a doorbell for the monitor.

The monitor reads the account over REST every POLL_SECONDS and decides what to tell the owner. With the
stream on, OKX pushes a message the moment a position, a stop order or a liquidation warning changes, and
each push makes the monitor read the account right away instead of at the next tick. Nothing in a push is
trusted or acted on by itself: the REST read that follows is the one source of truth, and the regular poll
stays as the reconciliation when the stream is down.

OKX serves the channels on two addresses: positions and liquidation warnings on /ws/v5/private, take-profit
and stop-loss orders (orders-algo) on /ws/v5/business. Asking the private address for orders-algo is refused
with error 60018, so each address gets a connection of its own, both logged in with the same read-only use.

It only reads. It never sends an order; the channels it subscribes to cannot place one.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable
from urllib.parse import urlparse

from .okx_client import sign

log = logging.getLogger(__name__)

LIVE_URL = "wss://ws.okx.com:8443/ws/v5/private"
DEMO_URL = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"
# What should wake the monitor: a position opened, changed or closed; OKX warning that a position is near
# liquidation (on /private); a stop or take-profit placed, moved, triggered or cancelled (on /business).
CHANNELS = (
    {"channel": "positions", "instType": "ANY", "extraParams": json.dumps({"updateInterval": "0"})},
    {"channel": "liquidation-warning", "instType": "ANY"},
)
BUSINESS_CHANNELS = ({"channel": "orders-algo", "instType": "ANY"},)
PING_SECONDS = 25  # OKX drops a connection that is quiet for 30 seconds
FIRST_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 300
MIN_POKE_GAP_SECONDS = 2.0  # a burst of pushes makes one read, not ten
LOGIN_WAIT_SECONDS = 30  # no answer to the login by then: connect again


class LoginRefused(Exception):
    """OKX turned the login down, or never answered it. The connection is dropped and tried again later."""


REGIONAL_URLS = {
    "my.okx.com": "wss://wseea.okx.com:8443/ws/v5/private",  # OKX EEA
    "app.okx.com": "wss://wsus.okx.com:8443/ws/v5/private",  # OKX US
}


def stream_url(base_url: str, demo: bool) -> str:
    """The private WebSocket that belongs to the REST host the bot reads from; "" when it is not known
    (then set OKX_WS_URL, or the bot keeps to the regular poll)."""
    if demo:
        return DEMO_URL
    host = urlparse(base_url).hostname or "www.okx.com"
    if host in ("www.okx.com", "okx.com", "aws.okx.com"):
        return LIVE_URL
    return REGIONAL_URLS.get(host, "")


def business_url(private_url: str) -> str:
    """The business address next to a private one ("" when the address does not have the usual shape)."""
    return private_url.replace("/ws/v5/private", "/ws/v5/business", 1) if "/ws/v5/private" in private_url else ""


def login_message(api_key: str, secret_key: str, passphrase: str, now: float) -> str:
    timestamp = str(int(now))
    return json.dumps(
        {
            "op": "login",
            "args": [
                {
                    "apiKey": api_key,
                    "passphrase": passphrase,
                    "timestamp": timestamp,
                    "sign": sign(secret_key, timestamp, "GET", "/users/self/verify"),
                }
            ],
        }
    )


def subscribe_message(channels=CHANNELS) -> str:
    return json.dumps({"op": "subscribe", "args": list(channels)})


class OkxStream:
    """Keeps the private stream open in a thread of its own and calls `poke()` when OKX pushes a change."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        url: str,
        poke: Callable[[str], None],
        connect=None,
        clock: Callable[[], float] = time.time,
        channels=CHANNELS,
        name: str = "okx stream",
    ):
        self._channels = tuple(channels)
        self.name = name  # how its log lines start
        self._api_key = api_key
        self._secret_key = secret_key
        self._passphrase = passphrase
        self._url = url
        self._poke = poke
        self._connect = connect  # websockets.sync.client.connect, or a stand-in in the tests
        self._clock = clock
        self._last_poke = 0.0
        self.logged_in = False
        self.subscribed = False
        self._refused = False
        self.last_error = ""  # why the last connection ended, for the settings card

    # ----------------------------------------------------------------- messages

    def handle(self, raw: str) -> None:
        """One message from OKX."""
        if raw == "pong":
            return
        try:
            message = json.loads(raw)
        except ValueError:
            log.info("%s: a message that is not JSON was ignored", self.name)
            return
        event = message.get("event")
        if event == "login":
            self.logged_in = message.get("code") == "0"
            self._refused = not self.logged_in
            if self.logged_in:
                log.info("%s: logged in", self.name)
            else:
                log.warning("%s: login refused (code %s)", self.name, message.get("code"))
            return
        if event == "subscribe":
            log.info("%s: subscribed to %s", self.name, (message.get("arg") or {}).get("channel"))
            return
        if event == "error":
            log.warning("%s: error %s %s", self.name, message.get("code"), str(message.get("msg", ""))[:200])
            return
        channel = (message.get("arg") or {}).get("channel")
        if channel and message.get("data") is not None:
            self._ring(channel)

    def _ring(self, channel: str) -> None:
        now = self._clock()
        if now - self._last_poke < MIN_POKE_GAP_SECONDS:
            return
        self._last_poke = now
        try:
            self._poke(channel)
        except Exception:
            log.exception("%s: the monitor could not be woken", self.name)

    # ---------------------------------------------------------------- the loop

    @property
    def up(self) -> bool:
        return self.logged_in and self.subscribed

    def status(self) -> str:
        """One line for the settings card."""
        if self.up:
            return "已连上，止损被打、仓位变化、强平预警几秒内就提醒"
        return f"没连上，正在重连{self.why()}；期间照常定时读账户"

    def why(self) -> str:
        return f"（上次断开：{self.last_error}）" if self.last_error else ""

    def run_forever(self, stop: threading.Event) -> None:
        connect = self._connect
        if connect is None:
            try:
                from websockets.sync.client import connect
            except ImportError:
                self.last_error = "服务器上缺 websockets 这个包"
                log.warning("%s: the websockets package is missing; positions are read on the regular poll only", self.name)
                return
        wait = FIRST_RETRY_SECONDS
        while not stop.is_set():
            started = self._clock()
            try:
                with connect(self._url, open_timeout=15, close_timeout=5) as socket:
                    log.info("%s: connected", self.name)
                    self._session(socket, stop)
            except LoginRefused as exc:
                self.last_error = str(exc)
                log.warning("%s: %s", self.name, exc)
            except Exception as exc:  # the regular poll carries on whatever happens here
                self.last_error = type(exc).__name__
                log.warning("%s: disconnected (%s)", self.name, type(exc).__name__)
            self.logged_in = self.subscribed = False
            if stop.is_set():
                break
            if self._clock() - started > 60:
                wait = FIRST_RETRY_SECONDS  # it had been up for a while: try again soon
            stop.wait(wait)
            wait = min(wait * 2, MAX_RETRY_SECONDS)

    def _session(self, socket, stop: threading.Event) -> None:
        self._refused = False
        socket.send(login_message(self._api_key, self._secret_key, self._passphrase, self._clock()))
        sent_at = self._clock()
        while not stop.is_set():
            try:
                raw = socket.recv(timeout=PING_SECONDS)
            except TimeoutError:
                if not self.logged_in and self._clock() - sent_at > LOGIN_WAIT_SECONDS:
                    raise LoginRefused("OKX 没有回应登录")
                socket.send("ping")
                continue
            self.handle(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
            if self._refused:
                raise LoginRefused("OKX 拒绝了登录，可能是 key 或 IP 白名单的问题")
            if self.logged_in and not self.subscribed:
                socket.send(subscribe_message(self._channels))
                self.subscribed = True
                self.last_error = ""


class Streams:
    """The private connection and, where there is one, the business connection, reported as one line."""

    def __init__(self, private: OkxStream, business: OkxStream | None = None):
        self.private = private
        self.business = business

    def status(self) -> str:
        private, business = self.private, self.business
        if business is None:
            if private.up:
                return "已连上仓位和强平预警，几秒内就提醒；止盈止损单的变化靠定时读账户"
            return private.status()
        if private.up and business.up:
            return private.status()
        if private.up:
            return f"仓位和强平预警已连上；止盈止损单那一路没连上，正在重连{business.why()}，期间照常定时读账户"
        if business.up:
            return f"止盈止损单已连上；仓位和强平预警那一路没连上，正在重连{private.why()}，期间照常定时读账户"
        return private.status()
