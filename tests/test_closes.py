"""How positions closed (OKX's own records) and the circuit breaker built on them."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from bot.app import build_trader
from bot.closes import Closed, Trigger, close_lines, how_it_closed, read_closed, read_triggers, stop_hits
from bot.config import load_config
from bot.journal import Journal
from bot.monitor import Event
from bot.okx_trade import _stop_fields
from bot.service import Reporter

from .helpers import OWNER, Clock, FakeAnalyst, FakeFeishu, FakeOkx, make_cfg
from .test_trading import XAU, Bench, FakeExchange, gold

LA = ZoneInfo("America/Los_Angeles")
NOW = 1_800_000_000.0  # the Bench clock
HOUR = 3600


def trigger_row(at: float, side: str = "sl", inst_id: str = XAU, state: str = "effective", **extra) -> dict:
    row = {
        "instId": inst_id, "algoId": f"a{int(at)}", "ordType": "conditional", "state": state, "actualSide": side,
        "triggerTime": str(int(at * 1000)), "slTriggerPx": "3816", "tpTriggerPx": "3938",
    }  # fmt: skip
    row.update(extra)
    return row


def closed_row(at: float, realized: str = "-306.7", close_type: str = "2", inst_id: str = XAU, **extra) -> dict:
    row = {
        "instId": inst_id, "direction": "long", "type": close_type, "openAvgPx": "3844.8", "closeAvgPx": "3815.5",
        "closeTotalPos": "10000", "pnl": "-293", "fee": "-11.6", "fundingFee": "-2.1", "liqPenalty": "0",
        "realizedPnl": realized, "ccy": "USDT", "uTime": str(int(at * 1000)),
    }  # fmt: skip
    row.update(extra)
    return row


class Source:
    """OKX's two history endpoints."""

    def __init__(self, triggered=(), closed=(), oco=()):
        self.triggered = {"conditional": list(triggered), "oco": list(oco)}
        self.closed = list(closed)
        self.asked: list[str] = []

    def algo_history(self, ord_type):
        self.asked.append(ord_type)
        return self.triggered[ord_type]

    def position_history(self):
        return self.closed


# ---------------------------------------------------------------- reading the records


def test_the_records_are_read_from_both_order_types_and_sorted_oldest_first():
    source = Source(
        triggered=[trigger_row(NOW - HOUR), trigger_row(NOW - 5 * HOUR, side="tp"), trigger_row(NOW, side="")],
        oco=[trigger_row(NOW - 3 * HOUR, state="canceled"), trigger_row(NOW - 2 * HOUR)],
        closed=[closed_row(NOW - HOUR), {"instId": XAU}, "junk"],
    )
    triggers = read_triggers(source)
    assert source.asked == ["conditional", "oco"]
    assert [(t.side, t.at) for t in triggers] == [("tp", NOW - 5 * HOUR), ("sl", NOW - 2 * HOUR), ("sl", NOW - HOUR)]
    assert triggers[0].trigger_px == 3938.0 and triggers[1].trigger_px == 3816.0
    closed = read_closed(source)
    assert closed == [
        Closed(XAU, "long", "2", 3844.8, 3815.5, 10000.0, -293.0, -11.6, -2.1, 0.0, -306.7, "USDT", NOW - HOUR)
    ]


def test_how_a_position_closed():
    closed = Closed.from_okx(closed_row(NOW))
    assert how_it_closed(closed, [Trigger(XAU, "sl", NOW - 1, 3816.0)]) == "止损单触发"
    assert how_it_closed(closed, [Trigger(XAU, "tp", NOW - 2, 3938.0)]) == "止盈单触发"
    assert how_it_closed(closed, [Trigger("BTC-USDT-SWAP", "sl", NOW - 1, 1.0)]) == "平仓单（不是止盈止损单触发的）"
    assert how_it_closed(closed, [Trigger(XAU, "sl", NOW - 600, 3816.0)]) == "平仓单（不是止盈止损单触发的）"  # too long before
    assert how_it_closed(Closed.from_okx(closed_row(NOW, close_type="3")), []) == "强平"
    assert how_it_closed(Closed.from_okx(closed_row(NOW, close_type="6")), []) == "自动减仓 (ADL)"


def test_the_close_alert_lines_carry_the_real_numbers():
    closed = Closed.from_okx(closed_row(NOW))
    assert close_lines(closed, [Trigger(XAU, "sl", NOW - 1, 3816.0)]) == (
        "平仓方式：止损单触发（全部平仓）",
        "开仓均价 3,844.80，平仓均价 3,815.50",
        "已实现盈亏 -306.70 USDT（价差 -293.00，手续费 -11.60，资金费 -2.10）",
    )


