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
from .envfile import write_env_values
from .feishu_client import FeishuClient, IncomingMessage
from .instruments import InstrumentResolver
from .journal import Journal
from .knowledge import KnowledgeBase
from .monitor import Monitor
from .okx_client import OkxClient
from .router import Router
from .scheduler import Scheduler
from .service import Reporter
from .tools import Toolbox

log = logging.getLogger(__name__)


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(cfg.log_dir / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [file_handler, console]


def build_okx(cfg: Config) -> OkxClient:
    return OkxClient(cfg.okx_api_key, cfg.okx_secret_key, cfg.okx_passphrase, cfg.okx_base_url, cfg.okx_demo)


def build_ai(cfg: Config, okx: OkxClient, journal: Journal) -> tuple[Analyst, InstrumentResolver, KnowledgeBase]:
    """The analyst with its tools, metered into the journal."""
    resolver = InstrumentResolver(okx)
    knowledge = KnowledgeBase(cfg.knowledge_dir)

    def record(purpose: str, usage: Usage) -> None:
        journal.record_usage(purpose, **usage.as_row())

    def month_spend() -> float:
        month_start = datetime.now(cfg.tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return journal.usage_between(month_start.timestamp(), time.time() + 1)["cost_usd"]

    analyst = Analyst(cfg, toolbox=Toolbox(okx, resolver, knowledge, cfg.tz), on_usage=record, month_spend=month_spend)
    return analyst, resolver, knowledge


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
    analyst, resolver, knowledge = build_ai(cfg, okx, journal)
    feishu = FeishuClient(cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_domain)
    reporter = Reporter(cfg, feishu, okx, analyst, journal)
    monitor = Monitor(okx, cfg, journal, reporter.notify_event)
    reporter.monitor = monitor

    bot_open_id = None
    try:
        bot_open_id = feishu.bot_open_id()
    except Exception as exc:
        log.warning("could not read the bot's own open_id (%s); group messages are answered when anyone is @-mentioned", exc)

    def save_setting(name: str, value: str) -> None:
        write_env_values(Path(env_file), {name: value})

    def restart() -> None:
        threading.Timer(RESTART_DELAY_SECONDS, _exit_for_restart).start()

    router = Router(
        cfg, feishu, okx, analyst, reporter, bot_open_id, save_setting=save_setting, restart=restart,
        resolver=resolver, knowledge=knowledge, usage=journal.usage_between,
    )  # fmt: skip
    log.info(
        "AI: questions on %s (%s), deep on %s (%s), web search %s, %d knowledge file(s)",
        cfg.model_daily, cfg.effort_daily, cfg.model_deep, cfg.effort_deep,
        "on" if cfg.web_search else "off", len(knowledge.summary()),
    )  # fmt: skip

    stop = threading.Event()
    if cfg.has_okx_keys:
        monitor.poll_once()  # first read, so the start-up message has the positions
        threading.Thread(target=monitor.run_forever, args=(stop,), name="monitor", daemon=True).start()
    else:
        log.warning("no OKX keys configured: position monitoring is off")

    scheduler = Scheduler(
        cfg,
        journal,
        {
            "morning": lambda: reporter.send_brief("morning"),
            "evening": lambda: reporter.send_brief("evening"),
            "weekly": reporter.send_weekly_review,
        },
    )
    threading.Thread(target=scheduler.run_forever, args=(stop,), name="scheduler", daemon=True).start()

    try:
        reporter.send_startup()
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
