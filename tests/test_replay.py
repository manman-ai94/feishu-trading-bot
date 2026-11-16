"""python -m bot replay: real group questions through the bot's own path, printed, never sent or ordered."""
from __future__ import annotations

import bot.app
import bot.risk
from bot.replay import QUESTIONS_FILE, read_questions, replay

from .helpers import FakeAnalyst, FakeOkx, make_cfg, make_state


def test_the_question_file_holds_the_group_questions_and_no_orders():
    questions = read_questions(QUESTIONS_FILE)
    assert len(questions) == 8 and questions[0] == "汇报目前持仓情况和风险点"
    for order_word in ("把止损调到", "平仓 XAU", "确认 ", "记住"):
        assert not any(question.startswith(order_word) for question in questions)


def test_replay_prints_every_answer_and_sends_nothing(tmp_path, monkeypatch):
    questions = tmp_path / "q.txt"
    questions.write_text("# a comment\n汇报目前持仓情况和风险点\n这个我去跟 Claude 商量\n", encoding="utf-8")
    analyst = FakeAnalyst()
    analyst.intent = "status"
    okx = FakeOkx(make_state())
    built = {}

    def fake_build_ai(cfg, okx, journal, crowd=None, track=True):
        built["track"] = track
        return analyst, None, None

    monkeypatch.setattr(bot.app, "build_okx", lambda cfg: okx)
    monkeypatch.setattr(bot.app, "build_crowd", lambda cfg, okx: None)
    monkeypatch.setattr(bot.app, "build_ai", fake_build_ai)
    monkeypatch.setattr(bot.risk, "RiskGauge", lambda okx: None)
    printed: list[str] = []
    cfg = make_cfg(db_path=tmp_path / "bot.db")
    assert replay(cfg, questions, out=printed.append) == 0
    text = "\n".join(printed)
    assert "1. 问：汇报目前持仓情况和风险点" in text and "2. 问：这个我去跟 Claude 商量" in text
    assert "AI:answer" in text  # a question with 风险 goes to the AI even when the router says status
    assert built["track"] is False  # a rehearsal stays out of the track record
    assert "[用量] 2 个问题" in text


def test_replay_needs_the_owner_and_the_group(tmp_path):
    printed: list[str] = []
    assert replay(make_cfg(group_chat_id="", db_path=tmp_path / "bot.db"), out=printed.append) == 1
    assert "GROUP_CHAT_ID" in printed[0]
