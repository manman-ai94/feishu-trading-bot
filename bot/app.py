"""Wires everything together and runs the bot."""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .analyst import Analyst, Usage
from .config import Config
from .crowd import Crowd
from .macro import Macro
from .okx_stream import BUSINESS_CHANNELS, OkxStream, Streams, business_url, stream_url
from .risk import RiskGauge
from .envfile import write_env_values
from .feishu_client import FeishuClient, IncomingMessage
from .instruments import InstrumentResolver
from .journal import Journal
from .knowledge import KnowledgeBase
from .monitor import Monitor
from .okx_client import OkxClient, OkxError, OkxNetworkError
from .okx_trade import OkxTradeClient
from .router import Router
from .scheduler import Scheduler
from .service import Reporter
from .tools import Toolbox
from .review import LESSONS_DAYS, LESSONS_SHOWN, Reviewer, lessons_text
from .track import Levels, Scorer, track_text_for_ai
from .trading import TradeDesk
from .upkeep import Upkeep

log = logging.getLogger(__name__)


class MaskingFormatter(logging.Formatter):
    """Writes "***" wherever a key, secret or passphrase from .env would appear in a log line, tracebacks
    included. The bot never logs them on purpose; this catches a library or an error message that does."""

    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        # Longest first, so a secret that contains another is masked whole. Short values are not secrets.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def secret_values(cfg: Config) -> list[str]:
    """Every value in the configuration that must never be written anywhere."""
    return [
        cfg.feishu_app_secret, cfg.okx_api_key, cfg.okx_secret_key, cfg.okx_passphrase, cfg.anthropic_api_key,
        cfg.okx_demo_api_key, cfg.okx_demo_secret_key, cfg.okx_demo_passphrase, cfg.heartbeat_url,
    ]  # fmt: skip


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = MaskingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", secret_values(cfg))
    file_handler = RotatingFileHandler(cfg.log_dir / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [file_handler, console]
    # At DEBUG the websockets library writes every frame, the stream's login with its key included.
    # Kept at WARNING even if someone turns the root logger down to DEBUG to look into a problem.
    logging.getLogger("websockets").setLevel(logging.WARNING)


def build_okx(cfg: Config) -> OkxClient:
    return OkxClient(cfg.okx_api_key, cfg.okx_secret_key, cfg.okx_passphrase, cfg.okx_base_url, cfg.okx_demo)


def build_trader(cfg: Config) -> OkxTradeClient | None:
    """The client that can place orders. None unless the owner switched trading on in .env.

    TRADING=demo sends orders to OKX demo trading: with the demo keys when they
    are filled in, otherwise with the main keys (which are demo keys when
    OKX_DEMO=true). TRADING=live uses the main keys on the real account.
    """
    if cfg.trading == "live":
        trader = OkxTradeClient(cfg.okx_api_key, cfg.okx_secret_key, cfg.okx_passphrase, cfg.okx_base_url, demo=False)
    elif cfg.trading == "demo" and cfg.has_okx_demo_keys:
        trader = OkxTradeClient(
            cfg.okx_demo_api_key, cfg.okx_demo_secret_key, cfg.okx_demo_passphrase, cfg.okx_base_url, demo=True
        )
    elif cfg.trading == "demo":
        trader = OkxTradeClient(cfg.okx_api_key, cfg.okx_secret_key, cfg.okx_passphrase, cfg.okx_base_url, demo=True)
    else:
        return None
    trader.trigger_px_type = cfg.trade_stop_trigger
    return trader


def build_trade_desk(cfg: Config, journal: Journal, okx: OkxClient, resolver: InstrumentResolver, reporter=None) -> TradeDesk:
    """The trade desk is always there, so the owner can ask how trading is set up; it only gets a
    client that can place orders when trading is switched on."""

    def day_change() -> float | None:
        state = reporter.current_state() if reporter is not None else None
        return reporter.day_change(state) if state is not None else None

    return TradeDesk(cfg, journal, okx, build_trader(cfg), resolver, day_change=day_change)


def build_crowd(cfg: Config, okx: OkxClient) -> Crowd | None:
    """Where the crowd's positions are read from. None when the owner switched that off."""
    return Crowd(okx, cfg.tz) if cfg.crowd_data else None


def build_macro(cfg: Config) -> Macro | None:
    """Where US rates and the dollar are read from (FRED). None when the owner switched that off."""
    return Macro() if cfg.macro_data else None


def build_ai(
    cfg: Config,
    okx: OkxClient,
    journal: Journal,
    crowd: Crowd | None = None,
    track: bool = True,
    macro: Macro | None = None,
) -> tuple[Analyst, InstrumentResolver, KnowledgeBase]:
    """The analyst with its tools, metered into the journal. `track=False` keeps the levels of its
    answers out of the track record (a rehearsal is not advice anyone was given)."""
    resolver = InstrumentResolver(okx)
    knowledge = KnowledgeBase(cfg.knowledge_dir)

    def record(purpose: str, usage: Usage) -> None:
        journal.record_usage(purpose, **usage.as_row())

    def month_spend() -> float:
        month_start = datetime.now(cfg.tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return journal.usage_between(month_start.timestamp(), time.time() + 1)["cost_usd"]

    def keep_levels(purpose: str, question: str, levels: Levels) -> None:
        """For the track record: the levels of an analysis, with the price at that moment."""
        try:
            ref_px = okx.ticker(levels.inst_id).last
        except (OkxError, OkxNetworkError) as exc:
            # No price means the instrument does not exist on OKX or cannot be read: nothing to score against.
            log.info("track record: %s not kept, no price (%s)", levels.inst_id, exc)
            return
        scenarios = [
            {"side": s.side, "trigger": s.trigger, "target": s.target, "invalid": s.invalid, "confidence": s.confidence}
            for s in levels.scenarios
        ]
        journal.record_analysis(purpose, levels.inst_id, levels.bias, ref_px, scenarios, question[:120])

    analyst = Analyst(
        cfg,
        toolbox=Toolbox(okx, resolver, knowledge, cfg.tz, crowd, macro),
        on_usage=record,
        month_spend=month_spend,
        on_levels=keep_levels if track else None,
    )
    analyst.attach_feedback(lambda with_account: feedback_text(cfg, journal, time.time(), with_account))
    return analyst, resolver, knowledge


def feedback_text(cfg: Config, journal: Journal, now: float, with_account: bool) -> str:
    """What the AI reads before it answers: its own track record, and the lessons of the latest
    trade reviews when the answer may show the account (they name the trader's trades)."""
    parts = []
    if cfg.track_record:
        parts.append(track_text_for_ai(journal, now, cfg.tz))
    if with_account and cfg.close_review:
        parts.append(lessons_text(journal.recent_lessons(LESSONS_SHOWN, now - LESSONS_DAYS * 86400), cfg.tz))
    return "\n\n".join(part for part in parts if part)


def start_stream(cfg: Config, monitor: Monitor, stop: threading.Event) -> Streams | None:
    """OKX's private WebSockets, which wake the monitor the moment something changes. The regular
    poll carries on regardless; without them an alert simply waits for the next tick."""
    if not cfg.okx_stream:
        log.info("okx stream: off (OKX_STREAM=false); positions are read every %d seconds", cfg.poll_seconds)
        return None
    url = cfg.okx_ws_url or stream_url(cfg.okx_base_url, cfg.okx_demo)
    if not url:
        log.warning("okx stream: no WebSocket address known for %s; set OKX_WS_URL to use it", cfg.okx_base_url)
        return None
    keys = (cfg.okx_api_key, cfg.okx_secret_key, cfg.okx_passphrase)
    private = OkxStream(*keys, url, monitor.poke)
    threading.Thread(target=private.run_forever, args=(stop,), name="okx-stream", daemon=True).start()
    business = None
    algo_url = business_url(url)
    if algo_url:
        business = OkxStream(*keys, algo_url, monitor.poke, channels=BUSINESS_CHANNELS, name="okx algo stream")
        threading.Thread(target=business.run_forever, args=(stop,), name="okx-algo-stream", daemon=True).start()
    else:
        log.info("okx stream: no business address next to %s; stop orders are read on the regular poll", url)
    return Streams(private, business)


RESTART_DELAY_SECONDS = 2.0  # long enough for the "restarting" reply to leave


def _exit_for_restart() -> None:
    """End the process. scripts/start.bat (Windows) and the systemd unit
    (Ubuntu) start the bot again, and it reads .env afresh."""
    logging.shutdown()
    os._exit(0)


def run(cfg: Config, env_file: str | os.PathLike = ".env") -> None:
    setup_logging(cfg)
    journal = Journal(cfg.db_path)
    okx = build_okx(cfg)
    crowd = build_crowd(cfg, okx)
    macro = build_macro(cfg)
    analyst, resolver, knowledge = build_ai(cfg, okx, journal, crowd, macro=macro)
    feishu = FeishuClient(cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_domain)
    gauge = RiskGauge(okx)
    reporter = Reporter(cfg, feishu, okx, analyst, journal, crowd=crowd, gauge=gauge, macro=macro)
    analyst.attach_account(lambda: reporter.account_for_ai(fresh=True), reporter.history_for_ai)
    monitor = Monitor(okx, cfg, journal, reporter.notify_event, gauge=gauge)
    reporter.monitor = monitor
    if cfg.close_review:
        reporter.reviewer = Reviewer(okx, journal, analyst, cfg, reporter)

    bot_open_id = None
    try:
        bot_open_id = feishu.bot_open_id()
    except Exception as exc:
        log.warning("could not read the bot's own open_id (%s); group messages are answered when anyone is @-mentioned", exc)

    def save_setting(name: str, value: str) -> None:
        write_env_values(Path(env_file), {name: value})

    def restart() -> None:
        threading.Timer(RESTART_DELAY_SECONDS, _exit_for_restart).start()

    desk = build_trade_desk(cfg, journal, okx, resolver, reporter)
    reporter.breaker_alert = desk.breaker_alert
    router = Router(
        cfg, feishu, okx, analyst, reporter, bot_open_id, save_setting=save_setting, restart=restart,
        resolver=resolver, knowledge=knowledge, usage=journal.usage_between, trade=desk, journal=journal,
    )  # fmt: skip
    log.info("trading: %s (instruments: %s)", desk.mode, ", ".join(cfg.tradable_instruments))
    if cfg.trading_blocked:
        log.warning("trading stays off: the TRADING setting in .env cannot be honoured")
    log.info(
        "AI: questions on %s (%s, own positions %s), deep on %s (%s), web search %s, crowd data %s, macro data %s, "
        "%d knowledge file(s), close reviews %s",
        cfg.model_daily, cfg.effort_daily, cfg.effort_account, cfg.model_deep, cfg.effort_deep,
        "on" if cfg.web_search else "off", "on" if crowd is not None else "off",
        "on" if macro is not None else "off", len(knowledge.summary()), "on" if cfg.close_review else "off",
    )  # fmt: skip

    stop = threading.Event()
    if cfg.has_okx_keys:
        monitor.poll_once()  # first read, so the start-up message has the positions
        threading.Thread(target=monitor.run_forever, args=(stop,), name="monitor", daemon=True).start()
        stream = start_stream(cfg, monitor, stop)
        desk.stream_status = stream.status if stream is not None else (lambda: f"没开，每 {cfg.poll_seconds} 秒读一次账户")
    else:
        log.warning("no OKX keys configured: position monitoring is off")

    scheduler = Scheduler(
        cfg,
        journal,
        {
            "morning": lambda: reporter.send_brief("morning"),
            "evening": lambda: reporter.send_brief("evening"),
            "weekly": reporter.send_weekly_review,
            "weekend": reporter.send_weekend_check,
        },
    )
    threading.Thread(target=scheduler.run_forever, args=(stop,), name="scheduler", daemon=True).start()
    if cfg.track_record:
        scorer = Scorer(journal, okx, cfg.track_window_hours)
        threading.Thread(target=scorer.run_forever, args=(stop,), name="track", daemon=True).start()

    upkeep = Upkeep(cfg, journal)
    downtime = upkeep.downtime()  # read before this run's first heartbeat overwrites it
    if downtime is not None:
        log.warning("the bot was not running for about %d minute(s)", (downtime[1] - downtime[0]) // 60)
    threading.Thread(target=upkeep.run_forever, args=(stop,), name="upkeep", daemon=True).start()

    try:
        reporter.send_startup(downtime)
    except Exception:
        log.exception("could not send the start-up message")

    # Messages are handled on worker threads so a slow AI answer never blocks
    # the connection to Feishu.
    workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="msg")

    def on_message(message: IncomingMessage) -> None:
        workers.submit(router.handle, message)

    log.info("connecting to Feishu (long connection) ...")
    try:
        feishu.listen(on_message)  # blocks
    finally:
        stop.set()
        workers.shutdown(wait=False)
