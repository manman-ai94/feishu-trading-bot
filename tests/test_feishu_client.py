from __future__ import annotations

import json
from types import SimpleNamespace

from bot.feishu_client import FeishuClient, FeishuError, markdown_card, parse_message


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


class ReactionApi:
    def __init__(self, ok=True):
        self.ok = ok
        self.requests = []

    def _answer(self, request, data=None):
        self.requests.append(request)
        return SimpleNamespace(success=lambda: self.ok, code=0 if self.ok else 231021, msg="no permission", data=data)

    def create(self, request):
        return self._answer(request, SimpleNamespace(reaction_id="reaction_1"))

    def delete(self, request):
        return self._answer(request)


def reacting_client(ok=True):
    client = FeishuClient("cli_test", "secret")
    api = ReactionApi(ok)
    client._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_reaction=api)))
    return client, api


def test_an_emoji_is_put_on_a_message_and_taken_off_again():
    client, api = reacting_client()
    assert client.add_reaction("om_1", "Typing") == "reaction_1"
    client.remove_reaction("om_1", "reaction_1")
    added, removed = api.requests
    assert added.message_id == "om_1" and added.request_body.reaction_type.emoji_type == "Typing"
    assert added.http_method.name == "POST" and added.uri == "/open-apis/im/v1/messages/:message_id/reactions"
    assert (removed.message_id, removed.reaction_id) == ("om_1", "reaction_1") and removed.http_method.name == "DELETE"


def test_a_refused_emoji_is_an_error_the_caller_can_catch():
    client, _ = reacting_client(ok=False)
    for call in (lambda: client.add_reaction("om_1", "Typing"), lambda: client.remove_reaction("om_1", "reaction_1")):
        try:
            call()
        except FeishuError as exc:
            assert exc.code == 231021
        else:
            raise AssertionError("expected a FeishuError")


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
