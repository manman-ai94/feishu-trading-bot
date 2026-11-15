from __future__ import annotations

from zoneinfo import ZoneInfo

from bot.analyst import AnalystUnavailable
from bot.journal import Journal
from bot.monitor import Event, Monitor, diff_positions
from bot.reports import DISCLAIMER, account_text, equity_text, event_markdown, journal_text, status_markdown
from bot.service import Reporter

from .helpers import GROUP, OPEN_GROUP, OWNER, Clock, FakeAnalyst, FakeFeishu, FakeOkx, make_cfg, make_position, make_state

LA = ZoneInfo("America/Los_Angeles")


def build(state="default", **cfg_overrides):
    cfg = make_cfg(**cfg_overrides)
    feishu, analyst = FakeFeishu(), FakeAnalyst()
    okx = FakeOkx(make_state() if state == "default" else state)
    clock = Clock(1_800_000_000.0)
    journal = Journal(":memory:")
    reporter = Reporter(cfg, feishu, okx, analyst, journal, clock)
    reporter.monitor = Monitor(okx, cfg, journal, reporter.notify_event, clock)
    if okx.state is not None:
        reporter.monitor.poll_once()
    return reporter, feishu, analyst, okx, clock, journal


# ------------------------------------------------------------------- reports


def test_private_status_has_everything():
    text = status_markdown(make_state(), day_change=-35.5, private=True)
    for expected in ("账户权益", "10,000.00 USD", "-35.50 USD", "保证金率 1250%", "数量 2 张", "杠杆 10 倍", "预估强平价 2,400.00", "止损 2,630.00"):
        assert expected in text


def test_public_status_has_levels_only():
    text = status_markdown(make_state(), day_change=-35.5, private=False)
    assert "均价 2,650.00" in text and "止损 2,630.00，止盈 2,700.00" in text
    for secret in (" USD", "账户权益", "张", "盈亏", "强平", "杠杆", "保证金"):
        assert secret not in text


def test_account_text_for_the_ai():
    text = account_text(make_state(), day_change=12.0)
    assert "账户权益 10,000.00 USD" in text and "XAU-USDT-SWAP 多单 2 张" in text and "今日权益变化 +12.00 USD" in text
    assert "当前没有持仓。" in account_text(make_state([]), None)


def test_event_markdown_picks_the_right_audience():
    [event] = diff_positions({}, {make_position().key: make_position()})
    assert "数量 2 张" in event_markdown(event, private=True)
    assert "张" not in event_markdown(event, private=False)


def test_journal_text_hides_scheduler_bookkeeping():
    events = [
        {"ts": 1_800_000_000, "kind": "opened", "summary": "开仓：XAU-USDT-SWAP 多单"},
        {"ts": 1_800_000_060, "kind": "job_morning", "summary": "2027-01-15 00:01"},
    ]
    text = journal_text(events, LA)
    assert "开仓：XAU-USDT-SWAP 多单" in text and "job" not in text and "2027-01-15" not in text
    assert journal_text([events[1]], LA) == "没有记录。"
    assert journal_text([], LA) == "没有记录。"


def test_equity_text():
    assert equity_text([(1, 100.0)]) == "记录太少，无法比较。"
    text = equity_text([(1, 100.0), (2, 130.0), (3, 90.0)])
    assert "变化 -10.00 USD" in text and "最高 130.00 USD" in text and "最低 90.00 USD" in text


# ------------------------------------------------------------------ Reporter


def test_position_event_goes_to_both_in_full_by_default():
    reporter, feishu, _, okx, clock, _ = build()
    okx.state = make_state([make_position(qty=3.0, avg_px=2655.0)])
    clock.advance(30)
    reporter.monitor.poll_once()
    [to_owner] = feishu.to(OWNER)
    [to_group] = feishu.to(GROUP)
    assert to_group.body == to_owner.body and "2 张变为 3 张" in to_group.body
    assert to_owner.footer is None and to_group.footer == DISCLAIMER


def test_levels_only_group_gets_a_different_version():
    reporter, feishu, _, okx, clock, _ = build(**OPEN_GROUP)
    okx.state = make_state([make_position(qty=3.0, avg_px=2655.0)])
    clock.advance(30)
    reporter.monitor.poll_once()

    [to_owner] = feishu.to(OWNER)
    assert to_owner.receive_id_type == "open_id" and "2 张变为 3 张" in to_owner.body and to_owner.footer is None
    [to_group] = feishu.to(GROUP)
    assert to_group.receive_id_type == "chat_id" and to_group.title == "加仓：XAU-USDT-SWAP 多单"
    assert "张" not in to_group.body and "2,655.00" in to_group.body
    assert to_group.footer == DISCLAIMER


def test_group_is_still_told_when_the_private_chat_fails():
    reporter, feishu, analyst, okx, clock, _ = build()
    real_send = feishu.send_markdown

    def flaky(receive_id, *args, **kwargs):
        if receive_id == OWNER:
            raise RuntimeError("Feishu refused the private message")
        real_send(receive_id, *args, **kwargs)

    feishu.send_markdown = flaky
    okx.state = make_state([])
    clock.advance(30)
    reporter.monitor.poll_once()
    reporter.send_brief("morning")
    assert [(item.target, item.title.split(" · ")[0]) for item in feishu.sent] == [(GROUP, "平仓：XAU-USDT-SWAP 多单"), (GROUP, "盘前简报")]


