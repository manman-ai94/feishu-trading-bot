"""The OKX private stream only rings the monitor's doorbell; it never acts on what it is told."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading

import pytest

from bot.okx_stream import DEMO_URL, LIVE_URL, OkxStream, login_message, stream_url, subscribe_message

from .helpers import Clock


def test_the_stream_address_follows_the_rest_host():
    assert stream_url("https://www.okx.com", demo=False) == LIVE_URL
    assert stream_url("https://www.okx.com", demo=True) == DEMO_URL
    assert stream_url("https://my.okx.com", demo=False).startswith("wss://wseea.okx.com")
    assert stream_url("https://unknown.example.com", demo=False) == ""


def test_login_is_signed_like_okx_asks():
    message = json.loads(login_message("key", "secret", "pass", 1_800_000_000.9))
    [args] = message["args"]
    expected = base64.b64encode(hmac.new(b"secret", b"1800000000GET/users/self/verify", hashlib.sha256).digest()).decode()
    assert message["op"] == "login" and args["timestamp"] == "1800000000" and args["sign"] == expected
    assert args["apiKey"] == "key" and args["passphrase"] == "pass"


def test_it_subscribes_only_to_channels_that_read():
    channels = [arg["channel"] for arg in json.loads(subscribe_message())["args"]]
    assert channels == ["positions", "orders-algo", "liquidation-warning"]


def make_stream(clock=None):
    pokes: list[str] = []
    stream = OkxStream("key", "secret", "pass", LIVE_URL, pokes.append, clock=clock or Clock(1_800_000_000.0))
    return stream, pokes


def test_a_push_wakes_the_monitor_and_a_burst_wakes_it_once():
    clock = Clock(1_800_000_000.0)
    stream, pokes = make_stream(clock)
    push = json.dumps({"arg": {"channel": "orders-algo"}, "data": [{"state": "effective"}]})
    stream.handle(push)
    stream.handle(push)
    assert pokes == ["orders-algo"]
    clock.advance(3)
    stream.handle(json.dumps({"arg": {"channel": "positions"}, "data": []}))
    assert pokes == ["orders-algo", "positions"]


def test_control_messages_do_not_wake_the_monitor():
    stream, pokes = make_stream()
    stream.handle("pong")
    stream.handle("not json")
    stream.handle(json.dumps({"event": "login", "code": "0"}))
    assert stream.logged_in
    stream.handle(json.dumps({"event": "subscribe", "arg": {"channel": "positions"}}))
    stream.handle(json.dumps({"event": "error", "code": "60012"}))
    stream.handle(json.dumps({"event": "login", "code": "60009"}))
    assert not stream.logged_in and pokes == []


class FakeSocket:
    def __init__(self, incoming, stop):
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.stop = stop

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout=None):
        if not self.incoming:
            self.stop.set()
            raise ConnectionError("closed")
        item = self.incoming.pop(0)
        if item is TimeoutError:
            raise TimeoutError
        return item


def test_a_session_logs_in_subscribes_pings_and_rings():
    stop = threading.Event()
    socket = FakeSocket(
        [
            json.dumps({"event": "login", "code": "0"}),
            TimeoutError,
            json.dumps({"arg": {"channel": "positions"}, "data": [{"pos": "10"}]}),
        ],
        stop,
    )
    pokes: list[str] = []
    stream = OkxStream("key", "secret", "pass", LIVE_URL, pokes.append, connect=lambda url, **kw: socket, clock=Clock(1_800_000_000.0))
    stream.run_forever(stop)
    assert json.loads(socket.sent[0])["op"] == "login"
    assert json.loads(socket.sent[1])["op"] == "subscribe"
    assert socket.sent[2] == "ping"
    assert pokes == ["positions"]


def test_a_refused_connection_never_raises(monkeypatch):
    stop = threading.Event()
    attempts = []

    def refuse(url, **kwargs):
        attempts.append(url)
        stop.set()
        raise OSError("refused")

    stream = OkxStream("key", "secret", "pass", LIVE_URL, lambda channel: None, connect=refuse)
    stream.run_forever(stop)  # returns once stopped, without an exception
    assert attempts == [LIVE_URL]


def test_the_monitor_reads_at_once_when_poked():
    from bot.journal import Journal
    from bot.monitor import Monitor

    from .helpers import FakeOkx, make_cfg, make_state

    okx = FakeOkx(make_state())
    monitor = Monitor(okx, make_cfg(poll_seconds=3600), Journal(":memory:"), lambda event: None, Clock(1_800_000_000.0))
    stop = threading.Event()
    thread = threading.Thread(target=monitor.run_forever, args=(stop,), daemon=True)
    thread.start()
    for _ in range(100):
        if okx.account_reads >= 1:
            break
        threading.Event().wait(0.01)
    reads = okx.account_reads
    monitor.poke("positions")
    for _ in range(200):
        if okx.account_reads > reads:
            break
        threading.Event().wait(0.01)
    stop.set()
    monitor.poke("stop")
    assert okx.account_reads > reads  # without the poke the next read was an hour away
