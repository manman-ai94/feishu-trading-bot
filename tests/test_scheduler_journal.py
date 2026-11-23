from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from bot.journal import Journal
from bot.scheduler import Scheduler, due_jobs, scheduled_jobs

from .helpers import Clock, make_cfg, make_state

LA = ZoneInfo("America/Los_Angeles")


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """November 2026 in Los Angeles. The 15th is a Sunday, the 16th a Monday."""
    return datetime(2026, 11, day, hour, minute, second, tzinfo=LA)


def never(name: str) -> bool:
    return False


def test_jobs_are_due_from_their_time_until_the_grace_period_ends():
    cfg = make_cfg()
    assert due_jobs(cfg, at(16, 5, 59, 59), never) == []
    assert due_jobs(cfg, at(16, 6, 0), never) == ["morning"]
    assert due_jobs(cfg, at(16, 7, 29), never) == ["morning"]
    assert due_jobs(cfg, at(16, 7, 30), never) == []  # more than 90 minutes late: skipped
    assert due_jobs(cfg, at(16, 14, 10), never) == ["evening", "plan"]  # the day's plan at 17:05 New York


def test_weekly_review_only_on_its_weekday():
    cfg = make_cfg()
    assert due_jobs(cfg, at(15, 17, 5), never) == ["weekly"]  # Sunday
    assert due_jobs(cfg, at(16, 17, 5), never) == []  # Monday
    assert [name for name, _ in scheduled_jobs(cfg, at(15, 0, 0))] == ["morning", "evening", "weekly", "plan"]


def test_switched_off_jobs_are_never_due():
    cfg = make_cfg(brief_morning=None, brief_evening=time(21, 30), weekly_review=None)
    assert due_jobs(cfg, at(15, 6, 0), never) == []
    assert due_jobs(cfg, at(15, 21, 45), never) == ["evening"]


def test_jobs_that_already_ran_are_not_due():
    assert due_jobs(make_cfg(), at(16, 6, 5), lambda name: name == "morning") == []


def build(now: datetime):
    ran = []
    clock = Clock(now.timestamp())
    journal = Journal(":memory:")
    jobs = {name: (lambda name=name: ran.append(name)) for name in ("morning", "evening", "weekly")}
    return Scheduler(make_cfg(), journal, jobs, clock), ran, clock, journal


def test_tick_runs_a_job_once_per_day():
    scheduler, ran, clock, _ = build(at(16, 6, 0, 10))
    assert scheduler.tick() == ["morning"]
    clock.advance(20)
    assert scheduler.tick() == []
    clock.advance(8 * 3600)  # 14:00
    assert scheduler.tick() == ["evening"]
    clock.advance(16 * 3600)  # next day 06:00
    assert scheduler.tick() == ["morning"]
    assert ran == ["morning", "evening", "morning"]


def test_restart_does_not_send_the_same_brief_again():
    scheduler, ran, clock, journal = build(at(16, 6, 0, 10))
    scheduler.tick()
    restarted = Scheduler(make_cfg(), journal, {"morning": lambda: ran.append("again")}, clock)
    clock.advance(60)
    assert restarted.tick() == []
    assert ran == ["morning"]


def test_crashing_job_is_not_retried_in_a_loop():
    clock = Clock(at(16, 6, 0, 10).timestamp())
    attempts = []

    def crash():
        attempts.append(1)
        raise RuntimeError("boom")

    scheduler = Scheduler(make_cfg(), Journal(":memory:"), {"morning": crash}, clock)
    scheduler.tick()
    clock.advance(20)
    scheduler.tick()
    assert len(attempts) == 1


def test_journal_round_trip(tmp_path):
    journal = Journal(tmp_path / "nested" / "bot.db")
    journal.record_snapshot(make_state(total_eq=100.0), ts=1000)
    journal.record_snapshot(make_state(total_eq=None), ts=1500)
    journal.record_snapshot(make_state(total_eq=120.0), ts=2000)
    journal.record_event("opened", "开仓：XAU-USDT-SWAP 多单", "XAU-USDT-SWAP", ts=1200)

    assert journal.first_equity_since(0) == 100.0
    assert journal.first_equity_since(1200) == 120.0  # the snapshot without equity is skipped
    assert journal.first_equity_since(3000) is None
    assert journal.equity_series(0, 3000) == [(1000, 100.0), (2000, 120.0)]
    assert journal.has_event("opened", 1000, 2000) and not journal.has_event("opened", 1201, 2000)
    assert [row["summary"] for row in journal.events_between(0, 3000)] == ["开仓：XAU-USDT-SWAP 多单"]
    journal.close()

    reopened = Journal(tmp_path / "nested" / "bot.db")  # data survives a restart
    assert reopened.has_event("opened", 0, 3000)
    reopened.close()


def test_ai_usage_is_added_up_per_period():
    journal = Journal(":memory:")
    journal.record_usage("question", "claude-opus-5-5", requests=3, input_tokens=9000, output_tokens=1500, web_searches=2, cost_usd=0.12, ts=1000)
    journal.record_usage("morning brief", "claude-opus-5-5", cost_usd=0.30, web_searches=4, ts=1500)
    journal.record_usage("routing", "claude-haiku-5-5", cost_usd=0.001, ts=1600)
    journal.record_usage("self-check", "claude-haiku-5-5", cost_usd=0.002, ts=1700)
    journal.record_usage("question", "claude-opus-5-5", cost_usd=0.5, ts=5000)

    totals = journal.usage_between(0, 2000)
    assert totals["jobs"] == 2 and totals["web_searches"] == 6  # sorting messages and self-checks are not "jobs"
    assert abs(totals["cost_usd"] - 0.423) < 1e-9  # but what they cost is counted
    assert journal.usage_between(2000, 9000) == {"jobs": 1, "web_searches": 0, "cost_usd": 0.5}
    assert journal.usage_between(9000, 9999) == {"jobs": 0, "web_searches": 0, "cost_usd": 0}
