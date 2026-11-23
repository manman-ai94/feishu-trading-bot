"""One-step install with a way back: what it writes, what it never touches, and when it goes back."""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from bot.deploy import DeployError, _run_in, deploy, members


class Box:
    """An installed bot in a folder, and the outside world as the deploy sees it."""

    def __init__(self, tmp_path, tests_pass=True, starts=(True,)):
        self.root = tmp_path / "bot-install"
        (self.root / "bot").mkdir(parents=True)
        (self.root / "data").mkdir()
        (self.root / "logs").mkdir()
        (self.root / "bot" / "__init__.py").write_text('__version__ = "0.13.0"\n', encoding="utf-8")
        (self.root / "bot" / "x.py").write_text("old = 1\n", encoding="utf-8")
        (self.root / "requirements.txt").write_text("lark-oapi==1.7.5\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=keep\n", encoding="utf-8")
        db = sqlite3.connect(self.root / "data" / "bot.db")
        db.execute("CREATE TABLE t (v)")
        db.execute("INSERT INTO t VALUES (42)")
        db.commit()
        db.close()
        (self.root / "logs" / "bot.log").write_text("old run\nconnecting to Feishu (long connection) ...\n", encoding="utf-8")
        self.zip = tmp_path / "ftb.zip"
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.writestr("bot/__init__.py", '__version__ = "0.14.1"\n')
            archive.writestr("bot/x.py", "new = 1\n")
            archive.writestr("bot/y.py", "added = 1\n")
            archive.writestr("requirements.txt", "lark-oapi==1.7.5\n")
            archive.writestr(".env", "SECRET=overwritten\n")
            archive.writestr("data/bot.db", "not a database")
        self.sha = hashlib.sha256(self.zip.read_bytes()).hexdigest()
        self.tests_pass = tests_pass
        self.starts = list(starts)  # one entry per restart: whether that start is clean
        self.calls: list[list[str]] = []
        self.now = 1_800_000_000.0

    def run(self, args, **kwargs):
        self.calls.append([str(a) for a in args])
        out = ""
        code = 0
        if "pytest" in args:
            code = 0 if self.tests_pass else 1
            out = "944 passed in 5.0s" if self.tests_pass else "1 failed, 943 passed"
        elif args[:2] == ["schtasks", "/Run"]:
            clean = self.starts.pop(0)
            with (self.root / "logs" / "bot.log").open("a", encoding="utf-8") as log:
                log.write("2026 INFO bot.app: connecting to Feishu (long connection) ...\n")
                if not clean:
                    log.write("Traceback (most recent call last):\n  boom\n")
        return subprocess.CompletedProcess(args, code, out, "")

    def sleep(self, seconds):
        self.now += seconds

    def deploy(self, sha=None, **kwargs):
        return deploy(self.root, self.zip, sha or self.sha, run=self.run, clock=lambda: self.now, sleep=self.sleep, **kwargs)

    def text(self, name):
        return (self.root / name).read_text(encoding="utf-8")

    def restarts(self):
        return [c for c in self.calls if c[:2] == ["schtasks", "/Run"]]


def test_a_good_version_goes_in_and_nothing_private_is_touched(tmp_path, capsys):
    box = Box(tmp_path)
    assert box.deploy() == 0
    assert box.text("bot/__init__.py") == '__version__ = "0.14.1"\n' and box.text("bot/y.py") == "added = 1\n"
    assert box.text(".env") == "SECRET=keep\n"
    copy = sqlite3.connect(box.root / "data" / "bot-before-0.14.1.db")
    assert copy.execute("SELECT v FROM t").fetchone() == (42,)
    [kept] = (box.root / "rollback").iterdir()
    assert kept.name.startswith("0.13.0-") and (kept / "bot" / "x.py").read_text(encoding="utf-8") == "old = 1\n"
    assert len(box.restarts()) == 1 and not any("pip" in c for c in box.calls)
    out = capsys.readouterr().out
    assert "tests: 944 passed" in out and out.strip().endswith("RESULT: v0.14.1 is running")


