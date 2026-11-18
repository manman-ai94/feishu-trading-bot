"""The owner's requests channel: what he wants the bot to do that it does not do yet.

Three ways in:
  * he says so: "记个诉求：跌破 3800 提醒我" (or 提个建议：…, 诉求：…);
  * the AI tells him something cannot be done yet, and ends its answer with a
    line "诉求：…", which the program takes off and records;
  * an order neither the fixed patterns nor the AI could read.

"诉求清单" lists them in Feishu. `python -m bot wishes` prints them on the
server, with the chats, for the daily look the developer takes.
"""
from __future__ import annotations

import re
from datetime import datetime, tzinfo

WISH_PREFIXES = (
    "记个诉求", "记一下诉求", "记一个诉求", "记诉求", "提个诉求", "提诉求", "记个需求", "提个需求", "记个建议",
    "提个建议", "/wish",
)  # fmt: skip
WISH_COLON_PREFIXES = ("诉求", "需求", "建议", "意见")  # only with a colon: "建议止损放哪" asks for advice
LIST_WORDS = ("诉求清单", "诉求列表", "我的诉求", "看诉求", "查诉求", "诉求", "/wishes")
LIST_DAYS = 30
LIST_LIMIT = 20
WISH_CHARS = 500
SAME_WISH_SECONDS = 86400  # the same words again within a day are not recorded twice

SOURCE_NAMES = {"owner": "你记的", "auto": "我没做到，自动记的", "unclear": "没看懂的指令"}
STATUS_NAMES = {"open": "", "done": "（已做好）", "dropped": "（不做了）"}

_AI_WISH = re.compile(r"\n?[ \t]*\**诉求\**[ \t]*[：:][ \t]*(.+?)[ \t]*\Z", re.S)


def wish_body(text: str) -> str | None:
    """The request in "记个诉求：……" (may be empty), or None when the message is not that command."""
    stripped = text.strip()
    lowered = stripped.lower()
    for prefix in WISH_PREFIXES:
        if lowered.startswith(prefix):
            return stripped[len(prefix) :].strip(" ：:，,\n\t")
    for prefix in WISH_COLON_PREFIXES:
        if stripped.startswith(prefix) and stripped[len(prefix) : len(prefix) + 1] in ("：", ":"):
            return stripped[len(prefix) + 1 :].strip(" ：:，,\n\t")
    return None


def is_list_request(text: str) -> bool:
    return text.strip(" 。.!！?？").lower() in LIST_WORDS


def take_ai_wish(answer: str) -> tuple[str, str]:
    """The AI's answer without its closing "诉求：…" line, and that wish ("" when there is none)."""
    found = _AI_WISH.search(answer)
    if found is None or "\n" in found.group(1).strip():
        return answer, ""
    return answer[: found.start()].rstrip(), found.group(1).strip()[:WISH_CHARS]


def record(journal, text: str, source: str, chat: str, now: float) -> int | None:
    """Records a wish; None when the same words were recorded within the last day."""
    text = text.strip()[:WISH_CHARS]
    if not text:
        return None
    for earlier in journal.wishes(now - SAME_WISH_SECONDS):
        if earlier["text"] == text:
            return None
    return journal.add_wish(text, source=source, chat=chat, ts=now)


def list_markdown(journal, now: float, tz: tzinfo | None, days: int = LIST_DAYS) -> str:
    wishes = journal.wishes(now - days * 86400)
    if not wishes:
        return (
            f"最近 {days} 天没有记下的诉求。想要我做到的事，发“记个诉求：……”；"
            "我做不到的事也会自动记下来，交给开发这个机器人的 Claude 每天看一次。"
        )
    shown = wishes[-LIST_LIMIT:]
    lines = [f"最近 {days} 天记下 {len(wishes)} 条" + (f"，下面是最新的 {len(shown)} 条" if len(shown) < len(wishes) else "") + "："]
    for wish in shown:
        when = datetime.fromtimestamp(wish["ts"], tz=tz).strftime("%m-%d %H:%M")
        source = SOURCE_NAMES.get(wish["source"], wish["source"])
        status = STATUS_NAMES.get(wish["status"], "")
        note = f"：{wish['note']}" if wish["note"] and status else ""
        lines.append(f"- #{wish['id']} {when} {wish['text']}（{source}）{status}{note}")
    lines.append("\n这些会交给开发这个机器人的 Claude，每天看一次，做好了会告诉你。")
    return "\n".join(lines)


def dump_text(journal, now: float, tz: tzinfo | None, days: float, with_dialog: bool, group_chat_id: str = "") -> str:
    """For the developer's daily look on the server: the wishes, and the chats when asked for."""
    since = now - days * 86400
    out = [f"== 诉求（最近 {days:g} 天）=="]
    wishes = journal.wishes(since)
    if not wishes:
        out.append("（没有）")
    for wish in wishes:
        when = datetime.fromtimestamp(wish["ts"], tz=tz).strftime("%Y-%m-%d %H:%M")
        out.append(f"#{wish['id']} {when} [{wish['source']}/{wish['status']}] {wish['text']}")
    if with_dialog:
        out.append(f"\n== 对话（最近 {days:g} 天）==")
        rows = journal.dialog_between(since, now + 1)
        if not rows:
            out.append("（没有）")
        for row in rows:
            when = datetime.fromtimestamp(row["ts"], tz=tz).strftime("%m-%d %H:%M")
            where = "群" if group_chat_id and row["chat_id"] == group_chat_id else "私聊"
            answer = row["answer"].replace("\n", " / ")
            if len(answer) > 400:
                answer = answer[:400] + "……"
            out.append(f"[{when} {where}] 问：{row['question']}\n    答：{answer}")
    return "\n".join(out)
