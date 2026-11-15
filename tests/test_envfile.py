from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from bot.__main__ import main
from bot.config import load_config
from bot.envfile import parse_assignments, quote_value, update_env_text, write_env_values

TEMPLATE = """# 飞书 (Feishu)
FEISHU_APP_ID=
FEISHU_APP_SECRET=
# 国内版飞书填 feishu
FEISHU_DOMAIN=feishu

OKX_PASSPHRASE=
"""

NASTY_VALUES = [
    "plain-Value_123",
    "abc==",
    "with space",
    "hash #inside",
    "#starts-with-hash",
    "single'quote",
    'double"quote',
    "back\\slash",
    "ends-with-backslash\\",
    "dollar${HOME}sign",
    "$HOME",
    "a=b=c",
    "mix '\" \\ # $ ! % ^ & * ( )",
    "中文口令",
]


class FakeStdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


@pytest.fixture(autouse=True)
def clean_env():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.mark.parametrize("value", NASTY_VALUES)
def test_quoted_value_reads_back_unchanged(tmp_path, value):
    path = tmp_path / ".env"
    path.write_text(f"SECRET={quote_value(value)}\nNEXT=1\n", encoding="utf-8")
    values = dotenv_values(path, interpolate=False, encoding="utf-8")
    assert values["SECRET"] == value
    assert values["NEXT"] == "1"


def test_simple_values_stay_unquoted():
    assert quote_value("cli_a1b2c3") == "cli_a1b2c3"
    assert quote_value("sk-ant-api03-AbC_dEf-123==") == "sk-ant-api03-AbC_dEf-123=="


def test_update_replaces_in_place_and_keeps_comments():
    text = update_env_text(TEMPLATE, {"FEISHU_APP_SECRET": "s3cret", "OKX_PASSPHRASE": "p w"})
    lines = text.splitlines()
    assert lines[0] == "# 飞书 (Feishu)"
    assert lines[1] == "FEISHU_APP_ID="
    assert lines[2] == "FEISHU_APP_SECRET=s3cret"
    assert lines[4] == "FEISHU_DOMAIN=feishu"
    assert lines[6] == "OKX_PASSPHRASE='p w'"
    assert len(lines) == len(TEMPLATE.splitlines())


def test_update_appends_names_that_are_not_in_the_file():
    text = update_env_text(TEMPLATE, {"GROUP_CHAT_ID": "oc_123"})
    assert text.endswith("\nGROUP_CHAT_ID=oc_123\n")
    assert text.startswith(TEMPLATE.rstrip("\n"))


def test_update_overwrites_an_existing_value():
    text = update_env_text("OKX_API_KEY=old\n", {"OKX_API_KEY": "new"})
    assert text == "OKX_API_KEY=new\n"


def test_parse_assignments_sorts_lines():
    updates, skipped, bad = parse_assignments(
        "FEISHU_APP_ID = cli_1 \r\nOKX_SECRET_KEY=abc=def==\nOKX_PASSPHRASE=\n# note\n\nnot a line\nlower=1\n"
    )
    assert updates == {"FEISHU_APP_ID": "cli_1", "OKX_SECRET_KEY": "abc=def=="}
    assert skipped == ["OKX_PASSPHRASE"]
    assert bad == ["第 6 行", "第 7 行"]


def test_write_keeps_a_file_saved_in_another_encoding(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_bytes("# 说明\nOKX_API_KEY=\n".encode("gbk"))
    monkeypatch.setattr("bot.envfile.locale.getpreferredencoding", lambda _=False: "gbk")
    write_env_values(path, {"OKX_API_KEY": "k1"})
    assert path.read_text(encoding="utf-8") == "# 说明\nOKX_API_KEY=k1\n"


def test_setenv_command_writes_values_and_prints_only_names(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    path.write_text(TEMPLATE, encoding="utf-8")
    secret = "tOp'Secret #1 ${X}"
    data = f"FEISHU_APP_ID=cli_abc\nFEISHU_APP_SECRET={secret}\nOKX_PASSPHRASE=\n".encode("utf-8")
    monkeypatch.setattr(sys, "stdin", FakeStdin(data))

    assert main(["--env", str(path), "setenv"]) == 0

    output = capsys.readouterr()
    assert "FEISHU_APP_ID" in output.out and "FEISHU_APP_SECRET" in output.out
    assert "OKX_PASSPHRASE" in output.out  # reported as left empty
    assert secret not in output.out and "cli_abc" not in output.out
    assert secret not in output.err
    assert not Path(str(path) + ".tmp").exists()

    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "OKX_PASSPHRASE"):
        os.environ.pop(name, None)
    cfg, _ = load_config(path)
    assert cfg.feishu_app_id == "cli_abc"
    assert cfg.feishu_app_secret == secret
    assert cfg.okx_passphrase == ""


def test_setenv_command_refuses_malformed_input(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    path.write_text(TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", FakeStdin(b"FEISHU_APP_ID=cli_abc\nthis is my secret\n"))

    assert main(["--env", str(path), "setenv"]) == 1

    output = capsys.readouterr()
    assert "this is my secret" not in output.out
    assert path.read_text(encoding="utf-8") == TEMPLATE


def test_setenv_command_with_nothing_to_write(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(sys, "stdin", FakeStdin(b"\n"))
    assert main(["--env", str(path), "setenv"]) == 0
    assert not path.exists()
