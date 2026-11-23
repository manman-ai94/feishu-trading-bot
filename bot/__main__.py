"""Command line entry point.

    python -m bot selfcheck     check the configuration and every connection
    python -m bot run           start the bot
    python -m bot status        print the current account status in the console
    python -m bot brief morning send a brief right now (morning | evening | weekly)
    python -m bot quote BTC ETH print the latest OKX price of the given instruments
    python -m bot ask "问题"    ask the AI one market question in the console (no account data)
    python -m bot setenv        write NAME=value lines from standard input into .env
    python -m bot checkenv      say whether each key sits in the right line (shows no values)
    python -m bot clearenv NAME empty the given lines of .env
    python -m bot trading       print how placing orders is set up: mode, limits, what the key may do
    python -m bot record        print the track record: the AI's levels against what the market did
    python -m bot account       print the account, positions and waiting orders exactly as the AI is shown them
    python -m bot wishes 1      print the owner's requests of the last day (--dialog adds the chats)
    python -m bot wishes --done 3,4   mark requests done with this version (the start-up card tells the owner)
    python -m bot eval          ask the eval questions, score each answer, compare with the last run (costs money)
"""
from __future__ import annotations

import argparse
import sys
import time

from .config import Config, load_config


def _print(mark: str, text: str) -> None:
    print(f"{mark} {text}")


def selfcheck(cfg: Config, problems: list[str]) -> int:
    """Walks through every connection and says, in plain words, what works."""
    from .analyst import AnalystUnavailable
    from .app import build_ai, build_okx
    from .feishu_client import FeishuClient
    from .instruments import InstrumentResolver
    from .journal import Journal
    from .market import fmt_price
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
            _print("[OK]", f"{inst_id} 最新价 {fmt_price(ticker.last)}")
        resolver = InstrumentResolver(okx)
        for name in ("BTC", "以太坊"):
            inst_id = resolver.resolve(name)
            if inst_id is None:
                _print("[!]", f"没能在 OKX 的品种列表里找到 {name}，问其他品种时可能查不到行情")
            else:
                _print("[OK]", f"其他品种也能查：{name} 对应 {inst_id}，最新价 {fmt_price(okx.ticker(inst_id).last)}")
    except (OkxError, OkxNetworkError) as exc:
        failures += 1
        _print("[X]", f"OKX 行情读取失败：{exc}")

    if cfg.mt5:
        failures += _check_mt5(cfg, okx)
    else:
        failures += _check_okx_account(cfg, okx)

    print("\n== 4. Claude ==")
    analyst, _, knowledge = build_ai(cfg, okx, Journal(":memory:"))  # a self-check leaves no records
    try:
        analyst.ping()
        _print("[OK]", f"Claude 接口可用（{cfg.model_quick}）")
        _print("[OK]", f"平时用 {cfg.model_daily} 的 {cfg.effort_daily} 档，“深度”用 {cfg.model_deep} 的 {cfg.effort_deep} 档，"
               f"定时盯仓的例行点评用 {cfg.model_watch} 的 {cfg.effort_watch} 档，几个字的追问用 {cfg.model_short} 的 {cfg.effort_short} 档")
        try:
            analyst.check_web_search()
            _print("[OK]", "联网搜索可用，AI 能查新闻和经济数据")
        except AnalystUnavailable as exc:
            _print("[!]", f"联网搜索用不了，AI 只能看 OKX 的行情：{exc}")
    except AnalystUnavailable as exc:
        failures += 1
        _print("[X]", str(exc))
    _print("[OK]", f"资料库文件夹 {cfg.knowledge_dir}，现有 {len(knowledge.summary())} 个文件")

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
                _print("[!]", "还没登记群，群里的简报和通知暂时不会发。把机器人加进群后，在群里 @机器人 发送“登记本群”就行")
        except Exception as exc:
            failures += 1
            _print("[X]", f"飞书连接失败：{exc}")
            _print("   ", "常见原因：App ID 或 App Secret 填错、应用还没发布、没有开通机器人能力和消息权限")

    print("\n== 6. 下单功能 ==")
    if cfg.trading_blocked:
        _print("[!]", f"{cfg.trading_blocked}。下单功能保持关闭")
    elif not cfg.trading_on:
        _print("[OK]", "关闭（TRADING=off）。机器人只读取账户，不会下单")
    else:
        where = "模拟盘" if cfg.trading == "demo" else "实盘"
        _print("[!]", f"已打开，{where}。每条指令都要主人在飞书私聊里回复确认码才执行。详情运行：python -m bot trading")

    print()
    if failures:
        print(f"有 {failures} 项没通过，请按上面的提示处理后再运行一次。")
        return 1
    print("全部通过，可以运行：python -m bot run")
    return 0


