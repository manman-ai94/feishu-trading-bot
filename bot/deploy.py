"""Installs a new version in one step, and goes back to the old one when the new one does not start cleanly.

    python -m bot.deploy --zip C:/bot/ftb.zip --sha256 <hash of the zip>

Run it from the installed folder with the bot's own Python. In order:
  1. the zip must have exactly the hash given (the one made where it was built);
  2. a consistent copy of the database is made (data/bot-before-<version>.db), the bot running or not;
  3. every file the zip will overwrite is copied aside (rollback/<old version>-<time>/), and the
     files it will add are noted, so the old code can be put back exactly; .env, data/, logs/ and
     the owner's knowledge files are not in the zip and are never touched;
  4. the zip is unpacked; when requirements.txt changed, the packages are installed;
  5. the tests run; when they fail, the old code is put back and the running bot is left alone;
  6. requests the version did can be marked (--done-wishes 3,4), for its start-up card;
  7. the bot is restarted (the Windows scheduled task, or a systemd service), and the log is watched
     until it says it is connecting to Feishu, with no error for a while after;
  8. when that does not happen, the old code is put back and the bot restarted on it.

It prints what it did in plain English lines and exits 0 only when the new version is running.
It never reads .env and never places, changes or cancels an order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

KEEP_OUT = ("data/", "logs/", "rollback/", ".venv/")  # folders a deploy never writes into, whatever the zip holds
STARTED = "connecting to Feishu"
BAD = re.compile(r"Traceback \(most recent call last\)| ERROR | CRITICAL ")
SETTLE_SECONDS = 20  # after the start line, this long without an error counts as a clean start
VERSION = re.compile(r'__version__\s*=\s*"([^"]+)"')


def say(text: str) -> None:
    """One line of the report. A character the console cannot show (Windows over SSH: cp1252, and the bot's
    log is Chinese) must not stop the deploy: a failure here would skip the way back."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="backslashreplace").decode(encoding), flush=True)


def _speak_utf8() -> None:
    """Where the console can be told to, write UTF-8: the person reading over SSH sees the Chinese."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


class DeployError(Exception):
    pass


@dataclass
class Snapshot:
    folder: Path
    saved: list[str] = field(default_factory=list)  # files the zip overwrote, copied into `folder`
    added: list[str] = field(default_factory=list)  # files the zip added, to delete when going back


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def members(archive: zipfile.ZipFile) -> list[str]:
    """The files in the zip that a deploy may write: no folders, nothing outside the install, nothing kept out."""
    names = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir():
            continue
        if name.startswith("/") or ".." in name.split("/") or ":" in name:
            raise DeployError(f"the zip holds a path outside the install: {name}")
        if name == ".env" or name.startswith(KEEP_OUT):
            continue
        names.append(name)
    return names


def version_in(text: str) -> str:
    found = VERSION.search(text)
    return found.group(1) if found else "unknown"


def backup_db(db: Path, target: Path) -> bool:
    """A consistent copy through SQLite's backup, safe while the bot writes. False when there is no database."""
    if not db.exists():
        return False
    source = sqlite3.connect(str(db))
    try:
        copy = sqlite3.connect(str(target))
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    return True


def take_snapshot(root: Path, names: list[str], folder: Path) -> Snapshot:
    snapshot = Snapshot(folder)
    for name in names:
        current = root / name
        if current.is_file():
            kept = folder / name
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current, kept)
            snapshot.saved.append(name)
        else:
            snapshot.added.append(name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "snapshot.json").write_text(json.dumps({"saved": snapshot.saved, "added": snapshot.added}), encoding="utf-8")
    return snapshot


def restore(root: Path, snapshot: Snapshot) -> None:
    for name in snapshot.saved:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot.folder / name, target)
    for name in snapshot.added:
        (root / name).unlink(missing_ok=True)


def extract(archive: zipfile.ZipFile, names: list[str], root: Path) -> None:
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(name) as source, target.open("wb") as out:
            shutil.copyfileobj(source, out)