def test_risk_warning_is_shared_with_the_group_by_default():
    reporter, feishu, *_ = build()
    reporter.notify_event(Event(kind="margin_warning", title="保证金率偏低", private_lines=("账户保证金率 250%",), warning=True))
    assert [(item.target, item.template, item.body) for item in feishu.sent] == [
        (OWNER, "red", "- 账户保证金率 250%"),
        (GROUP, "red", "- 账户保证金率 250%"),
    ]


def test_risk_warning_stays_private_in_a_levels_only_group():
    reporter, feishu, *_ = build(group_show_pnl=False)
    reporter.notify_event(Event(kind="margin_warning", title="保证金率偏低", private_lines=("账户保证金率 250%",), warning=True))
    [sent] = feishu.sent
    assert sent.target == OWNER and sent.template == "red"


def test_technical_notices_never_go_to_the_group():
    reporter, feishu, *_ = build()
    reporter.notify_event(Event(kind="okx_down", title="读不到 OKX 账户", private_lines=("原因：IP 不在白名单",), warning=True))
    reporter.notify_event(Event(kind="okx_recovered", title="OKX 账户读取已恢复", private_lines=("监控恢复正常。",)))
    assert [item.target for item in feishu.sent] == [OWNER, OWNER]


def test_group_can_show_amounts_when_opted_in():
    reporter, feishu, _, okx, clock, _ = build(group_show_pnl=True)
    okx.state = make_state([])
    clock.advance(30)
    reporter.monitor.poll_once()
    [to_group] = feishu.to(GROUP)
    assert "原数量 2 张" in to_group.body


def test_nothing_is_sent_to_a_group_that_is_not_configured():
    reporter, feishu, _, okx, clock, _ = build(group_chat_id="")
    okx.state = make_state([])
    clock.advance(30)
    reporter.monitor.poll_once()
    assert [item.target for item in feishu.sent] == [OWNER]


def test_startup_message():
    reporter, feishu, *_ = build()
    reporter.send_startup()
    [sent] = feishu.sent
    assert sent.target == OWNER and "开始监控" in sent.body and "账户权益" in sent.body

    reporter, feishu, *_ = build(state=None)
    reporter.send_startup()
    assert "还没读到 OKX 账户" in feishu.sent[0].body


def test_brief_is_written_once_and_shared_in_full_by_default():
    reporter, feishu, analyst, *_ = build()
    reporter.send_brief("morning")
    [call] = analyst.named("brief")
    assert call["for_owner"] is True and call["shared_with_group"] is True and "账户权益" in call["account_text"]
    to_owner, to_group = feishu.sent
    assert (to_owner.target, to_group.target) == (OWNER, GROUP)
    assert to_owner.body == to_group.body == "AI:brief"
    assert to_owner.footer is None and to_group.footer == DISCLAIMER


def test_levels_only_group_gets_its_own_brief_without_the_account():
    reporter, feishu, analyst, *_ = build(group_show_pnl=False)
    reporter.send_brief("morning")
    owner_call, group_call = analyst.named("brief")
    assert owner_call["for_owner"] is True and owner_call["shared_with_group"] is False and "账户权益" in owner_call["account_text"]
    assert owner_call["journal_text"] is None  # the day's trades belong in the evening wrap
    assert group_call["for_owner"] is False and group_call["account_text"] is None and group_call["journal_text"] is None
    assert [item.target for item in feishu.sent] == [OWNER, GROUP]
    assert feishu.sent[0].title.startswith("盘前简报") and feishu.sent[0].footer is None
    assert feishu.sent[1].footer == DISCLAIMER


def test_evening_wrap_gives_the_owner_todays_journal():
    reporter, _, analyst, okx, clock, _ = build(group_show_pnl=False)
    okx.state = make_state([make_position(qty=3.0)])
    clock.advance(30)
    reporter.monitor.poll_once()
    reporter.send_brief("evening")
    owner_call, group_call = analyst.named("brief")
    assert "加仓：XAU-USDT-SWAP 多单" in owner_call["journal_text"]
    assert group_call["journal_text"] is None


def test_brief_failure_tells_the_owner_and_stays_out_of_the_group():
    reporter, feishu, analyst, *_ = build()
    analyst.error = AnalystUnavailable("Claude 的 API key 无效")
    reporter.send_brief("morning")
    [sent] = feishu.sent
    assert sent.target == OWNER and "没有生成" in sent.body and "API key 无效" in sent.body


def test_weekly_review_is_shared_by_default():
    reporter, feishu, analyst, *_ = build()
    reporter.send_weekly_review()
    [call] = analyst.named("weekly")
    assert call["shared_with_group"] is True
    assert [(item.target, item.footer) for item in feishu.sent] == [(OWNER, None), (GROUP, DISCLAIMER)]
    assert feishu.sent[0].body == feishu.sent[1].body


def test_weekly_review_stays_private_in_a_levels_only_group():
    reporter, feishu, analyst, okx, clock, _ = build(group_show_pnl=False)
    okx.state = make_state([], total_eq=10100.0)
    clock.advance(3600)
    reporter.monitor.poll_once()
    feishu.sent.clear()
    reporter.send_weekly_review()
    [call] = analyst.named("weekly")
    assert "平仓：XAU-USDT-SWAP 多单" in call["journal_text"] and "变化 +100.00 USD" in call["equity_text"]
    assert [item.target for item in feishu.sent] == [OWNER]
    assert feishu.sent[0].title.startswith("本周复盘")
