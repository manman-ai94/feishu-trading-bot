"""Write values into the .env file without ever showing them.

Used by `python -m bot setenv`: the owner pipes `NAME=value` lines in (for
example over SSH from their own computer), and only the *names* are printed
back. Comments and the order of the file are kept.
"""
from __future__ import annotations

import locale
import os
import re
from pathlib import Path

NAME_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_\-./:+=@,]+")
_ASSIGNMENT = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def quote_value(value: str) -> str:
    """How `value` has to be written so that python-dotenv reads it back unchanged."""
    if _PLAIN_VALUE.fullmatch(value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def parse_assignments(text: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Split `NAME=value` lines into (updates, skipped_names, bad_lines).

    A line with an empty value means "leave this one as it is". `bad_lines`
    holds only line numbers and names, never the values.
    """
    updates: dict[str, str] = {}
    skipped: list[str] = []
    bad: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not NAME_PATTERN.fullmatch(name):
            bad.append(f"第 {number} 行")
            continue
        value = value.strip()
        if value:
            updates[name] = value
        else:
            skipped.append(name)
    return updates, skipped, bad


def update_env_text(text: str, updates: dict[str, str]) -> str:
    """Return `text` with the given names set. Existing lines are replaced in place."""
    remaining = dict(updates)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) in remaining:
            name = match.group(1)
            lines[index] = f"{name}={quote_value(remaining.pop(name))}"
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{name}={quote_value(value)}" for name, value in remaining.items())
    return "\n".join(lines) + "\n"


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    """Set the values in the file at `path`, creating it if needed. Atomic."""
    text = ""
    if path.exists():
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:  # Notepad may have saved it in the system's own encoding
            text = raw.decode(locale.getpreferredencoding(False), errors="replace")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(update_env_text(text, updates), encoding="utf-8", newline="\n")
    os.replace(temporary, path)
