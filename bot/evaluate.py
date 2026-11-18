"""An eval set for the AI's answers: fixed questions, a score for each answer, and the change since the last run.

`python -m bot eval` asks every question in scripts/eval-questions.json through the bot's own
answering path (see replay.Rehearsal: no Feishu, no trading, nothing kept in the track record),
then has the AI score each answer on five checks every answer gets, plus that question's own
points. The scores are written to data/eval/ and compared with the previous run, so a change to
the instructions is judged by the scores rather than by feel.

Every answer and every score is a real AI call, metered like any other: a run costs a few dollars.
`--dry-run` lists the questions without calling anything.

The scorer does not see what the tools returned, so it cannot check a number against its source;
it marks what looks made up. Answers to market questions depend on the market at the time, so
compare runs over the same few hours when the difference matters.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config

QUESTIONS_FILE = Path(__file__).resolve().parent.parent / "scripts" / "eval-questions.json"
CHECKS = (
    ("answers", "答所问：答的是他问的那件事，第一句就回答"),
    ("grounded", "有依据：价位和判断说明了依据（周期、前高前低、指标、数据）；不涉及价位的问题看是否有理有据"),
    ("sourced", "不编造：数字看起来来自数据和工具，没取到的说没取到；明显编造给 0"),
    ("no_orders", "不替他拍板：没有“现在买入”“把止损提到 X”“先平一半”这类指令"),
    ("length", "篇幅合适：问事实两三句，问判断适中，不重复前面说过的"),
)
JUDGE_SYSTEM = (
    "你是评分员，给一个交易助手机器人在飞书里的回答打分。交易员做主观交易，主要做黄金。"
    "只输出一个 JSON 对象，不要输出别的内容，不要用代码块。\n"
    "通用五项，每项 0、1 或 2 分：\n"
    + "\n".join(f"{key}：{text}" for key, text in CHECKS)
    + "\n再按这道题自己的要点逐条判断：做到了写 1，没做到写 0。要点不适用（例如“没有持仓时……”而他有持仓）也写 1。\n"
    '格式：{"answers":2,"grounded":1,"sourced":2,"no_orders":2,"length":1,"points":[1,0],"note":"一句话说扣分的原因，没扣分写“无”"}'
)
_JSON = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Question:
    number: int
    chat: str  # group | private
    source: str
    question: str
    points: list[str]


def read_questions(path: Path = QUESTIONS_FILE) -> list[Question]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Question(number, str(item.get("chat", "group")), str(item.get("source", "")), str(item["question"]), [str(p) for p in item.get("points", [])])
        for number, item in enumerate(data["questions"], 1)
    ]


class CapturingFeishu:
    """Keeps the bot's replies instead of sending them."""

    def __init__(self):
        self.replies: list[str] = []

    def reply_text(self, message_id: str, text: str) -> None:
        self.replies.append(text)

    def reply_markdown(self, message_id: str, title: str, markdown: str, template: str = "blue", footer: str | None = None) -> None:
        self.replies.append((f"【{title}】\n" if title else "") + markdown)

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> None:
        pass

    def send_markdown(self, receive_id, title, markdown, receive_id_type="chat_id", template="blue", footer=None) -> None:
        pass

    def add_reaction(self, message_id: str, emoji: str) -> str:
        return ""

    def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        pass


def parse_score(raw: str, points: int) -> dict | None:
    """The scorer's JSON, checked. None when it cannot be read."""
    found = _JSON.search(raw or "")
    if not found:
        return None
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return None
    score = {}
    for key, _ in CHECKS:
        value = data.get(key)
        if not isinstance(value, (int, float)) or not 0 <= value <= 2:
            return None
        score[key] = int(value)
    marks = data.get("points")
    if not isinstance(marks, list) or len(marks) != points or any(mark not in (0, 1) for mark in marks):
        return None
    score["points"] = marks
    score["note"] = str(data.get("note", ""))[:200]
    return score


def total(score: dict | None, points: int) -> tuple[int, int]:
    best = 2 * len(CHECKS) + points
    if score is None:
        return 0, best
    return sum(score[key] for key, _ in CHECKS) + sum(score["points"]), best


