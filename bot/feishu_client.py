"""Feishu / Lark: sending messages and receiving them over the long connection.

The long connection (WebSocket) is opened from this machine outwards, so no
inbound port, public IP or domain name is needed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageReactionRequest,
    Emoji,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

log = logging.getLogger(__name__)


class FeishuError(Exception):
    def __init__(self, code, message: str):
        super().__init__(f"Feishu error {code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str  # "p2p" (direct chat) or "group"
    sender_open_id: str
    text: str
    is_text: bool = True
    mentioned_open_ids: tuple[str, ...] = ()
    mentioned_names: tuple[str, ...] = ()  # the same people's names, in the same order

    @property
    def is_private(self) -> bool:
        return self.chat_type == "p2p"


def markdown_card(title: str, markdown: str, template: str = "blue", footer: str | None = None) -> dict:
    """A card in Feishu's card JSON 2.0 format with one markdown body. Without a title it has no header bar."""
    elements: list[dict] = [{"tag": "markdown", "content": markdown}]
    if footer:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": footer, "text_size": "notation"})
    card: dict = {"schema": "2.0", "config": {}, "body": {"elements": elements}}
    if title:
        card["header"] = {"template": template, "title": {"tag": "plain_text", "content": title}}
    return card


def parse_message(data: P2ImMessageReceiveV1) -> IncomingMessage | None:
    """Turn the SDK's event object into our own small message type."""
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    if message is None or sender is None:
        return None
    if getattr(sender, "sender_type", "user") != "user":
        return None  # ignore other bots and system messages

    mentions = message.mentions or []
    text = ""
    is_text = message.message_type == "text"
    if is_text:
        try:
            text = json.loads(message.content or "{}").get("text", "")
        except (ValueError, AttributeError):
            text = ""
        for mention in mentions:
            if mention.key:
                text = text.replace(mention.key, " ")
        text = " ".join(text.split())

    return IncomingMessage(
        message_id=message.message_id or "",
        chat_id=message.chat_id or "",
        chat_type=message.chat_type or "",
        sender_open_id=(sender.sender_id.open_id if sender.sender_id else "") or "",
        text=text,
        is_text=is_text,
        mentioned_open_ids=tuple(m.id.open_id for m in mentions if m.id and m.id.open_id),
        mentioned_names=tuple((getattr(m, "name", "") or "") for m in mentions if m.id and m.id.open_id),
    )


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, domain: str = lark.FEISHU_DOMAIN):
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        self._ws = None  # the long connection's client, once listen() has made it
        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    # --------------------------------------------------------------- sending

    def _send(self, receive_id: str, receive_id_type: str, msg_type: str, content: dict) -> None:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise FeishuError(response.code, response.msg)

    def _reply(self, message_id: str, msg_type: str, content: dict) -> None:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        if not response.success():
            raise FeishuError(response.code, response.msg)

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> None:
        self._send(receive_id, receive_id_type, "text", {"text": text})

    def reply_text(self, message_id: str, text: str) -> None:
        self._reply(message_id, "text", {"text": text})

    def send_markdown(
        self,
        receive_id: str,
        title: str,
        markdown: str,
        receive_id_type: str = "chat_id",
        template: str = "blue",
        footer: str | None = None,
    ) -> None:
        """Send a card; if Feishu rejects the card, fall back to plain text."""
        try:
            self._send(receive_id, receive_id_type, "interactive", markdown_card(title, markdown, template, footer))
        except FeishuError as exc:
            log.warning("card rejected (%s), sending plain text instead", exc)
            self.send_text(receive_id, _as_plain_text(title, markdown, footer), receive_id_type)

    def reply_markdown(self, message_id: str, title: str, markdown: str, template: str = "blue", footer: str | None = None) -> None:
        try:
            self._reply(message_id, "interactive", markdown_card(title, markdown, template, footer))
        except FeishuError as exc:
            log.warning("card rejected (%s), replying with plain text instead", exc)
            self.reply_text(message_id, _as_plain_text(title, markdown, footer))

    # ------------------------------------------------------------- reactions

    def add_reaction(self, message_id: str, emoji: str) -> str:
        """Puts an emoji on a message, the way a person does to say "seen". Returns the id it is taken off by.

        Needs no permission beyond im:message. Feishu may still refuse, for
        example in a group shared with another organisation.
        """
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder().reaction_type(Emoji.builder().emoji_type(emoji).build()).build()
            )
            .build()
        )
        response = self._client.im.v1.message_reaction.create(request)
        if not response.success():
            raise FeishuError(response.code, response.msg)
        return getattr(response.data, "reaction_id", None) or ""

    def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        request = DeleteMessageReactionRequest.builder().message_id(message_id).reaction_id(reaction_id).build()
        response = self._client.im.v1.message_reaction.delete(request)
        if not response.success():
            raise FeishuError(response.code, response.msg)

    # ------------------------------------------------------------- bot info

    def chat_members(self, chat_id: str) -> list[tuple[str, str]]:
        """(open_id, name) of the people in a group. Needs the app's permission to read group members
        (im:chat.members:read or im:chat:readonly); without it Feishu refuses and FeishuError is raised."""
        members: list[tuple[str, str]] = []
        page_token = ""
        for _ in range(5):  # 100 a page; a trading group is far smaller
            uri = f"/open-apis/im/v1/chats/{chat_id}/members?member_id_type=open_id&page_size=100"
            if page_token:
                uri += f"&page_token={page_token}"
            request = lark.BaseRequest.builder().http_method(lark.HttpMethod.GET).uri(uri).token_types({lark.AccessTokenType.TENANT}).build()
            response = self._client.request(request)
            if not response.success():
                raise FeishuError(response.code, response.msg)
            try:
                data = json.loads(response.raw.content).get("data") or {}
            except (ValueError, AttributeError, TypeError):
                break
            for item in data.get("items") or []:
                if item.get("member_id"):
                    members.append((str(item["member_id"]), str(item.get("name") or "")))
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                break
        return members

    def bot_open_id(self) -> str | None:
        """The bot's own open_id, used to tell whether a group message @-mentions it."""
        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        response = self._client.request(request)
        if not response.success():
            raise FeishuError(response.code, response.msg)
        try:
            payload = json.loads(response.raw.content)
        except (ValueError, AttributeError, TypeError):
            return None
        return (payload.get("bot") or {}).get("open_id")

    # ------------------------------------------------------------- receiving

    def listen(self, on_message: Callable[[IncomingMessage], None]) -> None:
        """Open the long connection and block forever. Call from the main thread."""

        def _on_receive(data: P2ImMessageReceiveV1) -> None:
            try:
                message = parse_message(data)
                if message is not None:
                    on_message(message)
            except Exception:
                log.exception("failed to handle an incoming Feishu message")

        handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(_on_receive).build()
        ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            domain=self._domain,
            log_level=lark.LogLevel.INFO,
        )
        self._ws = ws_client
        ws_client.start()

    def connected(self) -> bool:
        """Whether the long connection to Feishu is up now. False before listen() has opened it."""
        return getattr(self._ws, "_conn", None) is not None  # lark keeps the open socket there, None while it is down


def _as_plain_text(title: str, markdown: str, footer: str | None) -> str:
    text = markdown.replace("**", "")
    if title:
        text = f"【{title}】\n{text}"
    if footer:
        text += f"\n\n{footer}"
    return text
