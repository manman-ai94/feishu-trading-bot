"""Feishu / Lark: sending messages and receiving them over the long connection.

The long connection (WebSocket) is opened from this machine outwards, so no
inbound port, public IP or domain name is needed.

The bot speaks to Feishu itself, without the official SDK (lark-oapi): the SDK loads
every one of Feishu's APIs, about 140 MB of memory on a 2 GB server, for the few
calls the bot makes. Sending is Feishu's HTTP API with the app's tenant token; the
long connection is in feishu_ws.py.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .feishu_ws import LongConnection

log = logging.getLogger(__name__)


FEISHU_DOMAIN = "https://open.feishu.cn"
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
TOKEN_MARGIN = 300  # seconds: a token is renewed this long before it runs out
TOKEN_REFUSED = {99991661, 99991663, 99991664, 99991665}  # missing, invalid or expired tenant token: fetch a new one
HTTP_TIMEOUT = 20.0
JSON_TYPE = "application/json; charset=utf-8"


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


class Fields:
    """An event's JSON read by attribute, as the SDK's event objects were: a field that is not there is None."""

    __slots__ = ("_data",)

    def __init__(self, data: dict):
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return fields(self._data.get(name))


def fields(value):
    if isinstance(value, dict):
        return Fields(value)
    if isinstance(value, list):
        return [fields(item) for item in value]
    return value


def parse_message(data) -> IncomingMessage | None:
    """Turn an im.message.receive_v1 event (read by attribute) into our own small message type."""
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
    def __init__(self, app_id: str, app_secret: str, domain: str = FEISHU_DOMAIN, http: httpx.Client | None = None, clock=time.time):
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain.rstrip("/")
        self._ws: LongConnection | None = None  # the long connection, once listen() has made it
        self._http = http
        self._clock = clock
        self._token = ""
        self._token_until = 0.0
        self._token_lock = threading.Lock()

    # ------------------------------------------------------------------ HTTP

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=HTTP_TIMEOUT)
        return self._http

    def _post_json(self, url: str, body: dict, headers: dict | None = None) -> httpx.Response:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self._client().post(url, content=data, headers={"Content-Type": JSON_TYPE, **(headers or {})})

    def _tenant_token(self, renew: bool = False) -> str:
        with self._token_lock:
            now = self._clock()
            if renew or not self._token or now >= self._token_until:
                try:
                    response = self._post_json(self._domain + TOKEN_PATH, {"app_id": self._app_id, "app_secret": self._app_secret})
                except httpx.HTTPError as exc:
                    raise FeishuError("network", f"连不上飞书：{exc}") from exc
                body = _body(response)
                if body.get("code") != 0 or not body.get("tenant_access_token"):
                    raise FeishuError(body.get("code", response.status_code), body.get("msg") or "拿不到 tenant_access_token")
                self._token = str(body["tenant_access_token"])
                self._token_until = now + max(60.0, float(body.get("expire") or 7200) - TOKEN_MARGIN)
            return self._token

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        """One API call with the tenant token. Returns the answer's JSON; FeishuError unless its code is 0."""
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._tenant_token(renew=attempt == 1)}"}
            try:
                if body is None:
                    response = self._client().request(method, self._domain + path, params=params, headers=headers)
                else:
                    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    response = self._client().request(
                        method, self._domain + path, params=params, content=data, headers={**headers, "Content-Type": JSON_TYPE}
                    )
            except httpx.HTTPError as exc:
                raise FeishuError("network", f"连不上飞书：{exc}") from exc
            answer = _body(response)
            code = answer.get("code")
            if code in TOKEN_REFUSED and attempt == 0:
                continue
            if code != 0:
                raise FeishuError(code if code is not None else response.status_code, answer.get("msg") or response.text[:200])
            return answer
        raise FeishuError("token", "飞书不认这个 tenant_access_token")  # not reached: the second attempt returns or raises

    # --------------------------------------------------------------- sending

    def _send(self, receive_id: str, receive_id_type: str, msg_type: str, content: dict) -> None:
        body = {"receive_id": receive_id, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)}
        self._call("POST", "/open-apis/im/v1/messages", params={"receive_id_type": receive_id_type}, body=body)

    def _reply(self, message_id: str, msg_type: str, content: dict) -> None:
        body = {"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)}
        self._call("POST", f"/open-apis/im/v1/messages/{message_id}/reply", body=body)

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
        answer = self._call("POST", f"/open-apis/im/v1/messages/{message_id}/reactions", body={"reaction_type": {"emoji_type": emoji}})
        return str((answer.get("data") or {}).get("reaction_id") or "")

    def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        self._call("DELETE", f"/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}")

    # ------------------------------------------------------------- bot info

    def chat_members(self, chat_id: str) -> list[tuple[str, str]]:
        """(open_id, name) of the people in a group. Needs the app's permission to read group members
        (im:chat.members:read or im:chat:readonly); without it Feishu refuses and FeishuError is raised."""
        members: list[tuple[str, str]] = []
        page_token = ""
        for _ in range(5):  # 100 a page; a trading group is far smaller
            params = {"member_id_type": "open_id", "page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._call("GET", f"/open-apis/im/v1/chats/{chat_id}/members", params=params).get("data") or {}
            for item in data.get("items") or []:
                if item.get("member_id"):
                    members.append((str(item["member_id"]), str(item.get("name") or "")))
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                break
        return members

    def bot_open_id(self) -> str | None:
        """The bot's own open_id, used to tell whether a group message @-mentions it."""
        return (self._call("GET", "/open-apis/bot/v3/info").get("bot") or {}).get("open_id")

    # ------------------------------------------------------------- receiving

    def listen(self, on_message: Callable[[IncomingMessage], None]) -> None:
        """Open the long connection and block forever. Call from the main thread."""

        def on_event(payload: dict) -> None:
            if (payload.get("header") or {}).get("event_type") != "im.message.receive_v1":
                return
            try:
                message = parse_message(fields(payload))
                if message is not None:
                    on_message(message)
            except Exception:
                log.exception("failed to handle an incoming Feishu message")

        self._ws = LongConnection(self._app_id, self._app_secret, self._domain, on_event, http=self._client())
        self._ws.run_forever()

    def connected(self) -> bool:
        """Whether the long connection to Feishu is up now. False before listen() has opened it."""
        return getattr(self._ws, "_conn", None) is not None


def _body(response: httpx.Response) -> dict:
    try:
        answer = response.json()
    except ValueError:
        return {}
    return answer if isinstance(answer, dict) else {}


def _as_plain_text(title: str, markdown: str, footer: str | None) -> str:
    text = markdown.replace("**", "")
    if title:
        text = f"【{title}】\n{text}"
    if footer:
        text += f"\n\n{footer}"
    return text
