"""Upkeep: the heartbeat that makes downtime visible, and the daily copy of the data."""
from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

from bot.journal import Journal
from bot.upkeep import HEARTBEAT_SETTING, Upkeep, downtime_text

from .helpers import Clock, make_cfg

NOW = 1_800_000_000.0
UTC = ZoneInfo("UTC")


def upkeep(tmp_path, journal=None, clock=None, pings=None, **cfg):
    cfg.setdefault("db_path", tmp_path / "data" / "bot.db")
    cfg.setdefault("knowledge_dir", tmp_path / "knowledge")
    cfg.setdefault("timezone", "UTC")
    config = make_cfg(**cfg)
    journal = journal or Journal(config.db_path)
    clock = clock or Clock(NOW)

    def fetch(url: str) -> None:
        if pings is not None:
            pings.append(url)

    return Upkeep(config, journal, clock, fetch), journal, clock


# ------------------------------------------------------------------ heartbeat


def test_the_first_run_ever_reports_no_downtime(tmp_path):
    keeper, journal, _ = upkeep(tmp_path)
    assert keeper.downtime() is None
    keeper.beat()
    assert journal.get_setting(HEARTBEAT_SETTING) == str(int(NOW))


def test_a_restart_is_not_an_outage_but_a_long_gap_is(tmp_path):
    keeper, journal, clock = upkeep(tmp_path)
    keeper.beat()
    clock.now = NOW + 90  # an update: stopped and started again
    assert keeper.downtime() is None
    clock.now = NOW + 2 * 3600 + 15 * 60
    assert keeper.downtime() == (NOW, clock.now)
    keeper.beat()
    assert keeper.downtime() is None  # running again


def test_the_outage_is_put_in_words_with_local_times():
    text = downtime_text((NOW, NOW + 2 * 3600 + 15 * 60), UTC)
    assert text.startswith("上次停止运行约 2 小时 15 分（01-15 08:00 到 01-15 10:15）") and "没有监控仓位" in text
    assert "约 3 天 4 小时（" in downtime_text((NOW, NOW + 3 * 86400 + 4 * 3600 + 20 * 60), UTC)
    assert "约 7 分（" in downtime_text((NOW, NOW + 7 * 60 + 30), UTC)


def test_the_outside_watcher_is_pinged_every_few_minutes_and_only_when_set(tmp_path):
    pings: list[str] = []
    keeper, _, clock = upkeep(tmp_path, pings=pings, heartbeat_url="https://hc.example/ping/abc")
    for minute in range(11):
        clock.now = NOW + minute * 60
        keeper.beat()
    assert pings == ["https://hc.example/ping/abc"] * 3  # at 0, 5 and 10 minutes

    silent: list[str] = []
    keeper, _, _ = upkeep(tmp_path, pings=silent)
    keeper.beat()
    assert silent == []


def test_a_watcher_that_cannot_be_reached_never_disturbs_the_bot(tmp_path):
    config = make_cfg(db_path=tmp_path / "bot.db", heartbeat_url="https://hc.example/ping/abc")
    journal = Journal(config.db_path)

    def fetch(url: str) -> None:
        raise ConnectionError("no route")

    Upkeep(config, journal, Clock(NOW), fetch).beat()
    assert journal.get_setting(HEARTBEAT_SETTING) == str(int(NOW))


# --------------------------------------------------------------------- backup


def test_one_complete_copy_a_day(tmp_path):
    keeper, journal, clock = upkeep(tmp_path)
    journal.record_event("open", "开多 XAU 100 张", "XAU-USDT-SWAP", ts=NOW)
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "notes.md").write_text("只做 4H 顺势单", encoding="utf-8")

    made = keeper.backup_if_due()
    assert made == tmp_path / "data" / "backups" / "bot-20270115.db"
    copy = sqlite3.connect(made)
    assert copy.execute("SELECT summary FROM events").fetchall() == [("开多 XAU 100 张",)]
    copy.close()
    assert (tmp_path / "data" / "backups" / "notes-20270115.md").read_text(encoding="utf-8") == "只做 4H 顺势单"
    assert not list((tmp_path / "data" / "backups").glob("*.part"))

    clock.now = NOW + 3600
    assert keeper.backup_if_due() is None  # today's copy exists
    clock.now = NOW + 86400
    assert keeper.backup_if_due().name == "bot-20270116.db"


def test_a_restart_does_not_make_a_second_copy_of_the_same_day(tmp_path):
    keeper, journal, _ = upkeep(tmp_path)
    keeper.backup_if_due()
    again, _, _ = upkeep(tmp_path, journal=journal)
    assert again.backup_if_due() is None


def test_old_copies_are_removed_and_other_files_left_alone(tmp_path):
    keeper, _, clock = upkeep(tmp_path, backup_keep_days=3)
    folder = tmp_path / "data" / "backups"
    folder.mkdir(parents=True)
    for name in ("bot-20270110.db", "notes-20270110.md", "bot-20270112.db", "bot-20270113.db", "my-own-file.txt", "bot-keep.db"):
        (folder / name).write_text("x", encoding="utf-8")
    keeper.backup_if_due()  # 2027-01-15: copies of the 13th and later stay
    assert sorted(path.name for path in folder.iterdir()) == ["bot-20270113.db", "bot-20270115.db", "bot-keep.db", "my-own-file.txt"]


def test_backups_can_be_switched_off(tmp_path):
    keeper, _, _ = upkeep(tmp_path, backup_keep_days=0)
    assert keeper.backup_if_due() is None and not (tmp_path / "data" / "backups").exists()