# ------------------------------------------------------------------ what counts


def test_a_losing_stop_out_and_a_liquidation_count_a_stop_in_profit_does_not():
    triggers = [
        Trigger(XAU, "sl", NOW - 20 * HOUR, 3816.0),  # lost money
        Trigger(XAU, "sl", NOW - 10 * HOUR, 3900.0),  # a stop moved into profit
        Trigger(XAU, "tp", NOW - 8 * HOUR, 3938.0),  # a take-profit is never a hit
        Trigger(XAU, "sl", NOW - 2 * HOUR, 3816.0),  # its close is not in the records yet: counts anyway
    ]
    closed = [
        Closed.from_okx(closed_row(NOW - 20 * HOUR + 2)),
        Closed.from_okx(closed_row(NOW - 10 * HOUR + 2, realized="55.0")),
        Closed.from_okx(closed_row(NOW - 8 * HOUR + 2, realized="900.0")),
        Closed.from_okx(closed_row(NOW - 5 * HOUR, realized="-1700", close_type="3")),
    ]
    hits = stop_hits(closed, triggers, NOW - 24 * HOUR)
    assert [(hit.kind, hit.at, hit.realized) for hit in hits] == [
        ("止损", NOW - 20 * HOUR, -306.7),
        ("强平", NOW - 5 * HOUR, -1700.0),
        ("止损", NOW - 2 * HOUR, None),
    ]
    assert [hit.at for hit in stop_hits(closed, triggers, NOW - 6 * HOUR)] == [NOW - 5 * HOUR + 0, NOW - 2 * HOUR]


# ------------------------------------------------------------- the circuit breaker


def tripped_bench(trading: str = "live", **cfg) -> Bench:
    bench = Bench(FakeExchange([]), trading=trading, **cfg)
    bench.exchange.triggered["conditional"] = [trigger_row(NOW - 3 * HOUR), trigger_row(NOW - HOUR)]
    bench.exchange.closed = [closed_row(NOW - 3 * HOUR + 2), closed_row(NOW - HOUR + 2, realized="-120.5")]
    return bench


def test_two_losing_stop_outs_in_a_day_stop_new_positions():
    bench = tripped_bench()
    text = bench.say("做多 XAU 10张").text
    assert text.startswith("熔断中，不接受开仓指令。过去 24 小时内止损或强平 2 次，你定的规则是满 2 次就暂停开仓：")
    assert "XAU-USDT-SWAP 止损，已实现 -306.70 USDT" in text and "已实现 -120.50 USDT" in text
    assert "发“解除熔断”，再回确认码" in text
    assert bench.say("限价做多 XAU 10张 3800").text.startswith("熔断中")
    assert bench.sent == []


def test_the_breaker_only_ever_stops_new_positions():
    bench = tripped_bench()
    bench.exchange.positions = [gold()]
    assert "确认 1111" in bench.say("止损 XAU 3830").text
    assert bench.say("确认 1111").title.startswith("已执行")
    assert "确认 2222" in bench.say("平仓 XAU").text


def test_one_stop_out_is_not_enough_and_old_ones_drop_out():
    bench = tripped_bench()
    bench.exchange.triggered["conditional"] = [trigger_row(NOW - 30 * HOUR), trigger_row(NOW - HOUR)]
    assert "确认 1111" in bench.say("做多 XAU 10张").text


def test_lifting_the_breaker_takes_a_code_and_forgets_the_stop_outs_before_it():
    bench = tripped_bench()
    preview = bench.say("解除熔断")
    assert preview.title == "解除熔断 · 预览" and "确认 1111" in preview.text and "上面这几次不再计入" in preview.text
    assert bench.say("做多 XAU 10张").text.startswith("熔断中")  # a new command voids the preview; still tripped
    assert "确认 2222" in bench.say("恢复开仓").text
    reply = bench.say("确认 2222")
    assert reply.text.startswith("熔断已解除，可以开仓了。")
    assert "确认 3333" in bench.say("做多 XAU 10张").text
    assert bench.sent == []  # lifting it sends nothing to OKX
    # a fresh stop-out after the reset counts again, one is not yet enough
    bench.clock.advance(HOUR)
    bench.exchange.triggered["conditional"].append(trigger_row(NOW + HOUR - 60))
    assert "确认 4444" in bench.say("做多 XAU 10张").text


