from __future__ import annotations

import os
from datetime import time
from pathlib import Path

import pytest

from bot.__main__ import main
from bot.config import load_config, parse_weekly
from bot.okx_client import OkxNetworkError

PREFIXES = ("FEISHU_", "OKX_", "MODEL_", "EFFORT_", "ALERT_", "BRIEF_", "GROUP_")
NAMES = (
    "OWNER_OPEN_ID", "WATCH_INSTRUMENTS", "ANTHROPIC_API_KEY", "TIMEZONE", "POLL_SECONDS", "SNAPSHOT_MINUTES",
    "WEEKLY_REVIEW", "DB_PATH", "LOG_DIR", "WEB_SEARCH", "AI_MONTHLY_BUDGET_USD", "KNOWLEDGE_DIR",
)  # fmt: skip
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_env():
    """Each test starts without any bot settings, and leaves none behind."""
    saved = dict(os.environ)
    for name in list(os.environ):
        if name.startswith(PREFIXES) or name in NAMES:
            del os.environ[name]
    yield
    os.environ.clear()
    os.environ.update(saved)


def write_env(tmp_path, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / ".env"
    path.write_bytes(text.encode(encoding))
    return path


def test_empty_configuration_lists_what_is_missing():
    cfg, problems = load_config(None)
    assert len(problems) == 4
    assert any("FEISHU_APP_ID" in item for item in problems)
    assert any("OKX_API_KEY" in item for item in problems)
    assert any("ANTHROPIC_API_KEY" in item for item in problems)
    assert any("/whoami" in item for item in problems)
    assert not cfg.has_okx_keys
    assert cfg.watch_instruments == ("XAU-USDT-SWAP",)
    assert (cfg.model_quick, cfg.model_daily, cfg.model_deep) == ("claude-haiku-5-5", "claude-opus-5-5", "claude-opus-5-5")
    assert (cfg.effort_daily, cfg.effort_deep) == ("medium", "high")
    assert cfg.web_search and cfg.ai_monthly_budget_usd == 180.0 and cfg.knowledge_dir == Path("knowledge")
    assert (cfg.brief_morning, cfg.brief_evening, cfg.weekly_review) == (time(6, 0), time(14, 0), (6, time(17, 0)))
    assert str(cfg.tz) == "America/Los_Angeles"
    assert cfg.group_show_pnl and cfg.group_owner_only  # the owner's chosen defaults


def test_complete_env_file(tmp_path):
    path = write_env(
        tmp_path,
        "# 注释\nFEISHU_APP_ID=cli_a\nFEISHU_APP_SECRET=s\nFEISHU_DOMAIN=lark\nOWNER_OPEN_ID=ou_1\nGROUP_CHAT_ID=oc_1\n"
        "OKX_API_KEY=k\nOKX_SECRET_KEY=s2\nOKX_PASSPHRASE=p\nOKX_DEMO=true\nANTHROPIC_API_KEY=sk-ant-x\n"
        "WATCH_INSTRUMENTS=xau-usdt-swap, BTC-USDT-SWAP\nBRIEF_MORNING=off\nBRIEF_EVENING=21:30\nWEEKLY_REVIEW=fri 16:00\n"
        "ALERT_MARGIN_RATIO=off\nALERT_DAILY_LOSS_USD=500\nGROUP_SHOW_PNL=FALSE\nGROUP_OWNER_ONLY=no\nPOLL_SECONDS=1\n",
    )
    cfg, problems = load_config(path)
    assert problems == []
    assert cfg.feishu_domain == "https://open.larksuite.com"
    assert cfg.okx_demo and cfg.has_okx_keys
    assert not cfg.group_show_pnl and not cfg.group_owner_only
    assert cfg.watch_instruments == ("XAU-USDT-SWAP", "BTC-USDT-SWAP")
    assert cfg.brief_morning is None and cfg.brief_evening == time(21, 30) and cfg.weekly_review == (4, time(16, 0))
    assert cfg.alert_margin_ratio is None and cfg.alert_liq_distance_pct == 5.0 and cfg.alert_daily_loss_usd == 500.0
    assert cfg.poll_seconds == 5  # never faster than every 5 seconds


def test_ai_settings_can_be_changed(tmp_path):
    path = write_env(tmp_path, "MODEL_DAILY=claude-sonnet-5-5\nEFFORT_DAILY=LOW\nEFFORT_DEEP=max\nWEB_SEARCH=false\nAI_MONTHLY_BUDGET_USD=off\nKNOWLEDGE_DIR=kb\n")
    cfg, _ = load_config(path)
    assert (cfg.model_daily, cfg.effort_daily, cfg.effort_deep) == ("claude-sonnet-5-5", "low", "max")
    assert not cfg.web_search and cfg.ai_monthly_budget_usd is None and cfg.knowledge_dir == Path("kb")


def test_mistakes_in_the_ai_settings_are_explained(tmp_path):
    path = write_env(tmp_path, "EFFORT_DAILY=turbo\nAI_MONTHLY_BUDGET_USD=lots\n")
    cfg, problems = load_config(path)
    assert cfg.effort_daily == "medium" and cfg.ai_monthly_budget_usd == 180.0
    assert any("EFFORT_DAILY" in item for item in problems) and any("AI_MONTHLY_BUDGET_USD" in item for item in problems)


def test_values_left_empty_fall_back_to_defaults(tmp_path):
    path = write_env(tmp_path, "TIMEZONE=\nMODEL_QUICK=\nBRIEF_MORNING=\nWATCH_INSTRUMENTS=\nDB_PATH=\nALERT_DAILY_LOSS_USD=\nFEISHU_DOMAIN=\n")
    cfg, problems = load_config(path)
    assert len(problems) == 4  # only the four missing credentials
    assert cfg.timezone == "America/Los_Angeles" and cfg.model_quick == "claude-haiku-5-5"
    assert cfg.brief_morning == time(6, 0) and cfg.db_path == Path("data/bot.db") and cfg.alert_daily_loss_usd is None


def test_mistakes_are_explained_and_do_not_crash(tmp_path):
    path = write_env(tmp_path, "FEISHU_DOMAIN=dingding\nTIMEZONE=Mars/Olympus\nBRIEF_MORNING=6am\nWEEKLY_REVIEW=someday\nPOLL_SECONDS=fast\n")
    cfg, problems = load_config(path)
    text = "\n".join(problems)
    for name in ("FEISHU_DOMAIN", "TIMEZONE", "BRIEF_MORNING", "WEEKLY_REVIEW", "POLL_SECONDS"):
        assert name in text
    assert cfg.timezone == "America/Los_Angeles" and cfg.poll_seconds == 30 and cfg.brief_morning is None


def test_env_file_saved_by_windows_notepad(tmp_path):
    cfg, _ = load_config(write_env(tmp_path, "FEISHU_APP_ID=cli_bom\n# 中文注释\nOWNER_OPEN_ID=ou_1\n", "utf-8-sig"))
    assert cfg.feishu_app_id == "cli_bom" and cfg.owner_open_id == "ou_1"


def test_env_file_in_an_unreadable_encoding_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("locale.getpreferredencoding", lambda do_setlocale=True: "utf-8")
    _, problems = load_config(write_env(tmp_path, "# 中文注释\nFEISHU_APP_ID=cli_gbk\n", "gbk"))
    assert any("UTF-8" in item for item in problems)


def test_real_environment_wins_over_the_file(tmp_path):
    os.environ["FEISHU_APP_ID"] = "cli_from_environment"
    cfg, _ = load_config(write_env(tmp_path, "FEISHU_APP_ID=cli_from_file\n"))
    assert cfg.feishu_app_id == "cli_from_environment"


def test_secrets_are_not_in_the_config_repr(tmp_path):
    path = write_env(tmp_path, "FEISHU_APP_SECRET=feishu-secret\nOKX_API_KEY=okx-key\nOKX_SECRET_KEY=okx-secret\nOKX_PASSPHRASE=okx-pass\nANTHROPIC_API_KEY=sk-ant-key\n")
    cfg, _ = load_config(path)
    assert cfg.feishu_app_secret == "feishu-secret" and cfg.anthropic_api_key == "sk-ant-key"
    for secret in ("feishu-secret", "okx-key", "okx-secret", "okx-pass", "sk-ant-key"):
        assert secret not in repr(cfg)


def test_parse_weekly():
    assert parse_weekly("SUN 17:00") == (6, time(17, 0))
    assert parse_weekly("mon 08:05") == (0, time(8, 5))
    with pytest.raises(ValueError):
        parse_weekly("FUNDAY 10:00")


def test_env_example_is_complete_and_holds_no_secrets(tmp_path):
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    settings = dict(line.split("=", 1) for line in example.splitlines() if line and not line.startswith("#"))
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "OWNER_OPEN_ID", "GROUP_CHAT_ID", "OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "ANTHROPIC_API_KEY"):
        assert settings[name] == ""
    cfg, problems = load_config(write_env(tmp_path, example))
    assert len(problems) == 4  # the template parses cleanly; only credentials are missing
    assert cfg.okx_base_url == "https://www.okx.com" and cfg.group_daily_question_limit == 30


def test_env_file_is_ignored_by_git():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored and "data/" in ignored and "logs/" in ignored


def test_selfcheck_with_nothing_configured(tmp_path, monkeypatch, capsys):
    class OfflineOkx:
        def sync_clock(self):
            raise OkxNetworkError("连不上 OKX：ConnectionError")

    monkeypatch.setattr("bot.app.build_okx", lambda cfg: OfflineOkx())
    code = main(["--env", str(tmp_path / "missing.env"), "selfcheck"])
    output = capsys.readouterr().out
    assert code == 1
    assert "有 4 项没通过" in output
    for expected in ("OKX 行情读取失败", "还没填 OKX 的三个 key", "还没有配置 Claude 的 API key", "还没填飞书的 App ID"):
        assert expected in output


class OnlineOkx:
    """Public market data works; there are no account keys."""

    def __new__(cls):
        from .helpers import FakeOkx

        okx = FakeOkx()
        okx.sync_clock = lambda: 0.12
        return okx


def test_selfcheck_shows_that_other_instruments_can_be_looked_up(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("bot.app.build_okx", lambda cfg: OnlineOkx())
    monkeypatch.chdir(tmp_path)
    code = main(["--env", str(tmp_path / "missing.env"), "selfcheck"])
    output = capsys.readouterr().out
    assert code == 1  # the keys are still missing
    for expected in (
        "XAU-USDT-SWAP 最新价 2,650.00",
        "其他品种也能查：BTC 对应 BTC-USDT-SWAP",
        "其他品种也能查：以太坊 对应 ETH-USDT-SWAP",
        "资料库文件夹 knowledge，现有 0 个文件",
    ):
        assert expected in output
    assert not (tmp_path / "data").exists()  # a self-check writes nothing to disk


def test_quote_command_prints_prices_without_any_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("bot.app.build_okx", lambda cfg: OnlineOkx())
    code = main(["--env", str(tmp_path / "missing.env"), "quote", "BTC", "黄金", "AAPL"])
    output = capsys.readouterr().out
    assert code == 1  # one of the three is not on OKX
    assert "[OK] BTC：BTC-USDT-SWAP 2,650.00（24 小时 +1.92%" in output
    assert "[OK] 黄金：XAU-USDT-SWAP 2,650.00" in output and "[X] OKX 上没有找到 AAPL" in output


def test_ask_command_without_a_claude_key_explains_itself(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("bot.app.build_okx", lambda cfg: OnlineOkx())
    monkeypatch.chdir(tmp_path)
    code = main(["--env", str(tmp_path / "missing.env"), "ask", "BTC 现在怎么看"])
    output = capsys.readouterr().out
    assert code == 1 and "还没有配置 Claude 的 API key" in output and "[用量]" in output and "约 0.000 美元" in output


def test_run_refuses_to_start_without_feishu_credentials(tmp_path, capsys):
    assert main(["--env", str(tmp_path / "missing.env"), "run"]) == 1
    assert "selfcheck" in capsys.readouterr().out