def test_requirements_that_differ_only_in_line_endings_install_nothing(tmp_path):
    box = Box(tmp_path)
    (box.root / "requirements.txt").write_bytes(b"lark-oapi==1.7.5\r\n")  # as Windows writes it
    assert box.deploy() == 0
    assert not any("pip" in c for c in box.calls)


def test_the_tests_run_are_the_new_ones_not_the_old_code_kept_aside(tmp_path):
    """The old code kept in rollback/ has its own tests/ folder: collected too, the two collide by name."""
    box = Box(tmp_path)
    assert box.deploy() == 0
    [tests] = [call for call in box.calls if "pytest" in call]
    assert tests[-2:] == ["tests", "--ignore=rollback"]


def test_a_zip_that_is_not_the_one_built_is_not_installed(tmp_path):
    box = Box(tmp_path)
    with pytest.raises(DeployError, match="sha256"):
        box.deploy(sha="0" * 64)
    assert box.text("bot/x.py") == "old = 1\n" and box.calls == []


def test_failing_tests_put_the_old_code_back_and_leave_the_bot_running(tmp_path, capsys):
    box = Box(tmp_path, tests_pass=False)
    assert box.deploy() == 1
    assert box.text("bot/x.py") == "old = 1\n" and box.text("bot/__init__.py") == '__version__ = "0.13.0"\n'
    assert not (box.root / "bot" / "y.py").exists()
    assert box.restarts() == []
    assert "RESULT: not installed; the running bot was not touched" in capsys.readouterr().out


def test_a_version_that_does_not_start_cleanly_is_rolled_back_and_the_old_one_started(tmp_path, capsys):
    box = Box(tmp_path, starts=(False, True))
    assert box.deploy() == 2
    assert box.text("bot/x.py") == "old = 1\n" and not (box.root / "bot" / "y.py").exists()
    assert len(box.restarts()) == 2
    assert "RESULT: v0.14.1 did not start; back on v0.13.0, which is running" in capsys.readouterr().out


def test_new_packages_are_installed_when_the_requirements_change(tmp_path):
    box = Box(tmp_path)
    (box.root / "requirements.txt").write_text("lark-oapi==1.7.4\n", encoding="utf-8")
    assert box.deploy(done_wishes="3,4") == 0
    assert any(c[-3:] == ["install", "-r", "requirements.txt"] for c in box.calls)
    assert any(c[-3:] == ["wishes", "--done", "3,4"] for c in box.calls)


def test_a_zip_with_a_path_outside_the_install_is_refused(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../evil.py", "x")
    with zipfile.ZipFile(bad) as archive, pytest.raises(DeployError, match="outside"):
        members(archive)


def test_the_requests_marked_are_reported_in_words_and_a_failed_marking_says_so(tmp_path, capsys):
    box = Box(tmp_path)
    plain_run = box.run

    def run(args, **kwargs):
        if "wishes" in args:
            box.calls.append([str(a) for a in args])
            return subprocess.CompletedProcess(args, 0, "已标成做好：#3 #4\n", "")
        return plain_run(args, **kwargs)

    box.run = run
    assert box.deploy(done_wishes="3,4") == 0
    assert "requests marked done: 已标成做好：#3 #4" in capsys.readouterr().out
    # The exit code is never shown as if it were how many were marked.
    box = Box(tmp_path / "again")
    plain_run = box.run

    def failing(args, **kwargs):
        if "wishes" in args:
            return subprocess.CompletedProcess(args, 1, "", "no such request: 99\n")
        return plain_run(args, **kwargs)

    box.run = failing
    assert box.deploy(done_wishes="99") == 0
    assert "requests 99 not marked (exit 1): no such request: 99" in capsys.readouterr().out


def test_commands_are_read_as_utf8_whatever_the_console_code_page(tmp_path):
    shown = _run_in(tmp_path)([sys.executable, "-c", "import sys; sys.stdout.buffer.write('诉求 #3 已做好'.encode('utf-8'))"])
    assert shown.returncode == 0 and shown.stdout == "诉求 #3 已做好"
