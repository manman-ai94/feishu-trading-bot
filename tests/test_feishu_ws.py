"""Feishu's long connection without the SDK: the frame format, and a real WebSocket on this machine."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.server import serve

from bot.feishu_ws import CONTROL, DATA, Frame, LongConnection, Refused, _handshake_error, ping_frame


def test_a_frame_comes_back_as_it_went():
    frame = Frame(
        seq_id=2**40, log_id=7, service=-3, method=DATA, headers=[("type", "event"), ("message_id", "m1"), ("中文", "值")],
        payload_type="json", payload=b'{"a": 1}', log_id_new="x1",
    )  # fmt: skip
    again = Frame.decode(frame.encode())
    assert again == frame
    assert Frame.decode(Frame(method=CONTROL).encode()) == Frame(method=CONTROL)  # the optional fields stay absent
    assert ping_frame(7) == Frame(service=7, method=CONTROL, headers=[("type", "ping")])


def test_frames_match_the_sdk_protobuf_both_ways():
    pb = pytest.importorskip("lark_oapi.ws.pb.pbbp2_pb2")
    theirs = pb.Frame(SeqID=5, LogID=9, service=7, method=1, payload=b"{}", LogIDNew="n")
    for key, value in (("type", "event"), ("sum", "1")):
        header = theirs.headers.add()
        header.key, header.value = key, value
    ours = Frame.decode(theirs.SerializeToString())
    assert (ours.seq_id, ours.log_id, ours.service, ours.method, ours.payload, ours.log_id_new) == (5, 9, 7, 1, b"{}", "n")
    assert ours.headers == [("type", "event"), ("sum", "1")] and ours.payload_type is None
    ours.headers.append(("biz_rt", "3"))
    back = pb.Frame()
    back.ParseFromString(ours.encode())
    assert [(h.key, h.value) for h in back.headers] == [("type", "event"), ("sum", "1"), ("biz_rt", "3")]
    assert back.SerializeToString() == ours.encode()  # byte for byte what the SDK would send
    assert ping_frame(7).encode() == _sdk_ping(pb, 7)


def _sdk_ping(pb, service):
    frame = pb.Frame()
    header = frame.headers.add()
    header.key, header.value = "type", "ping"
    frame.service, frame.method, frame.SeqID, frame.LogID = service, 0, 0, 0
    return frame.SerializeToString()


def endpoint(port: int, code: int = 0, config=None):
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/callback/ws/endpoint"
        assert json.loads(request.content) == {"AppID": "cli_test", "AppSecret": "secret"}
        url = f"ws://127.0.0.1:{port}/ws?device_id=d1&service_id=7"
        settings = config or {"ReconnectCount": -1, "ReconnectInterval": 1, "ReconnectNonce": 0, "PingInterval": 1}
        return httpx.Response(200, json={"code": code, "msg": "ok" if not code else "app not found", "data": {"URL": url, "ClientConfig": settings}})

    return httpx.Client(transport=httpx.MockTransport(answer))


def event_frame(payload: bytes, seq: int = 0, total: int = 1, kind: str = "event") -> bytes:
    headers = [("type", kind), ("message_id", "m1"), ("sum", str(total)), ("seq", str(seq)), ("trace_id", "t1")]
    return Frame(seq_id=5, log_id=9, service=7, method=DATA, headers=headers, payload=payload, log_id_new="x").encode()


def test_events_are_answered_pings_go_out_settings_follow_the_pong_and_a_drop_reconnects():
    async def scenario():
        seen, connections, received = [], [], asyncio.Queue()

        def on_event(payload):
            if payload.get("boom"):
                raise ValueError("a handler that fails")
            seen.append(payload)

        async def handler(ws):
            connections.append(ws)
            async for message in ws:
                await received.put((len(connections), Frame.decode(message)))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            conn = LongConnection("cli_test", "secret", "http://feishu.test", on_event, http=endpoint(port))
            runner = asyncio.create_task(conn._run())

            async def next_frame(kind):
                while True:
                    number, frame = await asyncio.wait_for(received.get(), 10)
                    if frame.header("type") == kind or (kind == "answer" and frame.method == DATA):
                        return number, frame

            _, ping = await next_frame("ping")
            assert (ping.service, ping.method) == (7, CONTROL) and conn.connected
            ws = connections[0]
            body = json.dumps({"header": {"event_type": "im.message.receive_v1"}, "event": {"text": "仓位"}}, ensure_ascii=False).encode()
            await ws.send(event_frame(body[:10], seq=0, total=2))  # Feishu splits a long event in parts
            await ws.send(event_frame(body[10:], seq=1, total=2))
            _, answer = await next_frame("answer")
            assert seen == [json.loads(body)]
            assert json.loads(answer.payload) == {"code": 200} and answer.header("biz_rt") is not None
            assert (answer.seq_id, answer.log_id, answer.log_id_new, answer.header("trace_id")) == (5, 9, "x", "t1")

            await ws.send(event_frame(b'{"boom": true}'))
            _, failed = await next_frame("answer")
            assert json.loads(failed.payload) == {"code": 500}

            await ws.send(event_frame(b"{}", kind="card"))  # not answered, as the SDK did not
            pong = Frame(service=7, method=CONTROL, headers=[("type", "pong")], payload=b'{"PingInterval": 1, "ReconnectInterval": 2}')
            await ws.send(pong.encode())
            await asyncio.sleep(0.2)
            assert conn.reconnect_interval == 2

            await ws.close()  # Feishu drops the connection: the bot connects again by itself
            number, _ = await next_frame("ping")
            assert number == 2 and len(connections) == 2 and conn.connected

            conn.close()
            await asyncio.wait_for(runner, 10)
            assert not conn.connected

    asyncio.run(scenario())


def test_a_refused_app_stops_and_a_busy_one_is_tried_again():
    conn = LongConnection("cli_test", "secret", "http://feishu.test", lambda payload: None, http=endpoint(1, code=10003))
    with pytest.raises(Refused, match="app not found"):
        asyncio.run(conn._run())

    calls = []

    def busy(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 1, "msg": "system busy"})

    conn = LongConnection("cli_test", "secret", "http://feishu.test", lambda payload: None, http=httpx.Client(transport=httpx.MockTransport(busy)))
    conn.reconnect_count, conn.reconnect_nonce, conn.reconnect_interval = 1, 0, 0.01
    with pytest.raises(ConnectionError, match="after 2 tries"):
        asyncio.run(conn._run())
    assert len(calls) == 2


def test_a_refused_handshake_says_whether_trying_again_can_help():
    def refused(status, message, auth=None):
        headers = {"handshake-status": str(status), "handshake-msg": message}
        if auth:
            headers["handshake-autherrcode"] = str(auth)
        return SimpleNamespace(response=SimpleNamespace(headers=headers))

    assert isinstance(_handshake_error(refused(514, "too many", 1000040350)), Refused)
    assert isinstance(_handshake_error(refused(403, "forbidden")), Refused)
    assert isinstance(_handshake_error(refused(514, "expired ticket", 1)), ConnectionError)
    assert isinstance(_handshake_error(refused(500, "busy")), ConnectionError)
    plain = ValueError("no headers")
    assert _handshake_error(plain) is plain