def bot_processes(root: Path, run: Callable) -> list[int]:
    """Windows: the python processes running this install's bot ("-m bot run") and its MT5 worker
    ("-m bot.mt5_worker"), which ending the task may leave."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*-m bot run*' -or $_.CommandLine -like '*-m bot.mt5_worker*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    done = run(["powershell", "-NoProfile", "-Command", script], timeout=60)
    return [int(line) for line in (done.stdout or "").split() if line.strip().isdigit()]


def restart(how: str, name: str, root: Path, run: Callable, sleep: Callable[[float], None]) -> None:
    if how == "schtasks":
        run(["schtasks", "/End", "/TN", name], timeout=60)
        sleep(3)
        for pid in bot_processes(root, run):
            say(f"stopping a bot process the task left running: {pid}")
            run(["taskkill", "/PID", str(pid), "/F"], timeout=60)
        sleep(2)
        started = run(["schtasks", "/Run", "/TN", name], timeout=60)
        if started.returncode != 0:
            raise DeployError(f"the scheduled task {name} did not start: {(started.stdout or '').strip()} {(started.stderr or '').strip()}")
    elif how == "systemd":
        started = run(["systemctl", "restart", name], timeout=120)
        if started.returncode != 0:
            raise DeployError(f"systemctl restart {name} failed: {(started.stderr or '').strip()}")
    elif how != "none":
        raise DeployError(f"unknown way to restart: {how}")


def log_size(log: Path) -> int:
    return log.stat().st_size if log.exists() else 0


def watch_log(log: Path, offset: int, wait: float, clock: Callable[[], float], sleep: Callable[[float], None]) -> tuple[bool, str]:
    """Whether the bot started cleanly: the start line written after `offset`, and no error up to SETTLE_SECONDS
    after it. Returns (clean, the new lines of the log, cut to the last 40)."""
    deadline = clock() + wait
    started_at = None
    text = ""
    while clock() < deadline:
        size = log_size(log)
        start = offset if size >= offset else 0  # the log was rotated: read the new file from its start
        if log.exists():
            with log.open("rb") as handle:
                handle.seek(start)
                text = handle.read().decode("utf-8", errors="replace")
        if BAD.search(text):
            return False, _tail(text)
        if STARTED in text and started_at is None:
            started_at = clock()
        if started_at is not None and clock() - started_at >= SETTLE_SECONDS:
            return True, _tail(text)
        sleep(2)
    return False, _tail(text)


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _run_in(root: Path) -> Callable:
    """Runs a command in the install and reads what it prints as UTF-8. On Windows the default would be the
    console's code page (cp1252), which cannot read Chinese output: the reading thread then fails and the
    output is lost, though the command itself worked."""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    def run(args, **kw):
        return subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=root, env=env, **kw
        )

    return run


def deploy(
    root: Path,
    zip_path: Path,
    sha256: str,
    how: str = "schtasks",
    name: str = "FeishuTradingBot",
    wait: float = 150,
    done_wishes: str = "",
    run: Callable = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    run = run or _run_in(root)
    python = sys.executable
    if not zip_path.is_file():
        raise DeployError(f"no zip at {zip_path}")
    actual = sha256_of(zip_path)
    if actual.lower() != sha256.strip().lower():
        raise DeployError(f"the zip's sha256 is {actual}, not the {sha256} given: not installed")
    say(f"zip ok: sha256 {actual}")

    with zipfile.ZipFile(zip_path) as archive:
        names = members(archive)
        if "bot/__init__.py" not in names:
            raise DeployError("the zip has no bot/__init__.py: not this bot's code")
        new = version_in(archive.read("bot/__init__.py").decode("utf-8"))
        old_init = root / "bot" / "__init__.py"
        old = version_in(old_init.read_text(encoding="utf-8")) if old_init.exists() else "none"
        say(f"installing v{new} over v{old} ({len(names)} files)")

        db = root / "data" / "bot.db"
        copy = root / "data" / f"bot-before-{new}.db"
        if backup_db(db, copy):
            say(f"database copied: {copy.relative_to(root)} ({copy.stat().st_size} bytes)")
        else:
            say("no database yet: nothing to copy")

        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
        snapshot = take_snapshot(root, names, root / "rollback" / f"{old}-{stamp}")
        say(f"old code kept: {snapshot.folder.relative_to(root)} ({len(snapshot.saved)} files; {len(snapshot.added)} new)")
        requirements_before = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""
        extract(archive, names, root)
    say("unpacked")

    def go_back(why: str) -> None:
        say(f"going back to v{old}: {why}")
        restore(root, snapshot)

    try:
        checked = _check(root, python, requirements_before, done_wishes, run)
    except Exception as exc:  # a timeout or anything unforeseen: the old code goes back, the bot is left alone
        checked = f"{type(exc).__name__}: {exc}"
    if checked:
        go_back(checked)
        say("RESULT: not installed; the running bot was not touched")
        return 1

    log = root / "logs" / "bot.log"
    offset = log_size(log)
    say(f"restarting ({how} {name})")
    try:
        restart(how, name, root, run, sleep)
        clean, lines = watch_log(log, offset, wait, clock, sleep)
    except Exception as exc:
        clean, lines = False, f"{type(exc).__name__}: {exc}"
    if clean:
        say(lines)
        say(f"RESULT: v{new} is running")
        return 0

    say(lines)
    go_back("the new version did not start cleanly")
    offset = log_size(log)
    try:
        restart(how, name, root, run, sleep)
        clean, lines = watch_log(log, offset, wait, clock, sleep)
    except Exception as exc:
        clean, lines = False, f"{type(exc).__name__}: {exc}"
    say(lines)
    say(f"RESULT: v{new} did not start; back on v{old}, which " + ("is running" if clean else "did not start cleanly either: look at logs/bot.log"))
    return 2


def _same_lines(data: bytes) -> bytes:
    """Line endings left out of a comparison: a file written on Windows ends its lines with CR LF."""
    return data.replace(b"\r\n", b"\n")


def _check(root: Path, python: str, requirements_before: bytes, done_wishes: str, run: Callable) -> str:
    """The packages and the tests of the unpacked code. "" when all is well, else why not."""
    if _same_lines((root / "requirements.txt").read_bytes()) != _same_lines(requirements_before):
        say("requirements.txt changed: installing packages")
        installed = run([python, "-m", "pip", "install", "-r", "requirements.txt"], timeout=900)
        if installed.returncode != 0:
            say(_tail(installed.stdout or "") + "\n" + _tail(installed.stderr or ""))
            return "pip install failed"
    # Only tests/: the old code kept in rollback/ has a tests/ folder of its own, and the two would collide.
    tests = run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests", "--ignore=rollback"], timeout=900)
    summary = (tests.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
    say(f"tests: {summary[0]}")
    if tests.returncode != 0:
        say(_tail(tests.stdout or "", 30))
        return "tests failed"
    if done_wishes:
        marked = run([python, "-m", "bot", "wishes", "--done", done_wishes], timeout=120)
        shown = (marked.stdout or "").strip()
        if marked.returncode != 0:
            say(f"requests {done_wishes} not marked (exit {marked.returncode}): {shown or _tail(marked.stderr or '')}")
        else:
            say(f"requests marked done: {shown or done_wishes}")
    return ""


def main(argv: list[str] | None = None) -> int:
    _speak_utf8()
    parser = argparse.ArgumentParser(prog="python -m bot.deploy", description="Install a new version, with a way back.")
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--restart", default="schtasks" if os.name == "nt" else "systemd", choices=("schtasks", "systemd", "none"))
    parser.add_argument("--name", default="", help="the scheduled task or systemd service (FeishuTradingBot / feishu-trading-bot)")
    parser.add_argument("--wait", type=float, default=150, help="seconds to wait for a clean start")
    parser.add_argument("--done-wishes", default="", help="request numbers this version did, e.g. 3,4")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    name = args.name or ("FeishuTradingBot" if args.restart == "schtasks" else "feishu-trading-bot")
    try:
        return deploy(root, args.zip, args.sha256, args.restart, name, args.wait, args.done_wishes)
    except DeployError as exc:
        say(f"RESULT: not installed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