def print_trading(cfg: Config) -> int:
    """How placing orders is set up. Reads OKX, changes nothing, and costs nothing."""
    from .app import build_okx, build_trade_desk
    from .instruments import InstrumentResolver
    from .journal import Journal

    okx = build_okx(cfg)
    # The journal holds the "paused" switch and today's count. Where there is none yet, none is created.
    journal = Journal(cfg.db_path if cfg.db_path.exists() else ":memory:")
    desk = build_trade_desk(cfg, journal, okx, InstrumentResolver(okx))
    print("\n".join(desk.status_lines()).replace("**", ""))
    return 0


def print_account(cfg: Config, fields: bool = False) -> int:
    """The account and every waiting order as the AI is shown them. Reads OKX, changes nothing, costs nothing.

    With `fields`, also the names of the fields OKX sent for each waiting order (names only, no values), to
    check that the bot reads the fields OKX really uses.
    """
    from .app import build_okx
    from .book import book_text, read_book
    from .closes import history_text, read_closed, read_triggers
    from .okx_client import OkxError, OkxNetworkError
    from .reports import account_text

    okx = build_okx(cfg)
    try:
        state = okx.account_state()
    except (OkxError, OkxNetworkError) as exc:
        print(f"[X] 读不到账户：{exc}")
        return 1
    print(account_text(state, None, None, cfg.risk_per_trade_pct))
    book = read_book(okx)
    print(book_text(book, okx, state.snapshot.total_eq, cfg.tz))
    if fields:
        for row in book.orders + book.algos:
            attached = [sorted(item) for item in row.get("attachAlgoOrds") or [] if isinstance(item, dict)]
            print(f"[fields] {row.get('ordType', '?')}: {', '.join(sorted(row))}" + (f" | attachAlgoOrds: {attached}" if attached else ""))
    try:
        print(history_text(read_closed(okx), read_triggers(okx), cfg.tz, time.time() - 7 * 86400))
    except (OkxError, OkxNetworkError) as exc:
        print(f"[!] 读不到平仓记录：{exc}")
    return 0


def print_record(cfg: Config, days: int) -> int:
    """The track record as the owner sees it in Feishu. Reads the journal only; costs nothing."""
    import time

    from .journal import Journal
    from .track import scorecard_markdown

    if not cfg.db_path.exists():
        print("还没有记录：机器人还没有运行过。")
        return 0
    print(scorecard_markdown(Journal(cfg.db_path), time.time(), cfg.tz, days, None, cfg.track_window_hours).replace("**", ""))
    return 0


def print_wishes(cfg: Config, days: float, with_dialog: bool) -> int:
    """The owner's requests channel, for the developer's daily look. Reads the journal only; costs nothing."""
    from .journal import Journal
    from .wishes import dump_text

    if not cfg.db_path.exists():
        print("还没有记录：机器人还没有运行过。")
        return 0
    print(dump_text(Journal(cfg.db_path), time.time(), cfg.tz, days, with_dialog, cfg.group_chat_id))
    return 0


def mark_wishes(cfg: Config, numbers: str, status: str, note: str) -> int:
    """Marks requests done (or dropped). Done ones noted "v<version>" are shown on that version's start-up card."""
    from . import __version__
    from .journal import Journal
    from .wishes import mark

    ids = [int(part) for part in numbers.replace("，", ",").split(",") if part.strip().isdigit()]
    if not ids or not cfg.db_path.exists():
        print("没有要标的诉求编号，或者还没有记录。")
        return 1
    marked, missing = mark(Journal(cfg.db_path), ids, status, note or (f"v{__version__}" if status == "done" else ""))
    for wish in marked:
        print(f"#{wish['id']} -> {wish['status']} {wish['note']}: {wish['text'][:60]}")
    for wish_id in missing:
        print(f"#{wish_id}: not found")
    return 1 if missing else 0


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


