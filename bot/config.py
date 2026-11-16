"""Configuration: everything comes from environment variables / the .env file.

Secrets (API keys, app secret) are only ever read from the environment. They
are never logged and never written anywhere by the bot.
"""
from __future__ import annotations

import locale
import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .envfile import SECRET_NAMES, check_secret_shapes

FEISHU_DOMAINS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
OFF_WORDS = ("off", "none", "关")  # how the owner switches a feature off in .env
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # how hard the AI thinks; higher costs more and takes longer
TRADING_MODES = ("off", "demo", "live")
MARGIN_MODES = ("cross", "isolated")


def _get(name: str, default: str = "") -> str:
    """A value left empty in .env counts as "not set" and gets the default."""
    return os.environ.get(name, "").strip() or default


def _load_env_file(path: Path, problems: list[str]) -> None:
    # Notepad on Windows may save with a BOM, or in the system's own encoding.
    for encoding in ("utf-8-sig", locale.getpreferredencoding(False)):
        try:
            # interpolate=False: a secret that happens to contain "${...}" must stay as it is.
            load_dotenv(path, override=False, interpolate=False, encoding=encoding)
            return
        except UnicodeDecodeError:
            continue
    problems.append(f"读不懂 {path} 的文字编码。请用记事本打开它，另存为，编码选 UTF-8")


def read_secret_values(env_file: str | os.PathLike | None = ".env") -> dict[str, str]:
    """The keys as the bot would read them. For shape checks only: never print the values."""
    if env_file and Path(env_file).exists():
        _load_env_file(Path(env_file), [])
    return {name: _get(name) for name in SECRET_NAMES}


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float | None, problems: list[str]) -> float | None:
    raw = _get(name)
    if not raw:
        return default
    if raw.lower() in OFF_WORDS:
        return None
    try:
        return float(raw)
    except ValueError:
        problems.append(f"{name} 应该是数字，现在是 {raw!r}")
        return default


def _get_int(name: str, default: int, problems: list[str]) -> int:
    value = _get_float(name, float(default), problems)
    return int(value) if value is not None else default


def parse_hhmm(raw: str) -> time:
    hour, minute = raw.split(":")
    return time(int(hour), int(minute))


def parse_weekly(raw: str) -> tuple[int, time]:
    """'SUN 17:00' -> (6, time(17, 0)). Monday is 0."""
    day, hhmm = raw.split()
    return WEEKDAYS.index(day.upper()), parse_hhmm(hhmm)