def test_a_no_to_the_lifting_preview_voids_it():
    bench = tripped_bench()
    bench.say("解除熔断")
    assert bench.say("取消 1111").text.startswith("已作废：解除熔断")
    assert bench.say("做多 XAU 10张").text.startswith("熔断中")


def test_there_is_nothing_to_lift_when_it_has_not_tripped():
    bench = Bench(FakeExchange([]), trading="live")
    assert bench.say("解除熔断").text.startswith("熔断没有触发：过去 24 小时内止损或强平 0 次")


def test_when_the_records_cannot_be_read_no_position_is_opened():
    from bot.okx_client import OkxNetworkError

    bench = Bench(FakeExchange([]), trading="live")
    bench.exchange.history_failures = [OkxNetworkError("down")]
    assert bench.say("做多 XAU 10张").text.startswith("读不到 OKX 的止损和平仓记录，熔断规则没法核对，这次不开仓。")
    assert "确认 1111" in bench.say("做多 XAU 10张").text  # read again next time


def test_the_breaker_can_be_switched_off_and_its_window_set():
    assert "确认 1111" in tripped_bench(trade_breaker_stops=0).say("做多 XAU 10张").text
    assert tripped_bench(trade_breaker_stops=0).say("解除熔断").text.startswith("熔断没有打开")
    assert "确认 1111" in tripped_bench(trade_breaker_hours=2).say("做多 XAU 10张").text  # only one in the last 2 hours
    assert tripped_bench(trade_breaker_stops=3).say("解除熔断").text.startswith("熔断没有触发")


def test_the_settings_card_shows_where_the_breaker_stands():
    lines = "\n".join(tripped_bench().desk.status_lines())
    assert "- 熔断：**已触发**（2 次），不接受开仓，发“解除熔断”恢复。规则：24 小时内止损或强平满 2 次就暂停开仓" in lines
    lines = "\n".join(Bench(FakeExchange([]), trading="live").desk.status_lines())
    assert "- 熔断：24 小时内止损或强平满 2 次就暂停开仓；现在 0 次" in lines and "触发" not in lines.split("熔断")[1][:12]
    assert "按最新成交价触发" not in lines
    assert "- 机器人挂的止损止盈按标记价触发" in "\n".join(Bench(trading="live", trade_stop_trigger="mark").desk.status_lines())


def test_the_owner_is_told_once_when_it_trips_and_only_if_he_could_open():
    bench = tripped_bench()
    text = bench.desk.breaker_alert()
    assert text.startswith("过去 24 小时内止损或强平 2 次") and "我没有动任何仓位和挂单" in text
    assert bench.desk.breaker_alert() == ""  # the same stop-outs are not news twice
    assert tripped_bench(trade_actions=("stops", "close", "cancel")).desk.breaker_alert() == ""  # opening is closed anyway
    assert tripped_bench(trading="off").desk.breaker_alert() == ""


def test_a_close_is_followed_by_the_breaker_notice_in_the_private_chat_only():
    feishu = FakeFeishu()
    reporter = Reporter(make_cfg(), feishu, FakeOkx(), FakeAnalyst(), Journal(":memory:"), Clock(NOW))
    reporter.breaker_alert = lambda: "过去 24 小时内止损或强平 2 次"
    reporter.notify_event(Event(kind="closed", title="平仓：XAU-USDT-SWAP 多单", inst_id=XAU, to_group=True))
    cards = [(sent.target, sent.title) for sent in feishu.sent]
    assert (OWNER, "熔断：开仓已暂停") in cards and len([c for c in cards if c[1] == "熔断：开仓已暂停"]) == 1

    feishu.sent.clear()
    reporter.notify_event(Event(kind="stop_changed", title="止盈止损变动", inst_id=XAU))
    assert all(sent.title != "熔断：开仓已暂停" for sent in feishu.sent)

    def broken():
        raise RuntimeError("bug")

    reporter.breaker_alert = broken
    reporter.notify_event(Event(kind="closed", title="平仓", inst_id=XAU))  # never takes the alert down with it


# ------------------------------------------------------------ settings and stop prices


