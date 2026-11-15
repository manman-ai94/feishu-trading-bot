"""Wires everything together and runs the bot."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

from .analyst import Analyst
from .config import Config
from .feishu_client import FeishuClient, IncomingMessage
from .journal import Journal
from .monitor import Monitor
from .okx_client import OkxClient
from .router import Router
from .scheduler import Scheduler
from .service import Reporter

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


def run(cfg: Config) -> None:
    setup_logging(cfg)
    journal = Journal(cfg.db_path)
    okx = build_okx(cfg)
    analyst = Analyst(cfg)
    feishu = FeishuClient(cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_domain)
    reporter = Reporter(cfg, feishu, okx, analyst, journal)
    monitor = Monitor(okx, cfg, journal, reporter.notify_event)
    reporter.monitor = monitor

    bot_open_id = None
    try:
        bot_open_id = feishu.bot_open_id()
    except Exception as exc:
        log.warning("could not read the bot's own open_id (%s); group messages are answered when anyone is @-mentioned", exc)
    router = Router(cfg, feishu, okx, analyst, reporter, bot_open_id)

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
