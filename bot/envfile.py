"""Write values into the .env file without ever showing them.

Used by `python -m bot setenv`: the owner pipes `NAME=value` lines in (for
example over SSH from their own computer), and only the *names* are printed
back. Comments and the order of the file are kept.
"""
from __future__ import annotations

import locale
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

NAME_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_\-./:+=@,]+")
_ASSIGNMENT = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def quote_value(value: str) -> str:
    """How `value` has to be written so that python-dotenv reads it back unchanged."""
    if not value or _PLAIN_VALUE.fullmatch(value):
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


# --- Does each key sit in the right place? -----------------------------------
#
# The keys are pasted in by hand, one after another, and it is easy to paste
# the right key into the wrong line. A key in the wrong line would be sent to
# the wrong company (a Feishu secret to OKX, say), so the bot looks at the
# *shape* of each value before it uses it. The values themselves are never
# shown: every message below holds names only.

SECRET_NAMES = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_PASSPHRASE",
    "ANTHROPIC_API_KEY",
)

_KIND_PATTERNS = (
    ("feishu_app_id", re.compile(r"cli_[0-9A-Za-z_]+")),
    ("anthropic_key", re.compile(r"sk-ant-\S+")),
    ("okx_api_key", re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")),
    ("okx_secret", re.compile(r"[0-9A-F]{32}")),
    ("feishu_secret", re.compile(r"[0-9A-Za-z]{32}")),
)
KIND_LABELS = {
    "feishu_app_id": "飞书的 App ID",
    "feishu_secret": "飞书的 App Secret",
    "okx_api_key": "OKX 的 API key",
    "okx_secret": "OKX 的 Secret key",
    "anthropic_key": "Claude 的 API key",
}
_EXPECTED_KIND = {
    "FEISHU_APP_ID": "feishu_app_id",
    "FEISHU_APP_SECRET": "feishu_secret",
    "OKX_API_KEY": "okx_api_key",
    "OKX_SECRET_KEY": "okx_secret",
    "OKX_PASSPHRASE": "other",  # the owner makes this one up, so it has no fixed shape
    "ANTHROPIC_API_KEY": "anthropic_key",
}
# Shapes that cannot be mistaken for anything else: seeing one in another line is a sure sign.
_DISTINCTIVE_KINDS = frozenset({"feishu_app_id", "anthropic_key", "okx_api_key"})
# Lines whose real value always has its own prefix.
_STRICT_NAMES = {
    "FEISHU_APP_ID": "飞书的 App ID 应该以 cli_ 开头",
    "ANTHROPIC_API_KEY": "Claude 的 API key 应该以 sk-ant- 开头",
}


def value_kind(value: str) -> str:
    """Which key `value` looks like, judged by its shape alone. "other" when it looks like none."""
    for kind, pattern in _KIND_PATTERNS:
        if pattern.fullmatch(value):
            return kind
    return "other"


@dataclass(frozen=True)
class ShapeFinding:
    name: str
    status: str  # "ok" | "empty" | "wrong" | "unsure"
    note: str = ""  # names and plain words only, never a value


def check_secret_shapes(values: Mapping[str, str]) -> list[ShapeFinding]:
    """Say, for each key, whether the value in its line looks like it belongs there.

    "wrong" is only said when it is certain: the value clearly is another
    kind of key, or the same value sits in several lines. A shape that is
    merely unfamiliar is "unsure" and is still used, so that a key whose
    format changed one day does not get locked out.
    """
    cleaned = {name: (values.get(name) or "").strip() for name in SECRET_NAMES}
    same_value: dict[str, list[str]] = {}
    for name, value in cleaned.items():
        if value:
            same_value.setdefault(value, []).append(name)

    findings: list[ShapeFinding] = []
    for name in SECRET_NAMES:
        value = cleaned[name]
        if not value:
            findings.append(ShapeFinding(name, "empty"))
            continue
        kind = value_kind(value)
        expected = _EXPECTED_KIND[name]
        label = KIND_LABELS.get(kind)
        looks_like = f"看起来像{label}" if label else ""
        twins = [other for other in same_value[value] if other != name]
        if twins:
            # One value in several lines: it can belong to one of them at most.
            if kind == expected and kind != "other":
                findings.append(ShapeFinding(name, "ok"))
            else:
                note = f"和 {'、'.join(twins)} 填的是同一个值"
                findings.append(ShapeFinding(name, "wrong", f"{note}，{looks_like}" if looks_like else note))
        elif kind == expected:
            findings.append(ShapeFinding(name, "ok"))
        elif kind in _DISTINCTIVE_KINDS:
            findings.append(ShapeFinding(name, "wrong", looks_like))
        elif name in _STRICT_NAMES:
            note = _STRICT_NAMES[name]
            findings.append(ShapeFinding(name, "wrong", f"{note}，这个{looks_like}" if looks_like else note))
        elif looks_like:
            findings.append(ShapeFinding(name, "unsure", looks_like))
        else:
            findings.append(ShapeFinding(name, "unsure", f"格式不太像{KIND_LABELS[expected]}"))
    return findings
