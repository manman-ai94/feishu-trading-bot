"""python -m bot eval: fixed questions through the bot's own path, each answer scored, compared with the last run."""
from __future__ import annotations

import json

import bot.app
import bot.risk
from bot.__main__ import main
from bot.evaluate import CHECKS, QUESTIONS_FILE, compare, parse_score, read_questions, run_eval, total

from .helpers import FakeAnalyst, FakeOkx, make_cfg, make_state


def test_the_question_file_has_thirty_questions_and_no_orders():
    questions = read_questions(QUESTIONS_FILE)
    assert len(questions) == 30 and all(q.points for q in questions)
    assert {q.chat for q in questions} == {"group", "private"}
    assert sum(1 for q in questions if q.source != "补写") == 10  # the real ones, said where they came from
    for order_word in ("止损 XAU", "平仓 XAU", "做多 XAU", "撤单", "确认"):
        assert not any(q.question.startswith(order_word) for q in questions)


def test_a_score_is_read_only_when_it_is_complete():
    good = '好的：{"answers":2,"grounded":1,"sourced":2,"no_orders":2,"length":1,"points":[1,0],"note":"没给失效位"}'
    score = parse_score(good, 2)
    assert total(score, 2) == (9, 12) and score["note"] == "没给失效位"
    assert parse_score(good, 3) is None  # one mark per point
    assert parse_score('{"answers":3,"grounded":1,"sourced":2,"no_orders":2,"length":1,"points":[]}', 0) is None
    assert parse_score("不是 JSON", 0) is None and total(None, 2) == (0, 2 * len(CHECKS) + 2)


def test_the_change_since_last_time_is_on_the_shared_questions():
    previous = {"rows": [{"question": "a", "got": 8}, {"question": "b", "got": 10}, {"question": "gone", "got": 1}]}
    rows = [{"number": 1, "question": "a", "got": 11}, {"number": 2, "question": "b", "got": 9}, {"number": 3, "question": "new", "got": 5}]
    text = compare(previous, rows, "eval-1.json")
    assert text.startswith("和上一次（eval-1.json）比：相同的 2 题从 18 分到 20 分（+2）") and "- 1. a：+3" in text and "b：" not in text


def test_a_dry_run_lists_the_questions_and_calls_nothing(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(bot.app, "build_ai", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AI on a dry run")))
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    assert main(["--env", str(env), "eval", "--dry-run", "--only", "9,10"]) == 0
    output = capsys.readouterr().out
    assert "9. [群里，群里 11-17（这次改版的起因）] 查询我现在的仓位和挂单情况" in output and "    - 指出没带止损的挂单" in output
    assert "共 2 题。没有调用 AI，没有花钱。" in output


def test_a_run_asks_each_question_scores_it_and_keeps_the_result(tmp_path, monkeypatch):
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps({"questions": [
        {"chat": "group", "source": "x", "question": "黄金怎么看", "points": ["给价位"]},
        {"chat": "private", "source": "x", "question": "那白银呢", "points": []},
    ]}, ensure_ascii=False), encoding="utf-8")  # fmt: skip
    analyst = FakeAnalyst()
    graded = []

    def grade(system, user):
        graded.append(user)
        return '{"answers":2,"grounded":2,"sourced":2,"no_orders":2,"length":2,"points":[1],"note":"无"}' if len(graded) == 1 else "乱写"

    analyst.grade = grade
    monkeypatch.setattr(bot.app, "build_okx", lambda cfg: FakeOkx(make_state()))
    monkeypatch.setattr(bot.app, "build_crowd", lambda cfg, okx: None)
    monkeypatch.setattr(bot.app, "build_macro", lambda cfg: None)
    monkeypatch.setattr(bot.app, "build_ai", lambda cfg, okx, journal, crowd=None, track=True, macro=None: (analyst, None, None))
    monkeypatch.setattr(bot.risk, "RiskGauge", lambda okx: None)
    printed = []
    cfg = make_cfg(db_path=tmp_path / "bot.db")
    assert run_eval(cfg, questions, out=printed.append, results_dir=tmp_path / "eval") == 0
    text = "\n".join(printed)
    assert " 1. 11/11  黄金怎么看" in text and " 2.  0/10  那白银呢（评分没读出来）" in text and "总分 11/21（52%）" in text
    assert "机器人的回答：\nAI:answer" in graded[0] and "在哪里问的：交易员的私聊" in graded[1]
    [saved] = (tmp_path / "eval").glob("eval-*.json")
    assert json.loads(saved.read_text(encoding="utf-8"))["rows"][0]["answer"] == "AI:answer"
    assert [call["in_group"] for call in analyst.named("answer")] == [True, False]
