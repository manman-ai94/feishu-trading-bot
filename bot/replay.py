"""Replays real group questions through the bot's own answering path, without Feishu and without trading.

`python -m bot replay` reads one question per line from scripts/replay-group.txt (lines starting with
# are skipped) and hands each to the router as the owner @-mentioning the bot in the registered group,
in order, so a follow-up sees the earlier answers just as it would in the group. Every reply is printed
instead of sent. The trade desk is left out, so nothing can be ordered; the answers are kept out of the
track record. The AI calls are real and are metered like any other, which is what it costs.
"""
from __future__ import annotations

import time
from pathlib import Path

from .config import Config
from .feishu_client import IncomingMessage

QUESTIONS_FILE = Path(__file__).resolve().parent.parent / "scripts" / "replay-group.txt"
BOT_ID = "replay-bot"


class PrintingFeishu:
    """Stands in for the Feishu client: every reply is printed, nothing leaves the machine."""

    def __init__(self, out=print):
        self._out = out

    def reply_text(self, message_id: str, text: str) -> None:
        self._out(f"[文字] {text}")

    def reply_markdown(self, message_id: str, title: str, markdown: str, template: str = "blue", footer: str | None = None) -> None:
        head = f"[卡片：{title}]" if title else "[卡片，无标题]"
        self._out(head + "\n" + markdown.replace("**", "") + (f"\n（底注：{footer}）" if footer else ""))

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> None:
        self._out(f"[主动发送] {text}")

    def send_markdown(self, receive_id, title, markdown, receive_id_type="chat_id", template="blue", footer=None) -> None:
        self._out(f"[主动发送卡片：{title}]\n{markdown}")

    def add_reaction(self, message_id: str, emoji: str) -> str:
        return ""  # no sign is needed on a console

    def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        pass


def read_questions(path: Path = QUESTIONS_FILE) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


class Rehearsal:
    """The bot's own answering path, wired to a console: what replay and eval run questions through.

    Feishu is replaced by `feishu`, the trade desk is left out (nothing can be ordered), the levels
    of the answers stay out of the track record, and the monitor's first read goes to a scratch
    journal. Chat history stays in memory, so a rehearsal leaves no conversation behind. The AI's
    use is metered in the real journal with the rest of the month."""

    def __init__(self, cfg: Config, feishu):
        from .app import build_ai, build_crowd, build_macro, build_okx
        from .journal import Journal
        from .monitor import Monitor
        from .risk import RiskGauge
        from .router import Router
        from .service import Reporter

        self.cfg = cfg
        okx = build_okx(cfg)
        self.journal = Journal(cfg.db_path)
        self.analyst, resolver, _ = build_ai(cfg, okx, self.journal, build_crowd(cfg, okx), track=False, macro=build_macro(cfg))
        scratch = Journal(":memory:")  # the monitor's first read leaves nothing behind
        gauge = RiskGauge(okx)
        reporter = Reporter(cfg, feishu, okx, self.analyst, scratch, crowd=None, gauge=gauge)
        reporter.monitor = Monitor(okx, cfg, scratch, lambda event: None, gauge=gauge)
        self.analyst.attach_account(lambda: reporter.account_for_ai(fresh=True), reporter.history_for_ai)
        reporter.monitor.poll_once()
        self.router = Router(
            cfg, feishu, okx, self.analyst, reporter, BOT_ID, resolver=resolver, trade=None,
            later=lambda seconds, call: (lambda: None),
        )  # fmt: skip

    def ask(self, number: int, question: str, in_group: bool = True) -> None:
        """Hands one question to the router as the owner: @-mentioning the bot in the group, or in his private chat."""
        self.router.handle(
            IncomingMessage(
                message_id=f"replay{number}",
                chat_id=self.cfg.group_chat_id if in_group else PRIVATE_CHAT,
                chat_type="group" if in_group else "p2p",
                sender_open_id=self.cfg.owner_open_id,
                text=question,
                mentioned_open_ids=(BOT_ID,) if in_group else (),
            )
        )


PRIVATE_CHAT = "replay-private"


def replay(cfg: Config, path: Path = QUESTIONS_FILE, out=print) -> int:
    if not cfg.owner_open_id or not cfg.group_chat_id:
        out("[X] 没有登记主人或群（OWNER_OPEN_ID、GROUP_CHAT_ID），无法按群里的样子重放。")
        return 1
    questions = read_questions(path)
    if not questions:
        out(f"[X] {path} 里没有问题。")
        return 1
    rehearsal = Rehearsal(cfg, PrintingFeishu(out))
    if not rehearsal.analyst.enabled:
        out("[X] 还没有配置 Claude 的 API key。")
        return 1
    started = time.time()
    for number, question in enumerate(questions, 1):
        out(f"\n===== {number}. 问：{question}")
        asked = time.time()
        rehearsal.ask(number, question)
        out(f"（用时 {time.time() - asked:.0f} 秒）")
    used = rehearsal.journal.usage_between(int(started), time.time() + 1)
    out(f"\n[用量] {len(questions)} 个问题，联网搜索 {used['web_searches']} 次，约 {used['cost_usd']:.3f} 美元（估算）")
    return 0
