from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from bot.__main__ import main
from bot.config import load_config
from bot.envfile import (
    OPTIONAL_SECRET_NAMES,
    SECRET_NAMES,
    check_secret_shapes,
    parse_assignments,
    quote_value,
    update_env_text,
    value_kind,
    write_env_values,
)

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


# --- keys pasted into the wrong line ------------------------------------------

APP_ID = "cli_a1b2c3d4e5f60718"
FEISHU_SECRET = "AbCdEfGh1234567890abcdefGHIJKLmn"
OKX_KEY = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OKX_SECRET = "0123456789ABCDEF0123456789ABCDEF"
PASSPHRASE = "My-Pass#2026"
CLAUDE_KEY = "sk-ant-api03-AbC_dEf-123"
MAIN_NAMES = tuple(name for name in SECRET_NAMES if name not in OPTIONAL_SECRET_NAMES)
ALL_RIGHT = dict(zip(MAIN_NAMES, (APP_ID, FEISHU_SECRET, OKX_KEY, OKX_SECRET, PASSPHRASE, CLAUDE_KEY)))
DEMO_KEY = "9f8e7d6c-5b4a-4c3d-9e2f-1a0b9c8d7e6f"
DEMO_SECRET = "FEDCBA9876543210FEDCBA9876543210"


def statuses(values: dict[str, str], optional: bool = False) -> dict[str, str]:
    """The finding for each key. The demo-trading keys are left out unless asked for."""
    return {
        finding.name: finding.status
        for finding in check_secret_shapes(values)
        if optional or finding.name not in OPTIONAL_SECRET_NAMES
    }


def write_keys(tmp_path, values: dict[str, str]) -> Path:
    path = tmp_path / ".env"
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
    return path


def clear_key_environment() -> None:
    for name in SECRET_NAMES:
        os.environ.pop(name, None)


def test_value_kind_goes_by_shape():
    assert value_kind(APP_ID) == "feishu_app_id"
    assert value_kind(FEISHU_SECRET) == "feishu_secret"
    assert value_kind(OKX_KEY) == "okx_api_key"
    assert value_kind(OKX_SECRET) == "okx_secret"
    assert value_kind(CLAUDE_KEY) == "anthropic_key"
    assert value_kind(PASSPHRASE) == "other"


def test_keys_in_their_own_lines_are_all_ok():
    assert set(statuses(ALL_RIGHT).values()) == {"ok"}


def test_nothing_filled_in_is_empty_not_wrong():
    assert set(statuses({}).values()) == {"empty"}
    assert statuses({"OKX_API_KEY": "  "})["OKX_API_KEY"] == "empty"


def test_two_feishu_values_pasted_into_every_line():
    values = dict(zip(MAIN_NAMES, (APP_ID, FEISHU_SECRET) * 3))
    assert statuses(values) == {
        "FEISHU_APP_ID": "ok",
        "FEISHU_APP_SECRET": "ok",
        "OKX_API_KEY": "wrong",
        "OKX_SECRET_KEY": "wrong",
        "OKX_PASSPHRASE": "wrong",
        "ANTHROPIC_API_KEY": "wrong",
    }


def test_two_okx_values_pasted_into_every_line():
    values = dict(zip(MAIN_NAMES, (OKX_KEY, OKX_SECRET) * 3))
    assert statuses(values) == {
        "FEISHU_APP_ID": "wrong",
        "FEISHU_APP_SECRET": "wrong",
        "OKX_API_KEY": "ok",
        "OKX_SECRET_KEY": "ok",
        "OKX_PASSPHRASE": "wrong",
        "ANTHROPIC_API_KEY": "wrong",
    }


