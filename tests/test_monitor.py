from __future__ import annotations

from bot.journal import Journal
from bot.models import StopOrder
from bot.monitor import Monitor, check_risk, diff_positions
from bot.okx_client import OkxError, OkxNetworkError

from .helpers import Clock, FakeOkx, make_cfg, make_position, make_state


def keyed(*positions):
    return {position.key: position for position in positions}


def kinds(events):
    return [event.kind for event in events]


# ------------------------------------------------------------ diff_positions


def test_no_change_means_no_events():
    position = make_position()
    assert diff_positions(keyed(position), keyed(make_position(mark_px=2700.0, upl=99.0))) == []


def test_open_increase_reduce_close():
    position = make_position()
    assert kinds(diff_positions({}, keyed(position))) == ["opened"]
    assert kinds(diff_positions(keyed(position), keyed(make_position(qty=3.0)))) == ["increased"]
    assert kinds(diff_positions(keyed(position), keyed(make_position(qty=0.5)))) == ["reduced"]
    assert kinds(diff_positions(keyed(position), {})) == ["closed"]


def test_long_and_short_are_separate_positions():
    long_position = make_position()
    short_position = make_position(direction="short", pos_side="net")
    events = diff_positions(keyed(long_position), keyed(short_position))
    assert sorted(kinds(events)) == ["closed", "opened"]


def test_moved_stop_is_reported_with_both_prices():
    before = make_position()
    after = make_position(stops=(StopOrder("a1", sl_trigger_px=2610.0, tp_trigger_px=2700.0),))
    [event] = diff_positions(keyed(before), keyed(after))
    assert event.kind == "stop_changed"
    assert event.private_lines == ("止损 2,630.00 变为 2,610.00",)


def test_removed_stop_is_reported():
    [event] = diff_positions(keyed(make_position()), keyed(make_position(stops=())))
    assert "止损 2,630.00 变为 无" in event.private_lines


def test_group_lines_never_carry_sizes_or_money():
    before = make_position()
    changes = [
        diff_positions({}, keyed(before)),
        diff_positions(keyed(before), keyed(make_position(qty=5.0, avg_px=2655.0))),
        diff_positions(keyed(before), keyed(make_position(qty=1.0))),
        diff_positions(keyed(before), keyed(make_position(stops=()))),
        diff_positions(keyed(before), {}),
    ]
    for events in changes:
        for event in events:
            assert event.to_group
            public = " ".join(event.public_lines)
            assert "张" not in public and " USD" not in public and "盈亏" not in public


# ---------------------------------------------------------------- check_risk


def test_healthy_account_raises_nothing():
    assert check_risk(make_state(), make_cfg(alert_daily_loss_usd=500.0), day_start_equity=10000.0) == []


def test_low_account_margin_ratio():
    [event] = check_risk(make_state(mgn_ratio=2.5), make_cfg(), None)
    assert event.kind == "margin_warning" and event.warning and not event.to_group
    assert "250%" in event.private_lines[0]


def test_low_isolated_margin_ratio():
    position = make_position(mgn_mode="isolated", mgn_ratio=1.8)
    [event] = check_risk(make_state([position], mgn_ratio=None), make_cfg(), None)
    assert event.kind == "margin_warning" and event.inst_id == "XAU-USDT-SWAP"


def test_close_to_liquidation():
    position = make_position(mark_px=2000.0, liq_px=1920.0)  # 4% away, alert line 5%
    [event] = check_risk(make_state([position]), make_cfg(), None)
    assert event.kind == "liq_warning" and "4.00%" in event.private_lines[0]


def test_daily_loss_line():
    cfg = make_cfg(alert_daily_loss_usd=300.0)
    assert check_risk(make_state(total_eq=9750.0), cfg, 10000.0) == []
    [event] = check_risk(make_state(total_eq=9700.0), cfg, 10000.0)
    assert event.kind == "daily_loss"
    assert check_risk(make_state(total_eq=9000.0), cfg, None) == []  # no start-of-day figure yet


def test_alerts_can_be_switched_off():
    cfg = make_cfg(alert_margin_ratio=None, alert_liq_distance_pct=None)
    position = make_position(mark_px=2000.0, liq_px=1990.0)
    assert check_risk(make_state([position], mgn_ratio=1.1), cfg, None) == []


