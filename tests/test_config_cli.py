from __future__ import annotations

import os
from datetime import time
from pathlib import Path

import pytest

from bot.__main__ import main
from bot.config import load_config, parse_weekly
from bot.okx_client import OkxNetworkError

PREFIXES = ("FEISHU_", "OKX_", "MODEL_", "EFFORT_", "ALERT_", "BRIEF_", "GROUP_", "TRADE_", "TRADING", "TRACK_", "HEARTBEAT_", "BACKUP_")
NAMES = (
    "OWNER_OPEN_ID", "CO_OWNER_OPEN_IDS", "WATCH_INSTRUMENTS", "ANTHROPIC_API_KEY", "TIMEZONE", "POLL_SECONDS", "SNAPSHOT_MINUTES",
    "WEEKLY_REVIEW", "DB_PATH", "LOG_DIR", "WEB_SEARCH", "AI_MONTHLY_BUDGET_USD", "KNOWLEDGE_DIR", "MODEL_SHORT", "EFFORT_SHORT",
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
    assert (cfg.effort_daily, cfg.effort_account, cfg.effort_deep) == ("medium", "high", "xhigh")
    assert (cfg.model_watch, cfg.effort_watch) == ("claude-sonnet-5-5", "low")
    assert (cfg.model_short, cfg.effort_short) == ("claude-sonnet-5-5", "low")
    assert cfg.close_review is True
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

    cfg, problems = load_config(write_env(tmp_path, "MODEL_WATCH=claude-haiku-5-5\nEFFORT_WATCH=fast\n"))
    assert cfg.model_watch == "claude-haiku-5-5" and cfg.effort_watch == "low"
    assert any("EFFORT_WATCH" in item for item in problems)

    cfg, problems = load_config(write_env(tmp_path, "MODEL_SHORT=claude-opus-5-5\nEFFORT_SHORT=medium\n"))
    assert (cfg.model_short, cfg.effort_short) == ("claude-opus-5-5", "medium")
    assert not any("EFFORT_SHORT" in item for item in problems)


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


# ------------------------------------------------------------------ placing orders

LIVE_KEYS = "OKX_API_KEY=k\nOKX_SECRET_KEY=s\nOKX_PASSPHRASE=p\n"
DEMO_KEYS = "OKX_DEMO_API_KEY=dk\nOKX_DEMO_SECRET_KEY=ds\nOKX_DEMO_PASSPHRASE=dp\n"


def test_trading_is_off_unless_the_owner_switches_it_on(tmp_path):
    cfg, _ = load_config(write_env(tmp_path, LIVE_KEYS + DEMO_KEYS))
    assert cfg.trading == "off" and not cfg.trading_on and cfg.trading_blocked == ""
    assert cfg.tradable_instruments == ("XAU-USDT-SWAP",)  # the watch list, until the owner names others
    assert (cfg.trade_max_order_usd, cfg.trade_max_position_usd, cfg.trade_max_opens_per_day) == (1000.0, 3000.0, 20)
    assert cfg.trade_daily_loss_limit_usd is None and cfg.trade_margin_mode == "cross"
    assert (cfg.trade_max_deviation_pct, cfg.trade_max_drift_pct, cfg.trade_confirm_seconds) == (1.0, 0.5, 120)
    assert not cfg.trade_require_stop


@pytest.mark.parametrize(
    "text, mode, blocked",
    [
        ("TRADING=off\n" + LIVE_KEYS, "off", ""),
        ("TRADING=\n" + LIVE_KEYS, "off", ""),
        ("TRADING=false\n" + LIVE_KEYS, "off", ""),
        ("TRADING=demo\n" + LIVE_KEYS + DEMO_KEYS, "demo", ""),
        ("TRADING=DEMO\n" + DEMO_KEYS, "demo", ""),
        ("TRADING=demo\nOKX_DEMO=true\n" + LIVE_KEYS, "demo", ""),  # the main keys are demo keys
        ("TRADING=demo\n" + LIVE_KEYS, "off", "需要模拟盘的三项 key"),
        ("TRADING=demo\n" + LIVE_KEYS + "OKX_DEMO_API_KEY=dk\nOKX_DEMO_PASSPHRASE=dp\n", "off", "需要模拟盘的三项 key"),
        ("TRADING=live\n" + LIVE_KEYS, "live", ""),
        ("TRADING=live\n", "off", "需要先填好 OKX 的三项 key"),
        ("TRADING=live\nOKX_DEMO=true\n" + LIVE_KEYS, "off", "OKX_DEMO=true"),
        ("TRADING=yes\n" + LIVE_KEYS, "off", "只能填 off、demo 或 live"),
        ("TRADING=real\n" + LIVE_KEYS, "off", "只能填 off、demo 或 live"),
    ],
)
def test_a_trading_mode_that_cannot_be_honoured_stays_off_and_says_why(tmp_path, text, mode, blocked):
    cfg, problems = load_config(write_env(tmp_path, text))
    assert cfg.trading == mode
    assert blocked in cfg.trading_blocked
    assert bool(cfg.trading_blocked) == any("下单功能保持关闭" in item for item in problems)


def test_trading_limits_come_from_the_file(tmp_path):
    cfg, problems = load_config(
        write_env(
            tmp_path,
            "TRADE_INSTRUMENTS=xau-usdt-swap, BTC-USDT-SWAP\nTRADE_MAX_ORDER_USD=5000\nTRADE_MAX_POSITION_USD=60000\n"
            "TRADE_MAX_OPENS_PER_DAY=5\nTRADE_DAILY_LOSS_LIMIT_USD=300\nTRADE_MAX_DEVIATION_PCT=0.3\nTRADE_MAX_DRIFT_PCT=0.2\n"
            "TRADE_MARGIN_MODE=isolated\nTRADE_CONFIRM_SECONDS=60\nTRADE_REQUIRE_STOP=true\n",
        )
    )
    assert cfg.tradable_instruments == ("XAU-USDT-SWAP", "BTC-USDT-SWAP")
    assert (cfg.trade_max_order_usd, cfg.trade_max_position_usd, cfg.trade_max_opens_per_day) == (5000.0, 60000.0, 5)
    assert (cfg.trade_daily_loss_limit_usd, cfg.trade_max_deviation_pct, cfg.trade_max_drift_pct) == (300.0, 0.3, 0.2)
    assert (cfg.trade_margin_mode, cfg.trade_confirm_seconds, cfg.trade_require_stop) == ("isolated", 60, True)
    assert cfg.trade_max_risk_pct is None
    assert not any("TRADE" in item for item in problems)


def test_the_cap_on_an_opening_stop(tmp_path):
    cfg, problems = load_config(write_env(tmp_path, "TRADE_MAX_RISK_PCT=25\n"))
    assert cfg.trade_max_risk_pct == 25.0 and not any("TRADE" in item for item in problems)


def test_a_cap_over_the_whole_equity_is_not_used(tmp_path):
    cfg, problems = load_config(write_env(tmp_path, "TRADE_MAX_RISK_PCT=150\n"))
    assert cfg.trade_max_risk_pct is None and any("TRADE_MAX_RISK_PCT" in item for item in problems)


def test_mistakes_in_the_trading_limits_fall_back_to_the_cautious_defaults(tmp_path):
    cfg, problems = load_config(
        write_env(tmp_path, "TRADE_MAX_ORDER_USD=lots\nTRADE_MAX_POSITION_USD=-5\nTRADE_MARGIN_MODE=both\nTRADE_CONFIRM_SECONDS=1\n")
    )
    assert (cfg.trade_max_order_usd, cfg.trade_max_position_usd, cfg.trade_margin_mode) == (1000.0, 3000.0, "cross")
    assert cfg.trade_confirm_seconds == 20  # never so short that a code cannot be typed
    for name in ("TRADE_MAX_ORDER_USD", "TRADE_MAX_POSITION_USD", "TRADE_MARGIN_MODE"):
        assert any(name in item for item in problems)


def test_the_demo_keys_stay_out_of_the_config_repr(tmp_path):
    cfg, _ = load_config(write_env(tmp_path, "OKX_DEMO_API_KEY=demo-key\nOKX_DEMO_SECRET_KEY=demo-secret\nOKX_DEMO_PASSPHRASE=demo-pass\n"))
    assert cfg.has_okx_demo_keys
    for secret in ("demo-key", "demo-secret", "demo-pass"):
        assert secret not in repr(cfg)


def test_the_order_client_exists_only_when_trading_is_on(tmp_path):
    from bot.app import build_demo_trader, build_okx, build_trader
    from bot.okx_trade import OkxTradeClient

    off, _ = load_config(write_env(tmp_path, LIVE_KEYS + DEMO_KEYS))
    assert build_trader(off) is None
    assert not isinstance(build_okx(off), OkxTradeClient)  # the client the rest of the bot uses cannot place orders

    for name in list(os.environ):
        if name.startswith(PREFIXES):
            del os.environ[name]
    demo, _ = load_config(write_env(tmp_path, "TRADING=demo\n" + LIVE_KEYS + DEMO_KEYS))
    trader = build_trader(demo)
    assert isinstance(trader, OkxTradeClient)
    assert trader._demo and trader._api_key == "dk"  # demo orders never carry the live key
    assert not build_okx(demo)._demo and build_okx(demo)._api_key == "k"  # the monitor keeps watching the real account

    for name in list(os.environ):
        if name.startswith(PREFIXES):
            del os.environ[name]
    live, _ = load_config(write_env(tmp_path, "TRADING=live\n" + LIVE_KEYS + DEMO_KEYS))
    trader = build_trader(live)
    assert not trader._demo and trader._api_key == "k"
    rehearsal = build_demo_trader(live)  # while live, the rehearsal has a client of its own on the demo account
    assert rehearsal.demo and rehearsal._api_key == "dk"
    assert build_demo_trader(demo) is None and build_demo_trader(off) is None

    for name in list(os.environ):
        if name.startswith(PREFIXES):
            del os.environ[name]
    live_only, _ = load_config(write_env(tmp_path, "TRADING=live\n" + LIVE_KEYS))
    assert build_demo_trader(live_only) is None


def test_trading_command_prints_the_setup_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("bot.app.build_okx", lambda cfg: OnlineOkx())
    monkeypatch.chdir(tmp_path)
    assert main(["--env", str(write_env(tmp_path, LIVE_KEYS)), "trading"]) == 0
    output = capsys.readouterr().out
    for expected in ("下单功能：已关闭", "允许下单的品种：XAU-USDT-SWAP", "单笔上限 1,000 USDT", "交易权限：有", "提现权限：没有", "已绑定 IP"):
        assert expected in output
    assert "203.0.113.7" not in output and "**" not in output
    assert not (tmp_path / "data").exists()


# --------------------------------------------------------------- track record


def test_track_record_settings(tmp_path):
    cfg, problems = load_config(write_env(tmp_path, ""))
    assert cfg.track_record is True and cfg.track_window_hours == 72 and not any("TRACK" in problem for problem in problems)
    for name in list(os.environ):
        if name.startswith(PREFIXES):
            del os.environ[name]
    cfg, _ = load_config(write_env(tmp_path, "TRACK_RECORD=false\nTRACK_WINDOW_HOURS=500\n"))
    assert cfg.track_record is False and cfg.track_window_hours == 72  # OKX's candles reach back 75 hours
    for name in list(os.environ):
        if name.startswith(PREFIXES):
            del os.environ[name]
    cfg, _ = load_config(write_env(tmp_path, "TRACK_WINDOW_HOURS=1\n"))
    assert cfg.track_window_hours == 4


def test_levels_are_kept_with_the_price_of_that_moment(tmp_path):
    from bot.app import build_ai
    from bot.journal import Journal
    from bot.okx_client import OkxError
    from bot.track import Levels, Scenario

    cfg, _ = load_config(write_env(tmp_path, ""))
    journal = Journal(":memory:")
    okx = OnlineOkx()
    analyst, _, _ = build_ai(cfg, okx, journal)
    levels = Levels("XAU-USDT-SWAP", "long", (Scenario("long", 2700.0, 2750.0, 2660.0),))
    analyst._on_levels("question", "黄金怎么看" * 100, levels)
    [analysis] = journal.unscored_analyses()
    assert analysis["purpose"] == "question" and analysis["bias"] == "long" and analysis["ref_px"] == okx.ticker("XAU-USDT-SWAP").last
    assert len(analysis["question"]) == 120
    [call] = journal.open_calls()
    assert (call["side"], call["trigger_px"], call["target_px"], call["invalid_px"]) == ("long", 2700.0, 2750.0, 2660.0)

    okx.market_error = OkxError("51001", "Instrument ID does not exist")
    analyst._on_levels("question", "", Levels("NOPE-USDT-SWAP", "long", ()))
    assert len(journal.unscored_analyses()) == 1  # an instrument OKX cannot price is not kept


def test_record_command_prints_the_card_and_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    env = write_env(tmp_path, "")
    assert main(["--env", str(env), "record"]) == 0
    assert "还没有运行过" in capsys.readouterr().out and not (tmp_path / "data").exists()

    from bot.journal import Journal

    Journal(tmp_path / "data" / "bot.db").record_analysis("question", "XAU-USDT-SWAP", "long", 3880.0, [])
    assert main(["--env", str(env), "record", "7"]) == 0
    output = capsys.readouterr().out
    assert "最近 7 天" in output and "记录了 1 次带点位的分析" in output and "**" not in output


# --------------------------------------------------------------------- upkeep


def test_heartbeat_and_backup_settings(tmp_path):
    cfg, problems = load_config(write_env(tmp_path, ""))
    assert cfg.heartbeat_url == "" and cfg.backup_keep_days == 14
    for name in list(os.environ):
        if name.startswith(PREFIXES):
            del os.environ[name]
    cfg, problems = load_config(write_env(tmp_path, "HEARTBEAT_URL=https://hc.example/ping/abc\nBACKUP_KEEP_DAYS=0\n"))
    assert cfg.heartbeat_url == "https://hc.example/ping/abc" and cfg.backup_keep_days == 0
    assert "hc.example" not in repr(cfg)  # the address works like a password for the watcher
    for name in list(os.environ):
        if name.startswith(PREFIXES):
            del os.environ[name]
    cfg, problems = load_config(write_env(tmp_path, "HEARTBEAT_URL=ftp://somewhere\n"))
    assert cfg.heartbeat_url == "" and any("HEARTBEAT_URL" in problem for problem in problems)


def actions_from(tmp_path, line: str):
    os.environ.pop("TRADE_ACTIONS", None)  # a file never overrides what an earlier one put in the environment
    cfg, problems = load_config(write_env(tmp_path, line))
    return cfg.trade_actions, [item for item in problems if "TRADE_ACTIONS" in item]


def test_every_order_action_is_open_unless_the_file_narrows_them(tmp_path):
    everything = ("stops", "close", "cancel", "limit", "open")
    assert actions_from(tmp_path, LIVE_KEYS) == (everything, [])
    assert actions_from(tmp_path, "TRADE_ACTIONS=all\n") == (everything, [])
    assert actions_from(tmp_path, "TRADE_ACTIONS=stops\n") == (("stops",), [])
    assert actions_from(tmp_path, "TRADE_ACTIONS= Close , STOPS，cancel\n") == (("stops", "close", "cancel"), [])  # one fixed order
    assert actions_from(tmp_path, "TRADE_ACTIONS=none\n") == ((), [])


def test_a_misspelt_order_action_opens_nothing(tmp_path):
    actions, problems = actions_from(tmp_path, "TRADE_ACTIONS=stops,opne,everything\n")
    assert actions == ("stops",) and "看不懂的词：opne、everything" in problems[0]
    actions, problems = actions_from(tmp_path, "TRADE_ACTIONS=stop\n")  # the word is "stops"
    assert actions == () and len(problems) == 1


def test_account_command_prints_what_the_ai_sees_and_the_field_names_only(tmp_path, monkeypatch, capsys):
    from .helpers import make_state
    from .test_book import MANUAL_LIMIT, MANUAL_TRIGGER, Gold

    okx = Gold(make_state())
    okx.open_orders, okx.algos = [MANUAL_LIMIT], {"trigger": [MANUAL_TRIGGER]}
    monkeypatch.setattr("bot.app.build_okx", lambda cfg: okx)
    monkeypatch.chdir(tmp_path)
    assert main(["--env", str(write_env(tmp_path, LIVE_KEYS)), "account", "--fields"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("账户权益") and "限价单 买入 5 XAU" in output and "计划委托 买入 2 XAU" in output
    assert "[fields] limit: accFillSz, attachAlgoOrds, cTime" in output and "| attachAlgoOrds: [['slOrdPx'" in output
    assert "[fields] trigger: algoClOrdId, algoId" in output and "3905" not in output.split("[fields]", 1)[1]
    assert "这段时间没有平仓记录" in output and not (tmp_path / "data").exists()


def test_requests_are_marked_done_with_the_version_from_the_command_line(tmp_path, capsys):
    from bot import __version__
    from bot.journal import Journal

    db = tmp_path / "bot.db"
    env = tmp_path / "test.env"
    env.write_text(f"DB_PATH={db}\n", encoding="utf-8")
    journal = Journal(db)
    journal.add_wish("分档挂单", source="owner", chat="oc", ts=1)
    journal.add_wish("突破单", source="owner", chat="oc", ts=2)
    assert main(["--env", str(env), "wishes", "--done", "1,2"]) == 0
    assert [w["note"] for w in journal.wishes(0, "done")] == [f"v{__version__}"] * 2
    assert main(["--env", str(env), "wishes", "--drop", "7"]) == 1
    assert "#7: not found" in capsys.readouterr().out


def test_co_owners_are_read_from_the_file(tmp_path):
    cfg, _ = load_config(write_env(tmp_path, "OWNER_OPEN_ID=ou_1\nCO_OWNER_OPEN_IDS= ou_2 , ou_3,ou_2,\n"))
    assert cfg.co_owner_open_ids == ("ou_2", "ou_3")
    assert cfg.is_owner("ou_1") and cfg.is_owner("ou_3") and not cfg.is_owner("ou_4") and not cfg.is_owner("")
    os.environ.pop("CO_OWNER_OPEN_IDS", None)  # loading put it in the environment
    cfg, _ = load_config(write_env(tmp_path, "OWNER_OPEN_ID=ou_1\nCO_OWNER_OPEN_IDS=\n"))
    assert cfg.co_owner_open_ids == () and not cfg.is_owner("")