def judge_prompt(question: Question, answer: str) -> str:
    points = "\n".join(f"{index}. {point}" for index, point in enumerate(question.points, 1)) or "（没有）"
    where = "群里（交易员和朋友的群，交易员同意公开账户）" if question.chat == "group" else "交易员的私聊"
    return f"在哪里问的：{where}\n问题：{question.question}\n\n这道题的要点：\n{points}\n\n机器人的回答：\n{answer or '（没有回答）'}"


def run_eval(cfg: Config, path: Path = QUESTIONS_FILE, out=print, dry_run: bool = False, only: set[int] | None = None, results_dir: Path | None = None) -> int:
    questions = [q for q in read_questions(path) if not only or q.number in only]
    if not questions:
        out("[X] 没有要问的问题。")
        return 1
    if dry_run:
        for q in questions:
            out(f"{q.number}. [{'群里' if q.chat == 'group' else '私聊'}，{q.source}] {q.question}")
            for point in q.points:
                out(f"    - {point}")
        out(f"\n共 {len(questions)} 题。没有调用 AI，没有花钱。")
        return 0
    if not cfg.owner_open_id or not cfg.group_chat_id:
        out("[X] 没有登记主人或群（OWNER_OPEN_ID、GROUP_CHAT_ID），无法按群里的样子提问。")
        return 1

    from .replay import Rehearsal

    feishu = CapturingFeishu()
    rehearsal = Rehearsal(cfg, feishu)
    if not rehearsal.analyst.enabled:
        out("[X] 还没有配置 Claude 的 API key。")
        return 1
    started = time.time()
    rows = []
    for q in questions:
        before = len(feishu.replies)
        rehearsal.ask(q.number, q.question, in_group=q.chat == "group")
        answer = "\n\n".join(feishu.replies[before:])
        try:
            score = parse_score(rehearsal.analyst.grade(JUDGE_SYSTEM, judge_prompt(q, answer)), len(q.points))
        except Exception as exc:  # one failed score leaves the others standing
            out(f"[!] 第 {q.number} 题没打出分：{exc}")
            score = None
        got, best = total(score, len(q.points))
        note = (score or {}).get("note", "评分没读出来")
        out(f"{q.number:>2}. {got:>2}/{best}  {q.question}" + (f"（{note}）" if note and note != "无" else ""))
        rows.append({"number": q.number, "question": q.question, "chat": q.chat, "answer": answer, "score": score, "got": got, "best": best})

    got = sum(row["got"] for row in rows)
    best = sum(row["best"] for row in rows)
    used = rehearsal.journal.usage_between(int(started), time.time() + 1)
    out(f"\n总分 {got}/{best}（{got / best * 100:.0f}%），{len(rows)} 题，约 {used['cost_usd']:.2f} 美元（估算）")
    folder = results_dir or Path(cfg.db_path).parent / "eval"
    folder.mkdir(parents=True, exist_ok=True)
    previous = sorted(folder.glob("eval-*.json"))
    if previous:
        out(compare(json.loads(previous[-1].read_text(encoding="utf-8")), rows, previous[-1].name))
    stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
    target = folder / f"eval-{stamp}.json"
    target.write_text(json.dumps({"at": stamp, "rows": rows, "cost_usd": used["cost_usd"]}, ensure_ascii=False, indent=1), encoding="utf-8")
    out(f"结果存在 {target}")
    return 0


def compare(previous: dict, rows: list[dict], name: str) -> str:
    """The change since the last run, on the questions both runs asked."""
    before = {row["question"]: row for row in previous.get("rows", [])}
    shared = [row for row in rows if row["question"] in before]
    if not shared:
        return f"和上一次（{name}）没有相同的题，没法比较。"
    now = sum(row["got"] for row in shared)
    then = sum(before[row["question"]]["got"] for row in shared)
    lines = [f"和上一次（{name}）比：相同的 {len(shared)} 题从 {then} 分到 {now} 分（{now - then:+d}）"]
    for row in shared:
        change = row["got"] - before[row["question"]]["got"]
        if abs(change) >= 2:
            lines.append(f"- {row['number']}. {row['question']}：{change:+d}")
    return "\n".join(lines)
