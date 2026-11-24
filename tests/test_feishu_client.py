from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from bot.feishu_client import FeishuClient, FeishuError, fields, markdown_card, parse_message


def event(text='{"text":"@_user_1 现在黄金  怎么看"}', message_type="text", chat_type="group", sender_type="user", mentions="default"):
    if mentions == "default":
        mentions = [SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id="ou_bot"), name="交易助手")]
    message = SimpleNamespace(
        message_id="om_1", chat_id="oc_group", chat_type=chat_type, message_type=message_type, content=text, mentions=mentions
    )
    sender = SimpleNamespace(sender_type=sender_type, sender_id=SimpleNamespace(open_id="ou_friend"))
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def test_card_uses_schema_2_with_a_markdown_body():
    card = markdown_card("盘前简报", "**多头观点**\n- 站稳 2650", "indigo", footer="仅供参考")
    assert card["schema"] == "2.0"
    assert card["header"] == {"template": "indigo", "title": {"tag": "plain_text", "content": "盘前简报"}}
    assert card["body"]["elements"][0] == {"tag": "markdown", "content": "**多头观点**\n- 站稳 2650"}
    assert [element["tag"] for element in card["body"]["elements"]] == ["markdown", "hr", "markdown"]
    assert len(markdown_card("t", "m")["body"]["elements"]) == 1
    json.dumps(card)  # must be serialisable


def test_group_message_is_cleaned_of_the_mention_placeholder():
    message = parse_message(event())
    assert message.text == "现在黄金 怎么看"
    assert message.mentioned_open_ids == ("ou_bot",)
    assert (message.chat_id, message.sender_open_id, message.is_private) == ("oc_group", "ou_friend", False)


def test_private_message_without_mentions():
    message = parse_message(event('{"text":"仓位"}', chat_type="p2p", mentions=None))
    assert message.text == "仓位" and message.is_private and message.mentioned_open_ids == ()


def test_non_text_and_broken_content():
    message = parse_message(event('{"image_key":"img"}', message_type="image", mentions=None))
    assert not message.is_text and message.text == ""
    assert parse_message(event("not json", mentions=None)).text == ""


def test_messages_from_bots_and_empty_events_are_ignored():
    assert parse_message(event(sender_type="app")) is None
    assert parse_message(SimpleNamespace(event=None)) is None


class RecordingClient(FeishuClient):
    """Replaces the two network calls; everything above them is the real code."""

    def __init__(self, reject_cards=False):
        super().__init__("cli_test", "secret")
        self.reject_cards = reject_cards
        self.calls = []

    def _send(self, receive_id, receive_id_type, msg_type, content):
        if msg_type == "interactive" and self.reject_cards:
            raise FeishuError(230099, "card content invalid")
        self.calls.append(("send", receive_id, receive_id_type, msg_type, content))

    def _reply(self, message_id, msg_type, content):
        if msg_type == "interactive" and self.reject_cards:
            raise FeishuError(230099, "card content invalid")
        self.calls.append(("reply", message_id, msg_type, content))


def test_markdown_is_sent_as_a_card():
    client = RecordingClient()
    client.send_markdown("ou_owner", "标题", "**内容**", "open_id")
    [(kind, target, id_type, msg_type, content)] = client.calls
    assert (kind, target, id_type, msg_type) == ("send", "ou_owner", "open_id", "interactive")
    assert content["schema"] == "2.0"


def test_rejected_card_falls_back_to_plain_text():
    client = RecordingClient(reject_cards=True)
    client.send_markdown("oc_group", "行情分析", "**多头观点**\n- 站稳 2650", footer="仅供参考")
    client.reply_markdown("om_1", "行情分析", "**空头观点**")
    assert client.calls[0] == ("send", "oc_group", "chat_id", "text", {"text": "【行情分析】\n多头观点\n- 站稳 2650\n\n仅供参考"})
    assert client.calls[1] == ("reply", "om_1", "text", {"text": "【行情分析】\n空头观点"})


def test_a_card_without_a_title_has_no_header_bar():
    card = markdown_card("", "今晚 21:30 公布美国 CPI。", footer="仅供参考")
    assert "header" not in card and card["schema"] == "2.0"
    assert [element["tag"] for element in card["body"]["elements"]] == ["markdown", "hr", "markdown"]

    client = RecordingClient(reject_cards=True)
    client.reply_markdown("om_1", "", "今晚 **21:30** 公布。", footer="仅供参考")
    assert client.calls == [("reply", "om_1", "text", {"text": "今晚 21:30 公布。\n\n仅供参考"})]


