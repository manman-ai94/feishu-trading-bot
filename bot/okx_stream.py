"""OKX's private WebSocket, used only as a doorbell for the monitor.

The monitor reads the account over REST every POLL_SECONDS and decides what to tell the owner. With the
stream on, OKX pushes a message the moment a position, a stop order or a liquidation warning changes, and
each push makes the monitor read the account right away instead of at the next tick. Nothing in a push is
trusted or acted on by itself: the REST read that follows is the one source of truth, and the regular poll
stays as the reconciliation when the stream is down.

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
# What should wake the monitor: a position opened, changed or closed; a stop or take-profit placed,
# moved, triggered or cancelled; OKX warning that a position is near liquidation.
CHANNELS = (
    {"channel": "positions", "instType": "ANY", "extraParams": json.dumps({"updateInterval": "0"})},
    {"channel": "orders-algo", "instType": "ANY"},
    {"channel": "liquidation-warning", "instType": "ANY"},
)
PING_SECONDS = 25  # OKX drops a connection that is quiet for 30 seconds
FIRST_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 300
MIN_POKE_GAP_SECONDS = 2.0  # a burst of pushes makes one read, not ten


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


def subscribe_message() -> str:
    return json.dumps({"op": "subscribe", "args": list(CHANNELS)})


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
    ):
        self._api_key = api_key
        self._secret_key = secret_key
        self._passphrase = passphrase
        self._url = url
        self._poke = poke
        self._connect = connect  # websockets.sync.client.connect, or a stand-in in the tests
        self._clock = clock
        self._last_poke = 0.0
        self.logged_in = False

    # ----------------------------------------------------------------- messages

    def handle(self, raw: str) -> None:
        """One message from OKX."""
        if raw == "pong":
            return
        try:
            message = json.loads(raw)
        except ValueError:
            log.info("okx stream: a message that is not JSON was ignored")
            return
        event = message.get("event")
        if event == "login":
            self.logged_in = message.get("code") == "0"
            if self.logged_in:
                log.info("okx stream: logged in")
            else:
                log.warning("okx stream: login refused (code %s)", message.get("code"))
            return
        if event == "subscribe":
            log.info("okx stream: subscribed to %s", (message.get("arg") or {}).get("channel"))
            return
        if event == "error":
            log.warning("okx stream: error %s", message.get("code"))
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
            log.exception("okx stream: the monitor could not be woken")

    # ---------------------------------------------------------------- the loop

    def run_forever(self, stop: threading.Event) -> None:
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect
        wait = FIRST_RETRY_SECONDS
        while not stop.is_set():
            started = self._clock()
            try:
                with connect(self._url, open_timeout=15, close_timeout=5) as socket:
                    log.info("okx stream: connected")
                    self._session(socket, stop)
            except Exception as exc:  # the regular poll carries on whatever happens here
                log.warning("okx stream: disconnected (%s)", type(exc).__name__)
            self.logged_in = False
            if stop.is_set():
                break
            if self._clock() - started > 60:
                wait = FIRST_RETRY_SECONDS  # it had been up for a while: try again soon
            stop.wait(wait)
            wait = min(wait * 2, MAX_RETRY_SECONDS)

    def _session(self, socket, stop: threading.Event) -> None:
        socket.send(login_message(self._api_key, self._secret_key, self._passphrase, self._clock()))
        subscribed = False
        while not stop.is_set():
            try:
                raw = socket.recv(timeout=PING_SECONDS)
            except TimeoutError:
                socket.send("ping")
                continue
            self.handle(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
            if self.logged_in and not subscribed:
                socket.send(subscribe_message())
                subscribed = True
