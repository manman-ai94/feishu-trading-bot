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