def test_risk_warnings_have_no_group_text():
    position = make_position(mark_px=2000.0, liq_px=1990.0, mgn_mode="isolated", mgn_ratio=1.2)
    events = check_risk(make_state([position], mgn_ratio=1.1, total_eq=1.0), make_cfg(alert_daily_loss_usd=1.0), 10000.0)
    assert len(events) == 4
    assert all(not event.to_group and event.public_lines == () for event in events)


# ------------------------------------------------------------------- Monitor


def build(state=None, **cfg_overrides):
    okx = FakeOkx(state if state is not None else make_state())
    events = []
    clock = Clock(1_800_000_000.0)
    journal = Journal(":memory:")
    monitor = Monitor(okx, make_cfg(**cfg_overrides), journal, events.append, clock)
    return monitor, okx, events, clock, journal


def test_first_poll_is_a_silent_baseline():
    monitor, _, events, _, journal = build()
    state = monitor.poll_once()
    assert events == []
    assert monitor.state is state
    assert journal.first_equity_since(0) == 10000.0  # a snapshot was stored


def test_changes_after_the_baseline_are_reported_and_journaled():
    monitor, okx, events, clock, journal = build()
    monitor.poll_once()
    clock.advance(30)
    okx.state = make_state([make_position(qty=3.0)])
    monitor.poll_once()
    assert kinds(events) == ["increased"]
    [row] = journal.events_between(0, clock.now + 1)
    assert row["kind"] == "increased" and "2 张变为 3 张" in row["summary"]


def test_closing_everything_is_reported():
    monitor, okx, events, clock, _ = build()
    monitor.poll_once()
    clock.advance(30)
    okx.state = make_state([])
    monitor.poll_once()
    assert kinds(events) == ["closed"]


def test_repeated_warning_waits_for_the_cooldown():
    monitor, _, events, clock, _ = build(make_state(mgn_ratio=2.0), alert_cooldown_minutes=30)
    monitor.poll_once()
    assert kinds(events) == ["margin_warning"]  # warnings fire even on the first poll
    clock.advance(10 * 60)
    monitor.poll_once()
    assert kinds(events) == ["margin_warning"]
    clock.advance(21 * 60)
    monitor.poll_once()
    assert kinds(events) == ["margin_warning", "margin_warning"]


def test_okx_outage_is_reported_once_and_recovery_once():
    monitor, okx, events, clock, _ = build()
    monitor.poll_once()
    okx.error = OkxNetworkError("连不上 OKX")
    for _ in range(5):
        clock.advance(30)
        assert monitor.poll_once() is None
    assert kinds(events) == ["okx_down"]
    assert monitor.state is not None  # the last good read is kept
    okx.error = None
    clock.advance(30)
    monitor.poll_once()
    assert kinds(events) == ["okx_down", "okx_recovered"]


def test_two_failures_are_not_yet_an_outage_and_do_not_fake_a_close():
    monitor, okx, events, clock, _ = build()
    monitor.poll_once()
    okx.error = OkxError("50001", "Service temporarily unavailable")
    monitor.poll_once()
    monitor.poll_once()
    okx.error = None
    monitor.poll_once()
    assert events == []


def test_a_failing_notifier_does_not_stop_the_monitor():
    okx = FakeOkx(make_state())

    def broken(event):
        raise RuntimeError("Feishu is down")

    monitor = Monitor(okx, make_cfg(), Journal(":memory:"), broken, Clock(1_800_000_000.0))
    monitor.poll_once()
    okx.state = make_state([])
    assert monitor.poll_once() is not None


def test_day_start_equity_is_the_first_snapshot_of_the_local_day():
    monitor, okx, _, clock, _ = build()
    monitor.poll_once()
    okx.state = make_state(total_eq=9000.0)
    clock.advance(16 * 60)
    monitor.poll_once()
    assert monitor.day_start_equity() == 10000.0


# ------------------------------------------------------ positions without a stop


def bare(**overrides):
    return make_state([make_position(stops=(), **overrides)])


def test_a_position_left_without_a_stop_is_pointed_out_once_and_again_a_day_later():
    monitor, _, events, clock, _ = build(bare())
    monitor.poll_once()
    clock.advance(9 * 60)
    monitor.poll_once()
    assert events == []  # ten minutes' grace: the stop may be on its way
    clock.advance(61)
    monitor.poll_once()
    [event] = events
    assert event.kind == "no_stop" and event.title == "还没有止损：XAU-USDT-SWAP 多单" and event.warning
    text = "\n".join(event.private_lines)
    assert "数量 2 张，均价 2,650.00，标记价 2,660.00，浮动盈亏 +20.00 USDT" in text
    assert "预估强平价 2,400.00，离现在的标记价 9.77%" in text
    assert "设不设、设在哪由你决定" in text and "每 24 小时提醒一次" in text

    clock.advance(23 * 3600)
    monitor.poll_once()
    assert len(events) == 1
    clock.advance(3600)
    monitor.poll_once()
    assert kinds(events) == ["no_stop", "no_stop"]