def print_quotes(cfg: Config, names: list[str]) -> int:
    """Prints the latest OKX price of each name: a quick check that any instrument can be looked up."""
    from .app import build_okx
    from .instruments import InstrumentResolver
    from .market import quote_line
    from .okx_client import OkxError, OkxNetworkError

    okx = build_okx(cfg)
    resolver = InstrumentResolver(okx)
    failures = 0
    for name in names:
        inst_id = resolver.resolve(name)
        if inst_id is None:
            failures += 1
            _print("[X]", f"OKX 上没有找到 {name}")
            continue
        try:
            _print("[OK]", f"{name}：{quote_line(okx, inst_id).replace('**', '')}")
        except (OkxError, OkxNetworkError) as exc:
            failures += 1
            _print("[X]", f"{name}（{inst_id}）读取失败：{exc}")
    return 1 if failures else 0


def ask(cfg: Config, question: str) -> int:
    """One market question, answered in the console exactly as the bot would in a group
    that does not see the account. A way to try the AI and its tools without Feishu."""
    from .analyst import AnalystUnavailable
    from .app import build_ai, build_crowd, build_macro, build_okx
    from .journal import Journal
    from .market import build_market_context
    from .router import split_title

    import time

    okx = build_okx(cfg)
    journal = Journal(cfg.db_path)
    analyst, resolver, _ = build_ai(cfg, okx, journal, build_crowd(cfg, okx), macro=build_macro(cfg))
    named = resolver.find_in_text(question, limit=2)
    market = "\n\n".join(build_market_context(okx, inst_id).to_text() for inst_id in named)
    started = time.time()
    try:
        title, body = split_title(analyst.answer(question, market, None, in_group=True))
        print(f"【{title}】\n{body}" if title else body)
    except AnalystUnavailable as exc:
        _print("[X]", str(exc))
        return 1
    finally:
        used = journal.usage_between(int(started), time.time() + 1)
        print(f"\n[用量] 用时 {time.time() - started:.0f} 秒，联网搜索 {used['web_searches']} 次，约 {used['cost_usd']:.3f} 美元（估算）")
    return 0


def send_brief(cfg: Config, kind: str) -> int:
    from .app import build_ai, build_crowd, build_macro, build_okx, setup_logging
    from .feishu_client import FeishuClient
    from .journal import Journal
    from .monitor import Monitor
    from .service import Reporter

    setup_logging(cfg)
    journal = Journal(cfg.db_path)
    okx = build_okx(cfg)
    crowd = build_crowd(cfg, okx)
    macro = build_macro(cfg)
    analyst, _, _ = build_ai(cfg, okx, journal, crowd, macro=macro)
    feishu = FeishuClient(cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_domain)
    reporter = Reporter(cfg, feishu, okx, analyst, journal, crowd=crowd, macro=macro)
    reporter.monitor = Monitor(okx, cfg, journal, lambda event: None)
    if cfg.has_account:
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
    _warn_about_misplaced_keys(env_file)
    return 0