class FakeFeishu:
    """Feishu's HTTP API as far as the bot uses it, behind httpx's mock transport."""

    def __init__(self, fail_code=0):
        self.fail_code = fail_code
        self.requests = []
        self.tokens = 0
        self.refuse_token_once = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.url.params), body, request.headers.get("authorization")))
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            self.tokens += 1
            return httpx.Response(200, json={"code": 0, "msg": "ok", "tenant_access_token": f"t-{self.tokens}", "expire": 7200})
        if self.refuse_token_once:
            self.refuse_token_once = False
            return httpx.Response(400, json={"code": 99991663, "msg": "Invalid access token for authorization."})
        if self.fail_code:
            return httpx.Response(400, json={"code": self.fail_code, "msg": "no permission"})
        if request.url.path.endswith("/reactions"):
            return httpx.Response(200, json={"code": 0, "msg": "success", "data": {"reaction_id": "reaction_1"}})
        if request.url.path == "/open-apis/bot/v3/info":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "bot": {"open_id": "ou_bot", "app_name": "交易助手"}})
        if request.url.path.endswith("/members"):
            more = "page_token" not in request.url.params
            items = [{"member_id": "ou_a", "name": "阿强"}] if more else [{"member_id": "ou_b", "name": "小明"}]
            return httpx.Response(200, json={"code": 0, "data": {"items": items, "has_more": more, "page_token": "p2" if more else ""}})
        return httpx.Response(200, json={"code": 0, "msg": "success", "data": {"message_id": "om_new"}})


def api_client(fail_code=0, clock=None):
    fake = FakeFeishu(fail_code)
    http = httpx.Client(transport=httpx.MockTransport(fake))
    client = FeishuClient("cli_test", "secret", http=http, **({"clock": clock} if clock else {}))
    return client, fake


def test_an_emoji_is_put_on_a_message_and_taken_off_again():
    client, fake = api_client()
    assert client.add_reaction("om_1", "Typing") == "reaction_1"
    client.remove_reaction("om_1", "reaction_1")
    token, added, removed = fake.requests
    assert token[:2] == ("POST", "/open-apis/auth/v3/tenant_access_token/internal")
    assert token[3] == {"app_id": "cli_test", "app_secret": "secret"}
    assert added[:2] == ("POST", "/open-apis/im/v1/messages/om_1/reactions")
    assert added[3] == {"reaction_type": {"emoji_type": "Typing"}} and added[4] == "Bearer t-1"
    assert removed[:2] == ("DELETE", "/open-apis/im/v1/messages/om_1/reactions/reaction_1") and removed[4] == "Bearer t-1"


def test_a_refused_emoji_is_an_error_the_caller_can_catch():
    client, _ = api_client(fail_code=231021)
    for call in (lambda: client.add_reaction("om_1", "Typing"), lambda: client.remove_reaction("om_1", "reaction_1")):
        try:
            call()
        except FeishuError as exc:
            assert exc.code == 231021 and exc.message == "no permission"
        else:
            raise AssertionError("expected a FeishuError")


def test_messages_are_sent_and_replied_to_as_feishu_expects():
    client, fake = api_client()
    client.send_text("oc_group", "你好")
    client.send_markdown("ou_owner", "标题", "**内容**", "open_id")
    client.reply_text("om_1", "收到")
    _, sent, card, reply = fake.requests
    assert sent[:3] == ("POST", "/open-apis/im/v1/messages", {"receive_id_type": "chat_id"})
    assert sent[3] == {"receive_id": "oc_group", "msg_type": "text", "content": '{"text": "你好"}'}
    assert card[2] == {"receive_id_type": "open_id"} and card[3]["msg_type"] == "interactive"
    assert json.loads(card[3]["content"])["schema"] == "2.0"
    assert reply[:2] == ("POST", "/open-apis/im/v1/messages/om_1/reply") and reply[3] == {"msg_type": "text", "content": '{"text": "收到"}'}


def test_the_token_is_kept_until_it_runs_out_and_fetched_again_when_feishu_refuses_it():
    now = [1_800_000_000.0]
    client, fake = api_client(clock=lambda: now[0])
    client.send_text("oc_group", "1")
    client.send_text("oc_group", "2")
    assert fake.tokens == 1
    now[0] += 7200 - 300  # five minutes before it runs out: a new one
    client.send_text("oc_group", "3")
    assert fake.tokens == 2 and fake.requests[-1][4] == "Bearer t-2"
    fake.refuse_token_once = True
    client.send_text("oc_group", "4")  # refused once, sent again with a new token
    assert fake.tokens == 3 and fake.requests[-1][4] == "Bearer t-3" and fake.requests[-1][3]["content"] == '{"text": "4"}'


def test_group_members_come_page_by_page_and_the_bot_knows_its_own_id():
    client, fake = api_client()
    assert client.chat_members("oc_group") == [("ou_a", "阿强"), ("ou_b", "小明")]
    assert fake.requests[1][2] == {"member_id_type": "open_id", "page_size": "100"}
    assert fake.requests[2][2]["page_token"] == "p2"
    assert client.bot_open_id() == "ou_bot"