@dataclass(frozen=True)
class Config:
    # Feishu
    feishu_app_id: str
    feishu_app_secret: str = field(repr=False)
    feishu_domain: str = FEISHU_DOMAINS["feishu"]
    owner_open_id: str = ""
    group_chat_id: str = ""

    # OKX
    okx_api_key: str = field(default="", repr=False)
    okx_secret_key: str = field(default="", repr=False)
    okx_passphrase: str = field(default="", repr=False)
    okx_demo: bool = False
    okx_base_url: str = "https://www.okx.com"
    watch_instruments: tuple[str, ...] = ("XAU-USDT-SWAP",)

    # Claude
    anthropic_api_key: str = field(default="", repr=False)
    model_quick: str = "claude-haiku-5-5"  # sorts incoming messages
    model_daily: str = "claude-opus-5-5"  # questions and briefs
    model_deep: str = "claude-opus-5-5"  # "深度" questions and the weekly review
    effort_daily: str = "medium"
    effort_deep: str = "high"
    web_search: bool = True  # the AI may search the web for news and economic data
    ai_monthly_budget_usd: float | None = 180.0  # above this the AI switches to its cheapest setting
    knowledge_dir: Path = Path("knowledge")
    track_record: bool = True  # keep the levels the AI gives and score them against the market later
    track_window_hours: int = 72  # how long a scenario is watched

    # Behaviour
    timezone: str = "America/Los_Angeles"
    poll_seconds: int = 30
    snapshot_minutes: int = 15
    alert_margin_ratio: float | None = 3.0
    alert_liq_distance_pct: float | None = 5.0
    alert_daily_loss_usd: float | None = None
    alert_cooldown_minutes: int = 30
    brief_morning: time | None = time(6, 0)
    brief_evening: time | None = time(14, 0)
    weekly_review: tuple[int, time] | None = (6, time(17, 0))
    group_show_pnl: bool = True  # the group sees balances and PnL, and so does the AI when it answers there
    group_owner_only: bool = True  # in the group, only the owner's messages are answered
    group_daily_question_limit: int = 30
    db_path: Path = Path("data/bot.db")
    log_dir: Path = Path("logs")
    heartbeat_url: str = field(default="", repr=False)  # fetched every few minutes, for an outside watcher; empty = off
    backup_keep_days: int = 14  # daily copies of the data kept in data/backups; 0 = no backups

    # Placing orders. Off unless the owner switches it on in .env; none of this can be changed from a chat.
    trading: str = "off"  # "off", "demo" (OKX demo trading) or "live" (the real account)
    trading_blocked: str = ""  # why the mode asked for in .env could not be honoured
    okx_demo_api_key: str = field(default="", repr=False)
    okx_demo_secret_key: str = field(default="", repr=False)
    okx_demo_passphrase: str = field(default="", repr=False)
    trade_instruments: tuple[str, ...] = ()  # the only instruments an order may name; empty means the watch list
    trade_max_order_usd: float = 1000.0  # value of one order
    trade_max_position_usd: float = 3000.0  # value of the position in one instrument, open orders included
    trade_max_opens_per_day: int = 20
    trade_daily_loss_limit_usd: float | None = None  # no new positions once the day is down this much
    trade_max_deviation_pct: float = 1.0  # a limit price may not be further than this on the wrong side of the market
    trade_max_drift_pct: float = 0.5  # how far the price may move between the preview and the confirmation
    trade_margin_mode: str = "cross"  # for a new position; an existing one keeps its own
    trade_confirm_seconds: int = 120
    trade_require_stop: bool = False  # refuse an opening order that carries no stop-loss

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def has_okx_keys(self) -> bool:
        return bool(self.okx_api_key and self.okx_secret_key and self.okx_passphrase)

    @property
    def has_okx_demo_keys(self) -> bool:
        return bool(self.okx_demo_api_key and self.okx_demo_secret_key and self.okx_demo_passphrase)

    @property
    def trading_on(self) -> bool:
        return self.trading in ("demo", "live")

    @property
    def tradable_instruments(self) -> tuple[str, ...]:
        return self.trade_instruments or self.watch_instruments

    def register_group(self, chat_id: str) -> None:
        """The one setting that changes while the bot runs: the owner registers
        a group from inside Feishu. Every part of the bot shares this object, so
        the briefs and alerts follow at once."""
        object.__setattr__(self, "group_chat_id", chat_id)


