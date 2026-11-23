"""Runs the timed jobs (pre-market brief, daily wrap, weekly review, the day's trading plan) in the
owner's time zone. Each job runs at most once per day, even across restarts,
because every run is written to the journal."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Config
from .discipline import PLAN_WEEKDAYS_NY, plan_time
from .journal import Journal

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=90)  # a job missed by more than this is skipped, not sent late
# Friday 16:00 in New York: an hour before spot gold closes for the weekend, which it does at 17:00 New York
# time all year round (22:00 UTC in winter, 21:00 UTC in summer).
WEEKEND_CHECK_NY = (4, 16)
NEW_YORK = ZoneInfo("America/New_York")


def scheduled_jobs(cfg: Config, now_local: datetime) -> list[tuple[str, datetime]]:
    """Today's jobs as (name, scheduled local time)."""
    jobs: list[tuple[str, datetime]] = []

    def at(clock_time) -> datetime:
        return now_local.replace(hour=clock_time.hour, minute=clock_time.minute, second=0, microsecond=0)

    if cfg.brief_morning is not None:
        jobs.append(("morning", at(cfg.brief_morning)))
    if cfg.brief_evening is not None:
        jobs.append(("evening", at(cfg.brief_evening)))
    if cfg.weekly_review is not None and now_local.weekday() == cfg.weekly_review[0]:
        jobs.append(("weekly", at(cfg.weekly_review[1])))
    now_ny = now_local.astimezone(NEW_YORK)
    if cfg.weekend_check and now_ny.weekday() == WEEKEND_CHECK_NY[0]:
        friday = now_ny.replace(hour=WEEKEND_CHECK_NY[1], minute=0, second=0, microsecond=0)
        jobs.append(("weekend", friday.astimezone(now_local.tzinfo)))
    if cfg.plan_required and now_ny.weekday() in PLAN_WEEKDAYS_NY:
        # 17:05 New York, just after gold's session ends: the plan for the trading day that begins (discipline.py).
        jobs.append(("plan", plan_time(now_local.timestamp()).astimezone(now_local.tzinfo)))
    return jobs


def due_jobs(cfg: Config, now_local: datetime, already_ran: Callable[[str], bool]) -> list[str]:
    return [
        name
        for name, scheduled in scheduled_jobs(cfg, now_local)
        if scheduled <= now_local < scheduled + GRACE and not already_ran(name)
    ]


class Scheduler:
    def __init__(self, cfg: Config, journal: Journal, jobs: dict[str, Callable[[], None]], clock: Callable[[], float] = time.time):
        self._cfg = cfg
        self._journal = journal
        self._jobs = jobs
        self._clock = clock

    def tick(self) -> list[str]:
        """Run whatever is due right now. Returns the names that ran."""
        now_local = datetime.fromtimestamp(self._clock(), tz=self._cfg.tz)
        day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        def already_ran(name: str) -> bool:
            return self._journal.has_event(f"job_{name}", day_start.timestamp(), day_end.timestamp())

        ran = []
        for name in due_jobs(self._cfg, now_local, already_ran):
            # Record first: a job that crashes is not retried in a loop.
            self._journal.record_event(f"job_{name}", now_local.strftime("%Y-%m-%d %H:%M"), ts=self._clock())
            try:
                self._jobs[name]()
            except Exception:
                log.exception("scheduled job %s failed", name)
            ran.append(name)
        return ran

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick crashed")
            stop.wait(20)