def test_setting_a_stop_ends_it_and_taking_it_off_starts_again():
    monitor, okx, events, clock, _ = build(bare())
    monitor.poll_once()
    clock.advance(11 * 60)
    monitor.poll_once()
    okx.state = make_state()  # the stop is on
    clock.advance(60)
    monitor.poll_once()
    okx.state = bare()  # and off again an hour later
    clock.advance(3600)
    monitor.poll_once()
    assert kinds(events) == ["no_stop", "stop_changed", "stop_changed"]
    clock.advance(11 * 60)
    monitor.poll_once()
    assert kinds(events)[-1] == "no_stop" and kinds(events).count("no_stop") == 2  # not held back by the day's wait


def test_a_restart_does_not_repeat_the_reminder():
    monitor, okx, events, clock, journal = build(bare())
    monitor.poll_once()
    clock.advance(11 * 60)
    monitor.poll_once()
    assert kinds(events) == ["no_stop"]

    again = []
    restarted = Monitor(okx, make_cfg(), journal, again.append, clock)
    restarted.poll_once()
    clock.advance(11 * 60)
    restarted.poll_once()
    assert again == []
    clock.advance(24 * 3600)
    restarted.poll_once()
    assert kinds(again) == ["no_stop"]


def test_a_closed_position_is_forgotten():
    monitor, okx, events, clock, _ = build(bare())
    monitor.poll_once()
    clock.advance(11 * 60)
    monitor.poll_once()
    okx.state = make_state([])
    clock.advance(60)
    monitor.poll_once()
    okx.state = bare()  # opened again, still without a stop
    clock.advance(60)
    monitor.poll_once()
    clock.advance(11 * 60)
    monitor.poll_once()
    assert kinds(events) == ["no_stop", "closed", "opened", "no_stop"]


def test_the_reminder_can_be_switched_off_or_given_more_time():
    monitor, _, events, clock, _ = build(bare(), alert_no_stop_minutes=None)
    monitor.poll_once()
    clock.advance(3600)
    monitor.poll_once()
    assert events == []

    monitor, _, events, clock, _ = build(bare(), alert_no_stop_minutes=60)
    monitor.poll_once()
    clock.advance(59 * 60)
    monitor.poll_once()
    assert events == []
    clock.advance(61)
    monitor.poll_once()
    assert kinds(events) == ["no_stop"] and "已经 60 分钟以上没有止损单" in "\n".join(events[0].private_lines)


def test_a_take_profit_alone_is_not_a_stop():
    from bot.models import StopOrder

    monitor, _, events, clock, _ = build(make_state([make_position(stops=(StopOrder("a1", None, 2700.0),))]))
    monitor.poll_once()
    clock.advance(11 * 60)
    monitor.poll_once()
    assert kinds(events) == ["no_stop"]


def test_stops_that_could_not_be_read_are_neither_gone_nor_missing():
    from dataclasses import replace

    monitor, okx, events, clock, _ = build()
    monitor.poll_once()
    unread = replace(make_state([make_position(stops=())]), stops_known=False)  # OKX did not answer for the stop orders
    okx.state = unread
    for _ in range(3):
        clock.advance(11 * 60)
        monitor.poll_once()
    assert events == []  # no "止损 变为 无", no reminder
    assert monitor.state.positions[0].stop_loss == 2630.0  # what was last seen stands

    monitor, okx, events, clock, _ = build(replace(bare(), stops_known=False))  # unknown from the very start
    monitor.poll_once()
    clock.advance(11 * 60)
    monitor.poll_once()
    assert events == []


def test_the_reminder_says_how_to_set_one_only_when_that_would_reach_this_account():
    def text(**cfg):
        monitor, _, events, clock, _ = build(bare(), **cfg)
        monitor.poll_once()
        clock.advance(11 * 60)
        monitor.poll_once()
        return "\n".join(events[0].private_lines)

    hint = "私聊发“止损 XAU 价格”"
    assert hint in text(trading="live")
    assert hint not in text(trading="live", trade_actions=("close",))
    assert hint not in text(trading="demo")  # an order would go to the demo account, not to this position
    assert hint not in text()