def load_config(env_file: str | os.PathLike | None = ".env") -> tuple[Config, list[str]]:
    """Read the configuration. Returns (config, problems).

    `problems` is a list of human-readable messages (in Chinese, for the
    owner). An empty list means the configuration is complete.
    """
    problems: list[str] = []
    if env_file and Path(env_file).exists():
        _load_env_file(Path(env_file), problems)

    domain_key = _get("FEISHU_DOMAIN", "feishu").lower()
    if domain_key not in FEISHU_DOMAINS:
        problems.append("FEISHU_DOMAIN 只能填 feishu（国内版）或 lark（海外版）")
        domain_key = "feishu"

    timezone = _get("TIMEZONE", "America/Los_Angeles")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"TIMEZONE 不认识：{timezone!r}，例如 America/Los_Angeles")
        timezone = "America/Los_Angeles"

    def _time(name: str, default: str) -> time | None:
        raw = _get(name, default)
        if raw.lower() in OFF_WORDS:
            return None
        try:
            return parse_hhmm(raw)
        except (ValueError, IndexError):
            problems.append(f"{name} 的格式应该是 HH:MM，例如 06:00，现在是 {raw!r}")
            return None

    weekly_raw = _get("WEEKLY_REVIEW", "SUN 17:00")
    weekly: tuple[int, time] | None
    if weekly_raw.lower() in OFF_WORDS:
        weekly = None
    else:
        try:
            weekly = parse_weekly(weekly_raw)
        except (ValueError, IndexError):
            problems.append(f"WEEKLY_REVIEW 的格式应该像 SUN 17:00，现在是 {weekly_raw!r}")
            weekly = None

    def _effort(name: str, default: str) -> str:
        raw = _get(name, default).lower()
        if raw not in EFFORTS:
            problems.append(f"{name} 只能填 {'、'.join(EFFORTS)} 之一，现在是 {raw!r}")
            return default
        return raw

    instruments = tuple(
        item.strip().upper() for item in _get("WATCH_INSTRUMENTS", "XAU-USDT-SWAP").split(",") if item.strip()
    )
    if not instruments:
        problems.append("WATCH_INSTRUMENTS 至少要填一个品种，例如 XAU-USDT-SWAP")

    # A key pasted into the wrong line counts as "not filled in": it must never
    # be sent to a company it does not belong to.
    secrets = {name: _get(name) for name in SECRET_NAMES}
    for finding in check_secret_shapes(secrets):
        if finding.status == "wrong":
            secrets[finding.name] = ""
            problems.append(f"{finding.name} 填错位置了（{finding.note}），机器人当它没填。请重新填这一项")

    okx_demo = _get_bool("OKX_DEMO", False)
    has_main_keys = all(secrets[name] for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"))
    has_demo_keys = all(secrets[name] for name in ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE"))
    trading = _get("TRADING", "off").lower()
    heartbeat_url = _get("HEARTBEAT_URL")
    if heartbeat_url.lower() in OFF_WORDS:
        heartbeat_url = ""
    if heartbeat_url and not heartbeat_url.lower().startswith(("https://", "http://")):
        problems.append("HEARTBEAT_URL 要填一个以 https:// 开头的网址，现在填的不是，已忽略")
        heartbeat_url = ""

    trading_blocked = ""
    if trading in OFF_WORDS or trading in ("false", "0", "no"):
        trading = "off"
    elif trading not in TRADING_MODES:
        trading_blocked = f"TRADING 只能填 off、demo 或 live，现在是 {trading!r}"
    elif trading == "demo" and not has_demo_keys and not (okx_demo and has_main_keys):
        trading_blocked = "TRADING=demo 需要模拟盘的三项 key：OKX_DEMO_API_KEY、OKX_DEMO_SECRET_KEY、OKX_DEMO_PASSPHRASE"
    elif trading == "live" and okx_demo:
        trading_blocked = "TRADING=live 是在实盘下单，但 OKX_DEMO=true 说明填的是模拟盘的 key。要在模拟盘下单请填 TRADING=demo"
    elif trading == "live" and not has_main_keys:
        trading_blocked = "TRADING=live 需要先填好 OKX 的三项 key"
    if trading_blocked:
        problems.append(trading_blocked + "。下单功能保持关闭")
        trading = "off"

    margin_mode = _get("TRADE_MARGIN_MODE", "cross").lower()
    if margin_mode not in MARGIN_MODES:
        problems.append(f"TRADE_MARGIN_MODE 只能填 cross（全仓）或 isolated（逐仓），现在是 {margin_mode!r}")
        margin_mode = "cross"

    def _positive(name: str, default: float) -> float:
        value = _get_float(name, default, problems)
        if value is None or value <= 0:
            if _get(name):
                problems.append(f"{name} 要填一个大于 0 的数，现在按默认值 {default:g} 算")
            return default
        return value

    cfg = Config(
        feishu_app_id=secrets["FEISHU_APP_ID"],
        feishu_app_secret=secrets["FEISHU_APP_SECRET"],
        feishu_domain=FEISHU_DOMAINS[domain_key],
        owner_open_id=_get("OWNER_OPEN_ID"),
        group_chat_id=_get("GROUP_CHAT_ID"),
        okx_api_key=secrets["OKX_API_KEY"],
        okx_secret_key=secrets["OKX_SECRET_KEY"],
        okx_passphrase=secrets["OKX_PASSPHRASE"],
        okx_demo=okx_demo,
        okx_base_url=_get("OKX_BASE_URL", "https://www.okx.com").rstrip("/"),
        watch_instruments=instruments or ("XAU-USDT-SWAP",),
        anthropic_api_key=secrets["ANTHROPIC_API_KEY"],
        model_quick=_get("MODEL_QUICK", "claude-haiku-5-5"),
        model_daily=_get("MODEL_DAILY", "claude-opus-5-5"),
        model_deep=_get("MODEL_DEEP", "claude-opus-5-5"),
        effort_daily=_effort("EFFORT_DAILY", "medium"),
        effort_deep=_effort("EFFORT_DEEP", "high"),
        web_search=_get_bool("WEB_SEARCH", True),
        ai_monthly_budget_usd=_get_float("AI_MONTHLY_BUDGET_USD", 180.0, problems),
        knowledge_dir=Path(_get("KNOWLEDGE_DIR", "knowledge")),
        track_record=_get_bool("TRACK_RECORD", True),
        track_window_hours=min(72, max(4, _get_int("TRACK_WINDOW_HOURS", 72, problems))),
        timezone=timezone,
        poll_seconds=max(5, _get_int("POLL_SECONDS", 30, problems)),
        snapshot_minutes=max(1, _get_int("SNAPSHOT_MINUTES", 15, problems)),
        alert_margin_ratio=_get_float("ALERT_MARGIN_RATIO", 3.0, problems),
        alert_liq_distance_pct=_get_float("ALERT_LIQ_DISTANCE_PCT", 5.0, problems),
        alert_daily_loss_usd=_get_float("ALERT_DAILY_LOSS_USD", None, problems),
        alert_cooldown_minutes=max(1, _get_int("ALERT_COOLDOWN_MINUTES", 30, problems)),
        brief_morning=_time("BRIEF_MORNING", "06:00"),
        brief_evening=_time("BRIEF_EVENING", "14:00"),
        weekly_review=weekly,
        group_show_pnl=_get_bool("GROUP_SHOW_PNL", True),
        group_owner_only=_get_bool("GROUP_OWNER_ONLY", True),
        group_daily_question_limit=max(0, _get_int("GROUP_DAILY_QUESTION_LIMIT", 30, problems)),
        db_path=Path(_get("DB_PATH", "data/bot.db")),
        log_dir=Path(_get("LOG_DIR", "logs")),
        heartbeat_url=heartbeat_url,
        backup_keep_days=max(0, _get_int("BACKUP_KEEP_DAYS", 14, problems)),
        trading=trading,
        trading_blocked=trading_blocked,
        okx_demo_api_key=secrets["OKX_DEMO_API_KEY"],
        okx_demo_secret_key=secrets["OKX_DEMO_SECRET_KEY"],
        okx_demo_passphrase=secrets["OKX_DEMO_PASSPHRASE"],
        trade_instruments=tuple(
            item.strip().upper() for item in _get("TRADE_INSTRUMENTS").split(",") if item.strip()
        ),
        trade_max_order_usd=_positive("TRADE_MAX_ORDER_USD", 1000.0),
        trade_max_position_usd=_positive("TRADE_MAX_POSITION_USD", 3000.0),
        trade_max_opens_per_day=max(0, _get_int("TRADE_MAX_OPENS_PER_DAY", 20, problems)),
        trade_daily_loss_limit_usd=_get_float("TRADE_DAILY_LOSS_LIMIT_USD", None, problems),
        trade_max_deviation_pct=_positive("TRADE_MAX_DEVIATION_PCT", 1.0),
        trade_max_drift_pct=_positive("TRADE_MAX_DRIFT_PCT", 0.5),
        trade_margin_mode=margin_mode,
        trade_confirm_seconds=max(20, _get_int("TRADE_CONFIRM_SECONDS", 120, problems)),
        trade_require_stop=_get_bool("TRADE_REQUIRE_STOP", False),
    )

    if not cfg.feishu_app_id or not cfg.feishu_app_secret:
        problems.append("还没填飞书的 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
    if not cfg.has_okx_keys:
        problems.append("还没填 OKX 的 OKX_API_KEY、OKX_SECRET_KEY、OKX_PASSPHRASE（三样都要）")
    if not cfg.anthropic_api_key:
        problems.append("还没填 ANTHROPIC_API_KEY，AI 分析和简报用不了")
    if not cfg.owner_open_id:
        problems.append("还没填 OWNER_OPEN_ID。先启动机器人，在飞书里私聊它发送 /whoami，把它回的 open_id 填进来")

    return cfg, problems