def test_demo_trading_keys_are_checked_like_the_live_ones():
    demo = {"OKX_DEMO_API_KEY": DEMO_KEY, "OKX_DEMO_SECRET_KEY": DEMO_SECRET, "OKX_DEMO_PASSPHRASE": PASSPHRASE}
    # One passphrase for the live and the demo key is fine: it is the owner's own word for both.
    assert set(statuses({**ALL_RIGHT, **demo}, optional=True).values()) == {"ok", "unsure"} - {"unsure"} | {"ok"}
    found = statuses({**ALL_RIGHT, **demo}, optional=True)
    assert found["OKX_PASSPHRASE"] == "ok" and found["OKX_DEMO_PASSPHRASE"] == "ok"
    # Left empty they are simply empty, and a Claude key in the demo line is caught.
    assert statuses(ALL_RIGHT, optional=True)["OKX_DEMO_API_KEY"] == "empty"
    assert statuses({**ALL_RIGHT, "OKX_DEMO_API_KEY": CLAUDE_KEY}, optional=True)["OKX_DEMO_API_KEY"] == "wrong"
    # The demo secret pasted into the Feishu line is still a mistake there.
    swapped = statuses({**ALL_RIGHT, **demo, "FEISHU_APP_SECRET": DEMO_SECRET}, optional=True)
    assert swapped["FEISHU_APP_SECRET"] == "wrong" and swapped["OKX_DEMO_SECRET_KEY"] == "ok"


def test_the_same_made_up_value_in_two_lines_is_wrong_in_both():
    found = statuses({**ALL_RIGHT, "FEISHU_APP_SECRET": PASSPHRASE})
    assert found["FEISHU_APP_SECRET"] == "wrong" and found["OKX_PASSPHRASE"] == "wrong"


@pytest.mark.parametrize(
    "name, value",
    [
        ("FEISHU_APP_SECRET", CLAUDE_KEY),
        ("OKX_SECRET_KEY", OKX_KEY),
        ("OKX_PASSPHRASE", APP_ID),
        ("OKX_API_KEY", CLAUDE_KEY),
        ("FEISHU_APP_ID", FEISHU_SECRET),
        ("FEISHU_APP_ID", "not-an-app-id"),
        ("ANTHROPIC_API_KEY", OKX_SECRET),
        ("ANTHROPIC_API_KEY", "some-other-key"),
    ],
)
def test_a_key_that_clearly_belongs_elsewhere_is_wrong(name, value):
    assert statuses({name: value})[name] == "wrong"


@pytest.mark.parametrize(
    "name, value",
    [
        ("OKX_API_KEY", "a-key-in-a-format-we-have-not-seen"),
        ("OKX_SECRET_KEY", FEISHU_SECRET),
        ("FEISHU_APP_SECRET", OKX_SECRET),
        ("FEISHU_APP_SECRET", "short"),
        ("OKX_PASSPHRASE", FEISHU_SECRET),
    ],
)
def test_an_unfamiliar_shape_is_only_unsure(name, value):
    assert statuses({name: value})[name] == "unsure"


def test_findings_never_hold_a_value():
    values = dict(zip(SECRET_NAMES, (OKX_KEY, PASSPHRASE, CLAUDE_KEY, FEISHU_SECRET, APP_ID, OKX_SECRET)))
    text = " ".join(f"{finding.name} {finding.status} {finding.note}" for finding in check_secret_shapes(values))
    for value in values.values():
        assert value not in text


def test_load_config_ignores_misplaced_keys_and_says_which(tmp_path):
    path = write_keys(tmp_path, dict(zip(SECRET_NAMES, (APP_ID, FEISHU_SECRET) * 3)))
    clear_key_environment()
    cfg, problems = load_config(path)
    assert cfg.feishu_app_id == APP_ID and cfg.feishu_app_secret == FEISHU_SECRET
    assert (cfg.okx_api_key, cfg.okx_secret_key, cfg.okx_passphrase, cfg.anthropic_api_key) == ("", "", "", "")
    assert not cfg.has_okx_keys
    said = "\n".join(problems)
    for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "ANTHROPIC_API_KEY"):
        assert f"{name} 填错位置了" in said
    assert FEISHU_SECRET not in said and APP_ID not in said