def _warn_about_misplaced_keys(env_file: str) -> None:
    """After writing: point out, by name only, any key that sits in the wrong line."""
    from dotenv import dotenv_values

    from .envfile import check_secret_shapes

    try:
        values = dotenv_values(env_file, interpolate=False, encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for finding in check_secret_shapes(values):
        if finding.status == "wrong":
            _print("[X]", f"{finding.name} 好像填错位置了：{finding.note}。机器人会当它没填，请再运行一次，只重填这一项")


def check_env(env_file: str) -> int:
    """Says whether each key looks like it belongs in its line. Shows names only."""
    from .config import read_secret_values
    from .envfile import OPTIONAL_SECRET_NAMES, check_secret_shapes

    wrong = 0
    for finding in check_secret_shapes(read_secret_values(env_file)):
        if finding.status == "ok":
            _print("[OK]", f"{finding.name} 格式对")
        elif finding.status == "empty":
            optional = "（只有在模拟盘下单才需要）" if finding.name in OPTIONAL_SECRET_NAMES else ""
            _print("[--]", f"{finding.name} 还空着{optional}")
        elif finding.status == "unsure":
            _print("[??]", f"{finding.name} 拿不准：{finding.note}。会照常使用")
        else:
            wrong += 1
            _print("[X]", f"{finding.name} 填错位置了：{finding.note}")
    if wrong:
        print(f"\n有 {wrong} 项填错了位置，机器人会当它们没填。请只重填这几项。")
        return 1
    return 0


def clear_env(env_file: str, names: list[str]) -> int:
    """Empties the given lines of .env, for example a key that was pasted into the wrong line."""
    from pathlib import Path

    from .envfile import NAME_PATTERN, write_env_values

    bad = [name for name in names if not NAME_PATTERN.fullmatch(name)]
    if bad:
        _print("[X]", f"这不是配置项的名字，没有改动任何内容：{'、'.join(bad)}")
        return 1
    if not Path(env_file).exists():
        _print("[X]", f"找不到 {env_file}")
        return 1
    write_env_values(Path(env_file), {name: "" for name in names})
    _print("[OK]", f"已清空 {env_file} 里的：{'、'.join(names)}")
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
    quote = commands.add_parser("quote", help="打印几个品种在 OKX 的最新价，例如 quote BTC ETH 黄金")
    quote.add_argument("names", nargs="+", metavar="NAME")
    question = commands.add_parser("ask", help="在命令行里问 AI 一个行情问题（不带账户数据），用来试 AI 和它的工具")
    question.add_argument("question")
    commands.add_parser("setenv", help="把标准输入里的 NAME=value 写进 .env（只显示名字，不显示值）")
    commands.add_parser("checkenv", help="检查每个密钥是不是填在了对的位置（只显示名字，不显示值）")
    clear = commands.add_parser("clearenv", help="把 .env 里指定的几项清空")
    clear.add_argument("names", nargs="+", metavar="NAME")
    commands.add_parser("trading", help="打印下单功能的设置：模式、限额、这把 key 的权限（只读取，不下单）")
    account = commands.add_parser("account", help="打印 AI 看到的账户、持仓、所有挂着的委托和最近 7 天的平仓（只读取，不下单，不花钱）")
    account.add_argument("--fields", action="store_true", help="另外列出 OKX 每张挂单返回的字段名（只有名字，没有值）")
    record = commands.add_parser("record", help="打印成绩单：AI 给过的点位和事后的行情对照（只读取记录，不花钱）")
    record.add_argument("days", nargs="?", type=int, default=30)
    wishes = commands.add_parser("wishes", help="打印主人的诉求清单（--dialog 连同私聊和群里的问答），给开发的人每天看（只读取记录，不花钱）")
    wishes.add_argument("days", nargs="?", type=float, default=1.0)
    wishes.add_argument("--dialog", action="store_true", help="连同这几天私聊和群里的问答一起打印")
    wishes.add_argument("--done", default="", help="把这些编号（逗号分隔）标成做好了，默认记上当前版本号")
    wishes.add_argument("--drop", default="", help="把这些编号标成不做了")
    wishes.add_argument("--note", default="", help="和 --done/--drop 一起用：记在诉求后面的说明")
    commands.add_parser(
        "replay", help="把 scripts/replay-group.txt 里群里问过的问题按主人在群里 @我 的样子重放，只打印回答（不发飞书、不下单，AI 调用照常计费）"
    )
    evaluate = commands.add_parser(
        "eval", help="评测：问 scripts/eval-questions.json 里的题，给每个回答打分，和上一次比（不发飞书、不下单；一轮约几美元）"
    )
    evaluate.add_argument("--dry-run", action="store_true", help="只列出题目和要点，不调用 AI，不花钱")
    evaluate.add_argument("--only", default="", help="只问这几题，题号用逗号隔开，例如 9,10,11")
    args = parser.parse_args(argv)

    if args.command == "setenv":
        return set_env(args.env)
    if args.command == "checkenv":
        return check_env(args.env)
    if args.command == "clearenv":
        return clear_env(args.env, args.names)

    cfg, problems = load_config(args.env)

    if args.command == "selfcheck":
        return selfcheck(cfg, problems)
    if args.command == "status":
        return print_status(cfg)
    if args.command == "brief":
        return send_brief(cfg, args.kind)
    if args.command == "quote":
        return print_quotes(cfg, args.names)
    if args.command == "ask":
        return ask(cfg, args.question)
    if args.command == "trading":
        return print_trading(cfg)
    if args.command == "record":
        return print_record(cfg, max(1, min(365, args.days)))
    if args.command == "account":
        return print_account(cfg, args.fields)
    if args.command == "wishes":
        if args.done or args.drop:
            return mark_wishes(cfg, args.done or args.drop, "done" if args.done else "dropped", args.note)
        return print_wishes(cfg, max(0.1, min(30.0, args.days)), args.dialog)
    if args.command == "replay":
        from .replay import replay

        return replay(cfg)
    if args.command == "eval":
        from .evaluate import run_eval

        only = {int(part) for part in args.only.split(",") if part.strip().isdigit()}
        return run_eval(cfg, dry_run=args.dry_run, only=only or None)

    if not cfg.feishu_app_id or not cfg.feishu_app_secret:
        print("还没填飞书的 FEISHU_APP_ID 和 FEISHU_APP_SECRET，机器人无法启动。先运行：python -m bot selfcheck")
        return 1
    for problem in problems:
        print(f"[!] {problem}")
    from .app import run

    run(cfg, args.env)
    return 0


def _check_okx_account(cfg, okx) -> int:
    """Section 3 of the self-check on OKX. Returns the number of failures."""
    from .okx_client import OkxError, OkxNetworkError

    failures = 0
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
            account = okx.account_config()
            rights = "读取" + ("、交易" if account.can_trade else "")
            _print("[OK]", f"这把 key 的权限：{rights}；{'已绑定 IP' if account.ip else '没有绑定 IP'}")
            if account.can_withdraw:
                _print("[!]", "这把 key 开了提现权限。机器人用不到，建议在 OKX 里关掉")
            if not account.ip:
                _print("[!]", "建议在 OKX 里把这把 key 绑定到这台服务器的 IP")
        except (OkxError, OkxNetworkError) as exc:
            failures += 1
            _print("[X]", f"账户读取失败：{exc}")
            _print("   ", "常见原因：key 填错、Passphrase 不对、这台机器的 IP 不在这把 key 的白名单里")
    return failures


def _check_mt5(cfg, reader) -> int:
    """Section 3 of the self-check with BROKER=mt5: the terminal, the account and the symbols. Reads only."""
    from .market import fmt_price
    from .okx_client import OkxError, OkxNetworkError

    print("\n== 3. MT5 账户（只读取，不下单） ==")
    try:
        state = reader.account_state()
    except (OkxError, OkxNetworkError) as exc:
        _print("[X]", f"MT5 账户读取失败：{exc}")
        return 1
    failures = 0
    kind = "模拟账户" if reader.demo else "实盘账户"
    _print("[OK]", f"MT5 已登录，{kind}，权益 {state.snapshot.total_eq:,.2f} {reader.account_currency()}，当前 {len(state.positions)} 个持仓")
    if cfg.trading == "live" and reader.demo:
        _print("[!]", "TRADING=live，但登录的是模拟账户：下单会被拒绝")
    if cfg.trading == "demo" and not reader.demo:
        _print("[!]", "TRADING=demo，但登录的是实盘账户：下单会被拒绝")
    account = reader.account_config()
    _print("[OK]" if account.can_trade else "[!]", "可以自动交易" if account.can_trade else "现在不能自动交易：MT5 工具栏上的“算法交易”（Algo Trading）没打开，或者账户不允许")
    _print("[OK]", f"持仓模式：{'对冲（多空分开）' if account.hedge_mode else '净持仓'}")
    for base, _ in cfg.mt5_symbols:
        inst_id = f"{base}-USD-SWAP"
        try:
            spec = reader.instrument_spec(inst_id)
            ticker = reader.ticker(inst_id)
            _print("[OK]", f"{inst_id} → MT5 的 {reader.symbol(inst_id)}：1 手 = {spec.coins_per_contract:g} {spec.ct_val_ccy}，"
                           f"最小 {spec.min_sz:g} 手，步长 {spec.lot_sz:g}，现价 {fmt_price(ticker.last)}")
        except (OkxError, OkxNetworkError) as exc:
            failures += 1
            _print("[X]", f"{inst_id} 读不到：{exc}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