def test_no_network_is_a_feishu_error():
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    client = FeishuClient("cli_test", "secret", http=httpx.Client(transport=httpx.MockTransport(down)))
    try:
        client.send_text("oc_group", "你好")
    except FeishuError as exc:
        assert exc.code == "network"
    else:
        raise AssertionError("expected a FeishuError")


def test_an_event_from_the_long_connection_reads_like_the_sdk_object():
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_friend"}, "sender_type": "user"},
            "message": {
                "message_id": "om_9", "chat_id": "oc_group", "chat_type": "group", "message_type": "text",
                "content": '{"text":"@_user_1 仓位"}', "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "交易助手"}],
            },
        },
    }  # fmt: skip
    message = parse_message(fields(payload))
    assert (message.message_id, message.text, message.sender_open_id) == ("om_9", "仓位", "ou_friend")
    assert message.mentioned_open_ids == ("ou_bot",) and message.mentioned_names == ("交易助手",)
    del payload["event"]["message"]["mentions"]
    assert parse_message(fields(payload)).mentioned_open_ids == ()


def test_the_connection_is_down_until_listen_has_opened_it():
    from bot.feishu_client import FeishuClient

    client = FeishuClient("cli_x", "secret")
    assert client.connected() is False

    class Ws:
        _conn = None

    client._ws = Ws()
    assert client.connected() is False
    client._ws._conn = object()
    assert client.connected() is True


def test_the_people_mentioned_come_with_their_names():
    mentions = [
        SimpleNamespace(key="@_user_1", id=SimpleNamespace(open_id="ou_bot"), name="交易助手"),
        SimpleNamespace(key="@_user_2", id=SimpleNamespace(open_id="ou_buddy"), name="阿强"),
    ]
    message = parse_message(event('{"text":"@_user_1 设为主人 @_user_2"}', mentions=mentions))
    assert message.text == "设为主人"
    assert message.mentioned_open_ids == ("ou_bot", "ou_buddy") and message.mentioned_names == ("交易助手", "阿强")


def test_listen_hands_each_message_to_the_bot_over_a_real_websocket():
    import asyncio
    import threading

    from websockets.asyncio.server import serve

    from bot.feishu_ws import DATA, Frame

    got, answers, ready = [], [], threading.Event()
    port = [0]
    loop = asyncio.new_event_loop()
    stop = None

    async def server():
        nonlocal stop
        stop = asyncio.Event()

        async def handler(ws):
            payload = {
                "schema": "2.0", "header": {"event_type": "im.message.receive_v1"},
                "event": {"sender": {"sender_id": {"open_id": "ou_owner"}, "sender_type": "user"},
                          "message": {"message_id": "om_5", "chat_id": "oc_p2p", "chat_type": "p2p", "message_type": "text",
                                      "content": '{"text":"仓位"}'}},
            }  # fmt: skip
            other = {"header": {"event_type": "im.chat.member.bot.added_v1"}, "event": {}}
            for body in (other, payload):
                frame = Frame(seq_id=1, method=DATA, headers=[("type", "event"), ("message_id", "m"), ("sum", "1"), ("seq", "0")])
                frame.payload = json.dumps(body, ensure_ascii=False).encode()
                await ws.send(frame.encode())
            async for message in ws:
                frame = Frame.decode(message)
                if frame.method == DATA:
                    answers.append(json.loads(frame.payload))
            await stop.wait()

        async with serve(handler, "127.0.0.1", 0) as running:
            port[0] = running.sockets[0].getsockname()[1]
            ready.set()
            await stop.wait()

    threading.Thread(target=lambda: loop.run_until_complete(server()), daemon=True).start()
    assert ready.wait(10)

    def feishu(request: httpx.Request) -> httpx.Response:
        url = f"ws://127.0.0.1:{port[0]}/?device_id=d&service_id=1"
        return httpx.Response(200, json={"code": 0, "data": {"URL": url, "ClientConfig": {"PingInterval": 60, "ReconnectNonce": 0}}})

    client = FeishuClient("cli_test", "secret", http=httpx.Client(transport=httpx.MockTransport(feishu)))
    listening = threading.Thread(target=client.listen, args=(got.append,), daemon=True)
    listening.start()
    for _ in range(100):
        if got and len(answers) == 2:
            break
        threading.Event().wait(0.05)
    assert client.connected()
    [message] = got  # the other event is answered but not handed on
    assert (message.message_id, message.chat_id, message.text, message.is_private) == ("om_5", "oc_p2p", "仓位", True)
    assert answers == [{"code": 200}, {"code": 200}]
    client._ws.close()
    listening.join(10)
    assert not listening.is_alive() and not client.connected()
    loop.call_soon_threadsafe(stop.set)