def test_load_config_keeps_keys_of_an_unfamiliar_shape(tmp_path):
    path = write_keys(tmp_path, {**ALL_RIGHT, "OKX_API_KEY": "new-style-key", "OKX_SECRET_KEY": "new-style-secret"})
    clear_key_environment()
    cfg, problems = load_config(path)
    assert cfg.okx_api_key == "new-style-key" and cfg.okx_secret_key == "new-style-secret" and cfg.has_okx_keys
    assert not any("填错位置" in item for item in problems)


def test_checkenv_command_names_the_misplaced_keys_without_showing_them(tmp_path, capsys):
    values = dict(zip(MAIN_NAMES, (APP_ID, FEISHU_SECRET) * 3))
    values["OKX_PASSPHRASE"] = ""
    path = write_keys(tmp_path, values)
    clear_key_environment()

    assert main(["--env", str(path), "checkenv"]) == 1

    output = capsys.readouterr()
    lines = {line.split()[1]: line for line in output.out.splitlines() if line.startswith("[")}
    assert lines["FEISHU_APP_ID"].startswith("[OK]") and lines["FEISHU_APP_SECRET"].startswith("[OK]")
    assert lines["OKX_API_KEY"].startswith("[X]") and lines["ANTHROPIC_API_KEY"].startswith("[X]")
    assert lines["OKX_PASSPHRASE"].startswith("[--]")
    assert "有 3 项" in output.out
    for value in (APP_ID, FEISHU_SECRET):
        assert value not in output.out and value not in output.err


def test_checkenv_command_passes_when_every_key_is_in_place(tmp_path, capsys):
    path = write_keys(tmp_path, ALL_RIGHT)
    clear_key_environment()
    assert main(["--env", str(path), "checkenv"]) == 0
    output = capsys.readouterr().out
    assert output.count("[OK]") == 6
    for value in ALL_RIGHT.values():
        assert value not in output


def test_clearenv_command_empties_only_the_named_lines(tmp_path, capsys):
    path = tmp_path / ".env"
    path.write_text(f"# 说明\nFEISHU_APP_ID={APP_ID}\nOKX_API_KEY={APP_ID}\nOKX_PASSPHRASE='p w'\nTIMEZONE=Asia/Tokyo\n", encoding="utf-8")

    assert main(["--env", str(path), "clearenv", "OKX_API_KEY", "OKX_PASSPHRASE"]) == 0

    assert path.read_text(encoding="utf-8") == f"# 说明\nFEISHU_APP_ID={APP_ID}\nOKX_API_KEY=\nOKX_PASSPHRASE=\nTIMEZONE=Asia/Tokyo\n"
    output = capsys.readouterr().out
    assert "OKX_API_KEY" in output and "OKX_PASSPHRASE" in output and APP_ID not in output
    clear_key_environment()
    cfg, _ = load_config(path)
    assert cfg.feishu_app_id == APP_ID and cfg.okx_api_key == "" and cfg.okx_passphrase == ""


def test_clearenv_command_refuses_odd_names_and_a_missing_file(tmp_path):
    path = tmp_path / ".env"
    assert main(["--env", str(path), "clearenv", "OKX_API_KEY"]) == 1
    path.write_text("OKX_API_KEY=k\n", encoding="utf-8")
    assert main(["--env", str(path), "clearenv", "OKX_API_KEY", "not a name"]) == 1
    assert path.read_text(encoding="utf-8") == "OKX_API_KEY=k\n"


def test_setenv_command_warns_about_a_key_in_the_wrong_line(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    path.write_text(TEMPLATE, encoding="utf-8")
    data = f"FEISHU_APP_ID={APP_ID}\nFEISHU_APP_SECRET={FEISHU_SECRET}\nOKX_PASSPHRASE={FEISHU_SECRET}\n".encode("utf-8")
    monkeypatch.setattr(sys, "stdin", FakeStdin(data))

    assert main(["--env", str(path), "setenv"]) == 0

    output = capsys.readouterr()
    warnings = [line for line in output.out.splitlines() if line.startswith("[X]")]
    assert len(warnings) == 1 and "OKX_PASSPHRASE" in warnings[0] and "FEISHU_APP_SECRET" in warnings[0]
    assert FEISHU_SECRET not in output.out and FEISHU_SECRET not in output.err