def test_breaker_and_stop_trigger_settings(tmp_path, monkeypatch):
    for name in ("TRADE_BREAKER_STOPS", "TRADE_BREAKER_HOURS", "TRADE_STOP_TRIGGER"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    cfg, _ = load_config(env)
    assert (cfg.trade_breaker_stops, cfg.trade_breaker_hours, cfg.trade_stop_trigger) == (2, 24, "last")
    env.write_text("TRADE_BREAKER_STOPS=3\nTRADE_BREAKER_HOURS=500\nTRADE_STOP_TRIGGER=Mark\n", encoding="utf-8")
    cfg, problems = load_config(env)
    assert (cfg.trade_breaker_stops, cfg.trade_breaker_hours, cfg.trade_stop_trigger) == (3, 168, "mark")
    for name in ("TRADE_BREAKER_STOPS", "TRADE_BREAKER_HOURS", "TRADE_STOP_TRIGGER"):
        monkeypatch.delenv(name, raising=False)  # loading .env leaves its values in the environment
    env.write_text("TRADE_STOP_TRIGGER=bid\n", encoding="utf-8")
    cfg, problems = load_config(env)
    assert cfg.trade_stop_trigger == "last" and any("TRADE_STOP_TRIGGER" in problem for problem in problems)
    for name in ("TRADE_BREAKER_STOPS", "TRADE_BREAKER_HOURS", "TRADE_STOP_TRIGGER"):
        monkeypatch.delenv(name, raising=False)


def test_the_stops_the_bot_places_fire_on_the_price_chosen():
    assert _stop_fields("3816", None, "", "mark") == {"slTriggerPx": "3816", "slOrdPx": "-1", "slTriggerPxType": "mark"}
    assert _stop_fields(None, "3938", "new")["newTpTriggerPxType"] == "last"
    trader = build_trader(make_cfg(trading="live", trade_stop_trigger="index"))
    assert trader.trigger_px_type == "index"
    assert build_trader(make_cfg(trading="off")) is None


# ------------------------------------------------------------------- the close alert


def watch(history: list[dict], triggered: list[dict] = ()):
    """A monitor that has seen one XAU position, which is gone at the next read."""
    from bot.monitor import Monitor

    from .helpers import make_position, make_state

    okx = FakeOkx(make_state([make_position()]))
    events, waits = [], []
    clock = Clock(NOW)
    monitor = Monitor(okx, make_cfg(), Journal(":memory:"), events.append, clock, sleep=waits.append)
    monitor.poll_once()
    clock.advance(30)
    okx.state = make_state([])
    okx.closed = history
    okx.triggered = {"conditional": list(triggered)}
    return monitor, okx, events, waits


def test_a_stop_out_is_reported_with_what_okx_says_it_made():
    monitor, _, events, waits = watch([closed_row(NOW + 28)], [trigger_row(NOW + 27)])
    monitor.poll_once()
    [event] = events
    assert event.title == "止损触发：XAU-USDT-SWAP 多单"
    assert event.private_lines == (
        "平仓方式：止损单触发（全部平仓）",
        "开仓均价 3,844.80，平仓均价 3,815.50",
        "已实现盈亏 -306.70 USDT（价差 -293.00，手续费 -11.60，资金费 -2.10）",
        "原数量 2 张",
    )
    assert event.public_lines == ("平仓方式：止损单触发（全部平仓）", "开仓均价 3,844.80，平仓均价 3,815.50")
    assert waits == []


def test_a_liquidation_is_a_warning_and_says_so():
    monitor, _, events, _ = watch([closed_row(NOW + 28, realized="-1700", close_type="3")])
    monitor.poll_once()
    assert events[0].title == "强平：XAU-USDT-SWAP 多单" and events[0].warning
    assert events[0].private_lines[0] == "平仓方式：强平（强平）"


def test_a_record_that_is_late_is_waited_for_once_and_then_the_poll_numbers_are_used():
    old = closed_row(NOW - 5 * HOUR)
    monitor, okx, events, waits = watch([old])
    monitor.poll_once()
    assert waits == [2.0]
    assert events[0].title == "平仓：XAU-USDT-SWAP 多单" and "平仓前浮动盈亏" in events[0].private_lines[1]

    monitor, okx, events, waits = watch([])  # no records at all: nothing to wait for
    monitor.poll_once()
    assert waits == [] and events[0].title == "平仓：XAU-USDT-SWAP 多单"


def test_the_close_alert_goes_out_when_the_records_cannot_be_read():
    monitor, okx, events, _ = watch([closed_row(NOW + 28)])

    def broken():
        raise RuntimeError("no")

    okx.position_history = broken
    monitor.poll_once()
    assert events[0].kind == "closed" and events[0].title == "平仓：XAU-USDT-SWAP 多单"
