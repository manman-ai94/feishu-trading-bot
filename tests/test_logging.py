"""No key, secret or passphrase from .env ever reaches the log, whoever writes it."""
from __future__ import annotations

import logging

from bot.app import setup_logging

from .helpers import make_cfg


def test_secrets_are_masked_in_messages_and_tracebacks(tmp_path):
    cfg = make_cfg(
        log_dir=tmp_path, okx_api_key="okx-key-1234567890", okx_secret_key="OKXSECRET99887766",
        okx_passphrase="Passphrase!2026", feishu_app_secret="feishu-secret-abcdef", anthropic_api_key="sk-ant-test-0123456789",
        heartbeat_url="https://hc-ping.com/0f8e7d6c-uuid",
    )  # fmt: skip
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        setup_logging(cfg)
        log = logging.getLogger("some.library")
        log.info("request headers %s", {"OK-ACCESS-KEY": "okx-key-1234567890", "OK-ACCESS-PASSPHRASE": "Passphrase!2026"})
        try:
            raise ValueError("bad signature for OKXSECRET99887766 at https://hc-ping.com/0f8e7d6c-uuid")
        except ValueError:
            log.exception("call failed with sk-ant-test-0123456789 and feishu-secret-abcdef")
        log.info("short values like %s are left alone", "pass")
        for handler in root.handlers:
            handler.flush()
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved
    text = (tmp_path / "bot.log").read_text(encoding="utf-8")
    for secret in ("okx-key-1234567890", "Passphrase!2026", "OKXSECRET99887766", "sk-ant-test-0123456789", "feishu-secret-abcdef", "0f8e7d6c-uuid"):
        assert secret not in text
    assert text.count("***") >= 6 and "ValueError" in text and "short values like pass" in text


def test_the_websocket_library_never_logs_its_frames(tmp_path):
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        setup_logging(make_cfg(log_dir=tmp_path))
        root.setLevel(logging.DEBUG)  # someone looking into a problem
        assert not logging.getLogger("websockets.client").isEnabledFor(logging.DEBUG)
        assert logging.getLogger("bot.monitor").isEnabledFor(logging.DEBUG)
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved
