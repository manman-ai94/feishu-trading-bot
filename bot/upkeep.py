"""Upkeep: a heartbeat, so downtime gets noticed, and a daily copy of the data.

Heartbeat
  * Every minute the bot notes the time in its journal. When it starts, it
    compares that note with the clock: a gap means it was not running, and
    the start-up card says for how long, because nothing was watched then.
  * Optionally (HEARTBEAT_URL) it also fetches a web address every few
    minutes. A "dead man's switch" service behind that address alerts the
    owner when the fetches stop, which is the one thing a bot cannot report
    about itself. The fetch carries nothing about the account.

Health
  * A process can be running and still be of no use: the monitor stuck in a
    call that never returns, or the connection to Feishu gone and not coming
    back. Every minute the parts that matter are looked at. One that has not
    been alive for five minutes ends the process, and scripts/start.bat (or
    the systemd unit) starts it again, at most once in half an hour so a
    fault that a restart does not cure does not turn into a loop. While
    something is wrong the outside address above is not fetched, so the
    watcher there raises the alarm if the restart did not help.

Backup
  * Once a day the journal (snapshots, events, orders, the track record) and
    the owner's notes are copied into data/backups/, and copies older than
    BACKUP_KEEP_DAYS are removed. To go back to a copy: stop the bot, put
    the dated file in place of data/bot.db, start the bot.
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import requests

from .config import Config
from .journal import Journal
from .knowledge import NOTES_FILE

log = logging.getLogger(__name__)

HEARTBEAT_SETTING = "heartbeat_ts"
HEALTH_SECONDS = 5 * 60  # a part not alive for this long is stuck
HEALTH_RESTART_SECONDS = 30 * 60  # at most one restart for health in this long
HEALTH_RESTART_AT = "health_restart_at"
HEALTH_RESTART_REASON = "health_restart_reason"  # what was wrong, for the start-up card after the restart
HEALTH_LABELS = {"monitor": "监控仓位的循环停住了", "feishu": "和飞书的长连接断了"}
BEAT_SECONDS = 60
DOWNTIME_SECONDS = 5 * 60  # a shorter gap is a restart, not an outage
PING_SECONDS = 5 * 60
PING_TIMEOUT = 10
_DATED = re.compile(r"^(?:bot|notes)-(\d{8})\.(?:db|md)$")


def _get(url: str) -> None:
    requests.get(url, timeout=PING_TIMEOUT)


def downtime_text(gap: tuple[float, float], tz) -> str:
    """"上次停止运行约 2 小时 15 分（11-15 22:10 到 11-16 00:25）" for the start-up card."""
    start, end = gap
    minutes = int((end - start) // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} 天"] if days else []
    if hours:
        parts.append(f"{hours} 小时")
    if mins and not days:
        parts.append(f"{mins} 分")
    began = datetime.fromtimestamp(start, tz=tz).strftime("%m-%d %H:%M")
    back = datetime.fromtimestamp(end, tz=tz).strftime("%m-%d %H:%M")
    return f"上次停止运行约 {' '.join(parts) or '几分钟'}（{began} 到 {back}），这段时间没有监控仓位，也没有发提醒。"


def going_round(counter: Callable[[], int]) -> Callable[[], bool]:
    """Alive when the counter moved since the last look."""
    last = [None]

    def alive() -> bool:
        now = counter()
        moved = now != last[0]
        last[0] = now
        return moved

    return alive


class Health:
    """Whether the parts that matter are alive, and a restart when one of them has not been for too long."""

    def __init__(self, journal: Journal, restart: Callable[[], None], clock: Callable[[], float] = time.time):
        self._journal = journal
        self._restart = restart
        self._clock = clock
        self._watches: dict[str, list] = {}  # name -> [alive(), limit in seconds, last time it was alive]

    def watch(self, name: str, alive: Callable[[], bool], limit: float = HEALTH_SECONDS) -> None:
        """`name` is one of HEALTH_LABELS. It counts as alive from now, so a part gets `limit` to come up."""
        self._watches[name] = [alive, max(limit, HEALTH_SECONDS), self._clock()]

    def check(self) -> str:
        """What is wrong ("" when nothing is). Restarts the bot when something is, unless it did so lately."""
        now = self._clock()
        wrong = []
        for name, entry in self._watches.items():
            alive, limit, seen = entry
            try:
                ok = bool(alive())
            except Exception:
                ok = False
            if ok:
                entry[2] = now
            elif now - seen > limit:
                wrong.append((name, now - seen))
        if not wrong:
            return ""
        reason = "，".join(f"{HEALTH_LABELS.get(name, name)}（{int(gone // 60)} 分钟）" for name, gone in wrong)
        names = ", ".join(f"{name} for {int(gone // 60)} min" for name, gone in wrong)
        try:
            last = float(self._journal.get_setting(HEALTH_RESTART_AT) or 0)
        except ValueError:
            last = 0.0
        if now - last < HEALTH_RESTART_SECONDS:
            log.warning("health: not alive: %s; restarted for this less than 30 minutes ago, so not again", names)
            return reason
        log.warning("health: not alive: %s; restarting", names)
        self._journal.set_setting(HEALTH_RESTART_AT, str(int(now)))
        self._journal.set_setting(HEALTH_RESTART_REASON, reason)
        self._restart()
        return reason


def restart_text(journal: Journal, now: float, tz) -> str:
    """For the start-up card after a restart the health check made: when and why. Read once, then cleared."""
    reason = journal.get_setting(HEALTH_RESTART_REASON, "")
    if not reason:
        return ""
    journal.set_setting(HEALTH_RESTART_REASON, "")
    try:
        at = float(journal.get_setting(HEALTH_RESTART_AT) or 0)
    except ValueError:
        at = 0.0
    if not at or now - at > HEALTH_RESTART_SECONDS:
        return ""
    when = datetime.fromtimestamp(at, tz=tz).strftime("%H:%M")
    return f"{when} 我发现{reason}，自己重启了一次。"


class Upkeep:
    def __init__(
        self,
        cfg: Config,
        journal: Journal,
        clock: Callable[[], float] = time.time,
        fetch: Callable[[str], None] = _get,
        health: Health | None = None,
    ):
        self._cfg = cfg
        self._journal = journal
        self._clock = clock
        self._fetch = fetch
        self._health = health
        self._last_ping = 0.0
        self._backed_up: str | None = None  # the local date of the last backup made or found

    # ------------------------------------------------------------- heartbeat

    def downtime(self) -> tuple[float, float] | None:
        """(when the bot was last seen running, now), when that is long enough ago to matter.

        Ask before the first beat of this run.
        """
        try:
            last = float(self._journal.get_setting(HEARTBEAT_SETTING) or 0)
        except ValueError:
            return None
        now = self._clock()
        return (last, now) if last and now - last >= DOWNTIME_SECONDS else None

    def beat(self) -> None:
        now = self._clock()
        self._journal.set_setting(HEARTBEAT_SETTING, str(int(now)))
        if self._health is not None and self._health.check():
            return  # no ping while something is wrong: the outside watcher should notice
        url = self._cfg.heartbeat_url
        if url and now - self._last_ping >= PING_SECONDS:
            self._last_ping = now
            try:
                self._fetch(url)
            except Exception as exc:  # the watcher being unreachable must never disturb the bot
                log.info("heartbeat address not reached: %s", type(exc).__name__)

    # ---------------------------------------------------------------- backup

    @property
    def backup_dir(self) -> Path:
        return Path(self._cfg.db_path).parent / "backups"

    def backup_if_due(self) -> Path | None:
        """Makes today's copy when there is none yet. Returns the new file, or None."""
        keep = self._cfg.backup_keep_days
        if keep <= 0 or str(self._cfg.db_path) == ":memory:":
            return None
        today = datetime.fromtimestamp(self._clock(), tz=self._cfg.tz)
        stamp = today.strftime("%Y%m%d")
        if self._backed_up == stamp:
            return None
        folder = self.backup_dir
        target = folder / f"bot-{stamp}.db"
        made = None
        if not target.exists():
            folder.mkdir(parents=True, exist_ok=True)
            partial = folder / f"bot-{stamp}.db.part"
            partial.unlink(missing_ok=True)
            self._journal.backup_to(partial)
            partial.replace(target)  # a copy that was cut short never carries the final name
            notes = Path(self._cfg.knowledge_dir) / NOTES_FILE
            if notes.exists():
                shutil.copyfile(notes, folder / f"notes-{stamp}.md")
            made = target
            log.info("backup written: %s", target.name)
        self._backed_up = stamp
        self._prune(folder, (today - timedelta(days=keep)).strftime("%Y%m%d"))
        return made

    @staticmethod
    def _prune(folder: Path, oldest_kept: str) -> None:
        for path in folder.iterdir():
            dated = _DATED.match(path.name)
            if dated and dated.group(1) <= oldest_kept:
                path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ loop

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.beat()
                self.backup_if_due()
            except Exception:
                log.exception("upkeep failed")
            stop.wait(BEAT_SECONDS)
