"""Feishu's long connection (长连接), spoken directly instead of through the official SDK.

The SDK (lark-oapi) loads every one of Feishu's APIs when it is imported, about 140 MB of memory,
and the bot uses a handful. The long connection itself is small:

  1. POST {domain}/callback/ws/endpoint with the app's id and secret gives a wss:// address and
     the client settings (how often to ping, how to reconnect).
  2. On that WebSocket each message is one protobuf `pbbp2.Frame`. Control frames (method 0) are
     pings and pongs; a pong may carry new settings. Data frames (method 1) carry events, maybe
     split in parts ("sum", "seq"). Each event is answered with the same frame, a "biz_rt" header
     and the payload {"code": 200}.

This follows lark-oapi 1.7.5 (lark_oapi/ws/client.py) step by step, including its log lines'
levels: a connection that cannot be opened is an ERROR, which is what the deploy watches for.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake

log = logging.getLogger(__name__)

ENDPOINT_PATH = "/callback/ws/endpoint"
CONTROL, DATA = 0, 1
OK, SYSTEM_BUSY, INTERNAL_ERROR = 0, 1, 1000040343
FORBIDDEN, AUTH_FAILED, EXCEED_CONN_LIMIT = 403, 514, 1000040350
PART_SECONDS = 5  # how long the parts of a split event wait for the rest
HTTP_TIMEOUT = 20.0


class Refused(Exception):
    """Feishu will not let this app connect (wrong id or secret, too many connections): trying again does not help."""

    def __init__(self, code, message: str):
        super().__init__(f"Feishu refused the long connection ({code}): {message}")
        self.code = code
        self.message = message


# ------------------------------------------------------------------ the frame (protobuf, by hand)


def _varint(value: int) -> bytes:
    value &= (1 << 64) - 1  # negative int32 values go out as ten bytes, as protobuf does
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value, shift = 0, 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ValueError("a broken frame (varint)")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _fields(data: bytes):
    """(field number, wire type, value) of each field; varints as ints, length-delimited as bytes."""
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 2:
            size, pos = _read_varint(data, pos)
            if pos + size > len(data):
                raise ValueError("a broken frame (length)")
            value, pos = data[pos : pos + size], pos + size
        elif wire == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == 5:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ValueError(f"a broken frame (wire type {wire})")
        yield number, wire, value


def _signed32(value: int) -> int:
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= 1 << 63 else value


def _bytes_field(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _int_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


@dataclass
class Frame:
    """pbbp2.Frame. The optional fields stay None when the frame did not carry them."""

    seq_id: int = 0
    log_id: int = 0
    service: int = 0
    method: int = 0
    headers: list[tuple[str, str]] = field(default_factory=list)
    payload_encoding: str | None = None
    payload_type: str | None = None
    payload: bytes | None = None
    log_id_new: str | None = None

    def header(self, key: str) -> str | None:
        return next((value for name, value in self.headers if name == key), None)

    def encode(self) -> bytes:
        out = [_int_field(1, self.seq_id), _int_field(2, self.log_id), _int_field(3, self.service), _int_field(4, self.method)]
        for key, value in self.headers:
            out.append(_bytes_field(5, _bytes_field(1, key.encode()) + _bytes_field(2, value.encode())))
        if self.payload_encoding is not None:
            out.append(_bytes_field(6, self.payload_encoding.encode()))
        if self.payload_type is not None:
            out.append(_bytes_field(7, self.payload_type.encode()))
        if self.payload is not None:
            out.append(_bytes_field(8, self.payload))
        if self.log_id_new is not None:
            out.append(_bytes_field(9, self.log_id_new.encode()))
        return b"".join(out)

    @classmethod
    def decode(cls, data: bytes) -> Frame:
        frame = cls()
        for number, wire, value in _fields(data):
            if number in (1, 2, 3, 4) and wire == 0:
                if number == 1:
                    frame.seq_id = value
                elif number == 2:
                    frame.log_id = value
                elif number == 3:
                    frame.service = _signed32(value)
                else:
                    frame.method = _signed32(value)
            elif number == 5 and wire == 2:
                key = text = ""
                for inner, inner_wire, item in _fields(value):
                    if inner == 1 and inner_wire == 2:
                        key = item.decode("utf-8")
                    elif inner == 2 and inner_wire == 2:
                        text = item.decode("utf-8")
                frame.headers.append((key, text))
            elif number == 6 and wire == 2:
                frame.payload_encoding = value.decode("utf-8")
            elif number == 7 and wire == 2:
                frame.payload_type = value.decode("utf-8")
            elif number == 8 and wire == 2:
                frame.payload = bytes(value)
            elif number == 9 and wire == 2:
                frame.log_id_new = value.decode("utf-8")
        return frame


def ping_frame(service_id: int) -> Frame:
    return Frame(service=service_id, method=CONTROL, headers=[("type", "ping")])


# ------------------------------------------------------------------ the connection


def _handshake_error(exc: Exception) -> Exception:
    """What a refused WebSocket handshake means: Refused when retrying cannot help, else the error itself."""
    headers = getattr(exc, "headers", None)
    if headers is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return exc
    code, message = headers.get("handshake-status"), headers.get("handshake-msg")
    if code is None or message is None:
        return exc
    code = int(code)
    if code == AUTH_FAILED and int(headers.get("handshake-autherrcode") or 0) == EXCEED_CONN_LIMIT:
        return Refused(code, message)
    if code == FORBIDDEN:
        return Refused(code, message)
    return ConnectionError(f"Feishu refused the handshake ({code}): {message}")


def _connect_kwargs() -> dict:
    # websockets 15 looks for a proxy in the environment by default; the SDK connected directly.
    return {"proxy": None} if "proxy" in inspect.signature(websockets.connect).parameters else {}


class LongConnection:
    """Opens the long connection, keeps it up and hands each event (the parsed JSON) to `on_event`."""

    def __init__(self, app_id: str, app_secret: str, domain: str, on_event: Callable[[dict], None], http=None, clock=time.time):
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain.rstrip("/")
        self._on_event = on_event
        self._http = http
        self._clock = clock
        self._conn = None  # the open WebSocket; None while the connection is down
        self._conn_id = ""
        self._service_id = 0
        self._parts: dict[str, tuple[float, list[bytes]]] = {}
        self._send_lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closing = False
        # The SDK's defaults; the endpoint and the pongs replace them.
        self.reconnect_count = -1
        self.reconnect_interval = 120.0
        self.reconnect_nonce = 30.0
        self.ping_interval = 120.0

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def run_forever(self) -> None:
        """Blocks: connects, and connects again whenever the connection drops. Raises Refused."""
        asyncio.run(self._run())

    def close(self) -> None:
        """Ends run_forever (from any thread)."""
        self._closing = True
        loop, conn = self._loop, self._conn
        if loop is not None and conn is not None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(conn.close()))

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._send_lock = asyncio.Lock()
        pinger = asyncio.create_task(self._ping_loop())
        tries = 0  # attempts since the connection was last up
        try:
            while not self._closing:
                try:
                    await self._session()
                    tries = 0
                except Refused as exc:
                    log.error("feishu: %s", exc)
                    raise
                except Exception as exc:  # noqa: BLE001  any failure to connect: try again, as the SDK does
                    log.error("feishu: could not open the long connection: %s", exc)
                    tries += 1
                if self._closing:
                    break
                if 0 <= self.reconnect_count < tries:
                    raise ConnectionError(f"could not reach Feishu after {tries} tries")
                wait = random.random() * self.reconnect_nonce if tries <= 1 else self.reconnect_interval
                log.info("feishu: connecting again in %.0f s", wait)
                await asyncio.sleep(wait)
        finally:
            pinger.cancel()

    async def _session(self) -> None:
        """One connection, from opening it until it closes."""
        url = await asyncio.to_thread(self._endpoint)
        query = parse_qs(urlparse(url).query)
        conn_id = (query.get("device_id") or [""])[0]
        service_id = int((query.get("service_id") or ["0"])[0] or 0)
        try:
            conn = await websockets.connect(url, **_connect_kwargs())
        except InvalidHandshake as exc:
            raise _handshake_error(exc) from exc
        self._conn, self._conn_id, self._service_id = conn, conn_id, service_id
        log.info("feishu: connected to the long connection [conn_id=%s]", conn_id)
        try:
            await self._send(ping_frame(service_id).encode())  # the SDK pinged as soon as it was connected
            async for message in conn:
                if isinstance(message, bytes):
                    await self._handle(message)
            log.warning("feishu: the long connection closed [conn_id=%s]", conn_id)
        except ConnectionClosed as exc:
            log.warning("feishu: the long connection dropped: %s [conn_id=%s]", exc, conn_id)
        finally:
            self._conn = None
            try:
                await conn.close()
            except Exception:  # noqa: BLE001  it is going anyway
                pass

    def _endpoint(self) -> str:
        """The wss:// address for a new connection; applies the settings that come with it."""
        http = self._http or httpx.Client(timeout=HTTP_TIMEOUT)
        try:
            response = http.post(
                self._domain + ENDPOINT_PATH, json={"AppID": self._app_id, "AppSecret": self._app_secret}, headers={"locale": "zh"}
            )
        finally:
            if self._http is None:
                http.close()
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200:
            raise ConnectionError(f"Feishu's endpoint answered {response.status_code}: {body.get('msg') or 'system busy'}")
        code, message = body.get("code"), body.get("msg") or ""
        if code in (SYSTEM_BUSY, INTERNAL_ERROR):
            raise ConnectionError(f"Feishu is busy ({code}): {message}")
        if code != OK:
            raise Refused(code, message)
        data = body.get("data") or {}
        if data.get("ClientConfig"):
            self._configure(data["ClientConfig"])
        if not data.get("URL"):
            raise ConnectionError("Feishu's endpoint gave no address")
        return str(data["URL"])

    def _configure(self, config: dict) -> None:
        for key, name in (("ReconnectCount", "reconnect_count"), ("ReconnectInterval", "reconnect_interval"),
                          ("ReconnectNonce", "reconnect_nonce"), ("PingInterval", "ping_interval")):  # fmt: skip
            value = config.get(key)
            if isinstance(value, (int, float)):
                setattr(self, name, value)

    async def _ping_loop(self) -> None:
        while True:
            if self._conn is not None:
                try:
                    await self._send(ping_frame(self._service_id).encode())
                except Exception as exc:  # noqa: BLE001  the receive loop notices a dead connection
                    log.warning("feishu: ping failed: %s", exc)
            await asyncio.sleep(max(1.0, float(self.ping_interval)))

    async def _send(self, data: bytes) -> None:
        async with self._send_lock:
            conn = self._conn
            if conn is None:
                raise ConnectionError("the long connection is closed")
            await conn.send(data)

    async def _handle(self, raw: bytes) -> None:
        try:
            frame = Frame.decode(raw)
            if frame.method == CONTROL:
                if frame.header("type") == "pong" and frame.payload:
                    self._configure(json.loads(frame.payload.decode("utf-8")))
                return
            if frame.method == DATA:
                await self._data(frame)
        except Exception as exc:  # noqa: BLE001  one bad frame must not end the connection
            log.error("feishu: could not handle a frame: %s", exc)

    async def _data(self, frame: Frame) -> None:
        payload = frame.payload or b""
        total = int(frame.header("sum") or 1)
        if total > 1:
            payload = self._combine(frame.header("message_id") or "", total, int(frame.header("seq") or 0), payload)
            if payload is None:
                return
        if frame.header("type") != "event":
            return  # card callbacks: the SDK did not answer them either
        code, started = 200, self._clock()
        try:
            self._on_event(json.loads(payload.decode("utf-8")))
            frame.headers.append(("biz_rt", str(int((self._clock() - started) * 1000))))
        except Exception:  # noqa: BLE001
            log.exception("feishu: could not handle an event [message_id=%s]", frame.header("message_id"))
            code = 500
        frame.payload = json.dumps({"code": code}).encode("utf-8")
        await self._send(frame.encode())

    def _combine(self, message_id: str, total: int, seq: int, part: bytes) -> bytes | None:
        now = self._clock()
        self._parts = {key: entry for key, entry in self._parts.items() if entry[0] > now}
        _, parts = self._parts.get(message_id, (0.0, [b""] * total))
        if len(parts) != total or not 0 <= seq < total:
            return None
        parts[seq] = part
        if all(parts):
            self._parts.pop(message_id, None)
            return b"".join(parts)
        self._parts[message_id] = (now + PART_SECONDS, parts)
        return None
