"""Command line entry point.

    python -m bot selfcheck     check the configuration and every connection
    python -m bot run           start the bot
    python -m bot status        print the current account status in the console
    python -m bot brief morning send a brief right now (morning | evening | weekly)
    python -m bot setenv        write NAME=value lines from standard input into .env
"""
from __future__ import annotations

import argparse
import sys

from .config import Config, load_config


def _print(mark: str, text: str) -> None:
    print(f"{mark} {text}")


def selfcheck(cfg: Config, problems: list[str]) -> int:
    """Walks through every connection and says, in plain words, what works."""
    from .analyst import Analyst, AnalystUnavailable
    from .app import build_okx
    from .feishu_client import FeishuClient
    from .okx_client import OkxError, OkxNetworkError

    failures = 0

    print("== 1. 配置文件 (.env) ==")
    if problems:
        for problem in problems:
            _print("[!]", problem)
    else:
        _print("[OK]", "配置项齐全")

    print("\n== 2. OKX 公开行情 ==")
    okx = build_okx(cfg)
    try:
        offset = okx.sync_clock()
        if abs(offset) > 5:
            _print("[!]", f"这台机器的时间和 OKX 相差 {offset:+.1f} 秒，请打开系统的自动校时")
        else:
            _print("[OK]", f"能连上 OKX，时间相差 {offset:+.2f} 秒")
        for inst_id in cfg.watch_instruments:
            ticker = okx.ticker(inst_id)
            _print("[OK]", f"{inst_id} 最新价 {ticker.last:,.2f}")
    except (OkxError, OkxNetworkError) as exc:
        failures += 1
        _print("[X]", f"OKX 行情读取失败：{exc}")

    print("\n== 3. OKX 账户（只读取，不下单） ==")
    if not cfg.has_okx_keys:
        failures += 1
        _print("[X]", "还没填 OKX 的三个 key")
    else:
        try:
            state = okx.account_state()
            _print("[OK]", f"账户读取成功，当前 {len(state.positions)} 个持仓")
            if cfg.okx_demo:
                _print("[!]", "现在连的是模拟盘 (OKX_DEMO=true)")
        except (OkxError, OkxNetworkError) as exc:
            failures += 1
            _print("[X]", f"账户读取失败：{exc}")
            _print("   ", "常见原因：key 填错、Passphrase 不对、这台机器的 IP 不在这把 key 的白名单里")

    print("\n== 4. Claude ==")
    try:
        Analyst(cfg).ping()
        _print("[OK]", f"Claude 接口可用（{cfg.model_quick}）")
    except AnalystUnavailable as exc:
        failures += 1
        _print("[X]", str(exc))

    print("\n== 5. 飞书 ==")
    if not cfg.feishu_app_id or not cfg.feishu_app_secret:
        failures += 1
        _print("[X]", "还没填飞书的 App ID 和 App Secret")
    else:
        feishu = FeishuClient(cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_domain)
        try:
            bot_id = feishu.bot_open_id()
            _print("[OK]", f"飞书应用凭证有效，机器人 ID {bot_id or '未知'}")
            if cfg.owner_open_id:
                feishu.send_text(cfg.owner_open_id, "这是一条自检消息：机器人能给你发私聊了。", "open_id")
                _print("[OK]", "已给你发了一条私聊测试消息，请在飞书里确认收到")
            else:
                _print("[!]", "还没填 OWNER_OPEN_ID，先 run 起来，私聊机器人发送 /whoami")
            if not cfg.group_chat_id:
                _print("[!]", "还没填 GROUP_CHAT_ID，群里的简报和通知暂时不会发。在群里 @机器人 发送 /whoami 可以拿到")
        except Exception as exc:
            failures += 1
            _print("[X]", f"飞书连接失败：{exc}")
            _print("   ", "常见原因：App ID 或 App Secret 填错、应用还没发布、没有开通机器人能力和消息权限")

    print()
    if failures:
        print(f"有 {failures} 项没通过，请按上面的提示处理后再运行一次。")
        return 1
    print("全部通过，可以运行：python -m bot run")
    return 0


def print_status(cfg: Config) -> int:
    from .app import build_okx
    from .okx_client import OkxError, OkxNetworkError
    from .reports import status_markdown

    try:
        state = build_okx(cfg).account_state()
    except (OkxError, OkxNetworkError) as exc:
        print(f"读取失败：{exc}")
        return 1
    print(status_markdown(state, None, private=True).replace("**", ""))
    return 0


def send_brief(cfg: Config, kind: str) -> int:
    from .analyst import Analyst
    from .app import build_okx, setup_logging
    from .feishu_client import FeishuClient
    from .journal import Journal
    from .monitor import Monitor
    from .service import Reporter

    setup_logging(cfg)
    journal = Journal(cfg.db_path)
    okx = build_okx(cfg)
    reporter = Reporter(cfg, FeishuClient(cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_domain), okx, Analyst(cfg), journal)
    reporter.monitor = Monitor(okx, cfg, journal, lambda event: None)
    if cfg.has_okx_keys:
        reporter.monitor.poll_once()
    if kind == "weekly":
        reporter.send_weekly_review()
    else:
        reporter.send_brief(kind)
    print("已发送。")
    return 0


def set_env(env_file: str, stdin=None) -> int:
    """Reads NAME=value lines from standard input and writes them into .env.

    Only the names are printed. The values are never shown or logged.
    """
    from pathlib import Path

    from .envfile import parse_assignments, write_env_values

    stream = stdin if stdin is not None else sys.stdin
    raw = stream.buffer.read() if hasattr(stream, "buffer") else stream.read()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    updates, skipped, bad = parse_assignments(text)
    if bad:
        _print("[X]", f"这几行不是 NAME=value 的格式，没有写入任何内容：{'、'.join(bad)}")
        return 1
    if skipped:
        _print("[!]", f"留空没改：{'、'.join(skipped)}")
    if not updates:
        _print("[!]", "没有收到要写入的内容")
        return 0
    write_env_values(Path(env_file), updates)
    _print("[OK]", f"已写入 {env_file}：{'、'.join(updates)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; make Chinese output safe.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="python -m bot", description="飞书交易助手")
    parser.add_argument("--env", default=".env", help="配置文件路径，默认 .env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="启动机器人")
    commands.add_parser("selfcheck", help="检查配置和各项连接")
    commands.add_parser("status", help="在命令行里打印当前账户状态")
    brief = commands.add_parser("brief", help="立刻发送一份简报")
    brief.add_argument("kind", choices=["morning", "evening", "weekly"])
    commands.add_parser("setenv", help="把标准输入里的 NAME=value 写进 .env（只显示名字，不显示值）")
    args = parser.parse_args(argv)

    if args.command == "setenv":
        return set_env(args.env)

    cfg, problems = load_config(args.env)

    if args.command == "selfcheck":
        return selfcheck(cfg, problems)
    if args.command == "status":
        return print_status(cfg)
    if args.command == "brief":
        return send_brief(cfg, args.kind)

    if not cfg.feishu_app_id or not cfg.feishu_app_secret:
        print("还没填飞书的 FEISHU_APP_ID 和 FEISHU_APP_SECRET，机器人无法启动。先运行：python -m bot selfcheck")
        return 1
    for problem in problems:
        print(f"[!] {problem}")
    from .app import run

    run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
