"""The demo rehearsal: one command, eight steps, each read back from the exchange."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from bot import __version__
from bot.models import OrderInfo, StopOrder
from bot.okx_client import OkxError, OkxNetworkError
from bot.selftest import CHECK_TRIES, SETTING, STEPS
from bot.trading import PAUSED_SETTING, Command, CommandParser

from .helpers import make_position
from .test_trading import BTC, XAU, Bench, FakeExchange, FakeResolver, small


class Demo(FakeExchange):
    """Keeps a position the way OKX does: a filled order opens or reduces it, a close removes it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orphans: list[dict] = []  # stop orders whose position is gone
        self.leave_stops = False  # a close forgets to take the stop orders away
        self.deaf_to_amend = False  # an amend is accepted and changes nothing
        self.reads_fail = 0  # this many reads of the stop orders fail, after the rehearsal has begun

    def pending_stops(self) -> list[dict]:
        if self.reads_fail and self.changes:
            self.reads_fail -= 1
            raise OkxNetworkError("timed out")
        rows = [{"instId": p.inst_id, "algoId": s.algo_id} for p in self.positions for s in p.stops]
        return rows + list(self.orphans)

    def place_order(self, **order) -> str:
        known = len(self.orders)
        try:
            return super().place_order(**order)
        finally:
            if len(self.orders) > known and order["ord_type"] == "market":
                self._fill(order)

    def _fill(self, order: dict) -> None:
        inst_id, size = order["inst_id"], float(order["sz"])
        held = [p for p in self.positions if p.inst_id == inst_id]
        rest = [p for p in self.positions if p.inst_id != inst_id]
        if order["side"] == "sell":
            left = held[0].qty - size
            self.positions = rest + ([replace(held[0], qty=left)] if left > 0 else [])
            return
        if held:
            self.positions = rest + [replace(held[0], qty=held[0].qty + size)]
            return
        stops = ()
        if order.get("sl_trigger_px"):  # a stop sent with the order is a stop order once it has filled
            stops = (StopOrder("9001", sl_trigger_px=float(order["sl_trigger_px"])),)
        price = self.last[inst_id]
        self.positions = rest + [
            make_position(inst_id=inst_id, qty=size, avg_px=price, mark_px=price, upl=0.0, notional_usd=None, stops=stops, ccy="USDT")
        ]

    def close_position(self, inst_id: str, mgn_mode: str, pos_side: str | None = None, cl_ord_id: str = "") -> None:
        stops = [s for p in self.positions if p.inst_id == inst_id for s in p.stops]
        super().close_position(inst_id, mgn_mode, pos_side, cl_ord_id)
        if self.leave_stops:
            self.orphans += [{"instId": inst_id, "algoId": s.algo_id} for s in stops]

    def amend_stop(self, inst_id, algo_id, sl_trigger_px=None, tp_trigger_px=None) -> None:
        if self.deaf_to_amend:
            self._begin("amend_stop", {"inst_id": inst_id, "algo_id": algo_id})
            return
        super().amend_stop(inst_id, algo_id, sl_trigger_px, tp_trigger_px)

    def cancel_stops(self, items) -> None:
        super().cancel_stops(items)
        gone = {algo_id for _, algo_id in items}
        self.orphans = [row for row in self.orphans if row["algoId"] not in gone]


def calls(bench: Bench) -> list[str]:
    return [name for name, _ in bench.sent]


def empty(exchange: Demo) -> bool:
    return not exchange.positions and not exchange.pending and not exchange.pending_stops()


def last_run(bench: Bench) -> dict:
    return json.loads(bench.journal.get_setting(SETTING))


# ------------------------------------------------------------------- the command


def test_the_words_that_start_it():
    parser = CommandParser(FakeResolver(), lambda: (XAU, BTC))
    assert parser.parse("模拟盘自测") == Command("selftest")
    assert parser.parse("/selftest") == Command("selftest")
    assert parser.parse("模拟盘自测 BTC") == Command("selftest", inst_id=BTC)
    assert parser.parse("模拟盘自测是什么") is None  # a question goes to the AI
    assert parser.parse("模拟盘自测 BTC 和 XAU") is None


def test_the_preview_says_what_will_happen_and_sends_nothing():
    bench = Bench(Demo())
    preview = bench.say("模拟盘自测")
    assert preview.title == "模拟盘自测 · 预览"
    assert XAU in preview.text and "2 张（合 0.002 XAU）" in preview.text and "7.76 USDT" in preview.text
    assert "只在模拟盘" in preview.text and "确认 1111" in preview.text
    assert bench.sent == []
    assert bench.say("取消").text.startswith("已作废：模拟盘自测")
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"
    assert bench.sent == []


def test_it_is_not_run_from_the_group():
    bench = Bench(Demo())
    assert "私聊" in bench.say("模拟盘自测", private=False).text
    assert bench.sent == []


# --------------------------------------------------------------------- a full run


def test_all_eight_steps_are_sent_checked_and_cleared_away():
    bench = Bench(Demo())
    reply = bench.run("模拟盘自测")
    assert reply.title == "模拟盘自测 · 通过" and reply.template == "green"
    assert f"{STEPS} 步都通过" in reply.text and reply.text.count("✅") == STEPS and "❌" not in reply.text
    assert calls(bench) == [
        "place_order", "place_stop", "amend_stop", "place_order", "cancel_stops", "place_stop", "place_order",
        "cancel_order", "close_position",
    ]  # fmt: skip
    opened, reduced, resting = bench.exchange.named("place_order")
    assert (opened["side"], opened["ord_type"], opened["sz"]) == ("buy", "market", "2")
    assert (reduced["side"], reduced["sz"], reduced["reduce_only"]) == ("sell", "1", True)
    assert (resting["side"], resting["ord_type"], resting["sz"], resting["px"]) == ("buy", "limit", "1", "3763.6")
    first, second = bench.exchange.named("place_stop")
    assert (first["sl_trigger_px"], first["tp_trigger_px"]) == ("3802.4", "3957.6")
    assert second["sl_trigger_px"] == "3821.8" and "tp_trigger_px" not in second
    assert bench.exchange.named("amend_stop")[0]["sl_trigger_px"] == "3821.8"
    assert empty(bench.exchange)
    assert "止损 3,821.8，止盈 3,957.6" in reply.text and "止损 3,821.8，止盈 无" in reply.text


def test_a_run_is_in_the_order_record_and_counts_as_two_openings():
    bench = Bench(Demo())
    bench.run("模拟盘自测")
    rows = bench.rows()
    assert len(rows) == STEPS and all(row["summary"].startswith("自测：") for row in rows)
    assert {row["mode"] for row in rows} == {"demo"} and {row["result"] for row in rows} == {"ok"}
    assert bench.journal.count_opens(0, 2_000_000_000, "demo") == 2
    # The day's events get one line for the whole run, so the evening brief is not eight lines longer.
    events = bench.journal.events_between(0, 2_000_000_000)
    assert [event["summary"] for event in events] == [f"[模拟盘] 下单自测：{STEPS} 步都通过"]


def test_a_pass_is_remembered_and_shown_in_the_settings():
    bench = Bench(Demo())
    assert "还没有跑过" in "\n".join(bench.desk.status_lines())
    bench.run("模拟盘自测")
    last = last_run(bench)
    assert last["ok"] is True and last["version"] == __version__ and last["inst_id"] == XAU
    line = next(line for line in bench.desk.status_lines() if line.startswith("**模拟盘自测**"))
    assert f"在 v{__version__} 上通过" in line and XAU in line and "还没有跑过" not in line


def test_a_pass_on_an_older_version_is_said_to_be_old():
    bench = Bench(Demo())
    value = {"ts": bench.clock(), "version": "0.0.1", "inst_id": XAU, "ok": True, "at": ""}
    bench.journal.set_setting(SETTING, json.dumps(value))
    line = next(line for line in bench.desk.status_lines() if line.startswith("**模拟盘自测**"))
    assert "v0.0.1 上通过" in line and f"现在是 v{__version__}，这个版本还没有跑过" in line


def test_a_record_that_cannot_be_read_counts_as_never_run():
    bench = Bench(Demo())
    bench.journal.set_setting(SETTING, "{broken")
    assert "还没有跑过" in "\n".join(bench.desk.status_lines())


def test_another_instrument_can_be_named():
    bench = Bench(Demo())
    reply = bench.run("模拟盘自测 BTC")
    assert reply.title == "模拟盘自测 · 通过"
    assert {order["inst_id"] for order in bench.exchange.named("place_order")} == {BTC}
    assert bench.exchange.named("place_order")[0]["sz"] == "0.02"
    assert last_run(bench)["inst_id"] == BTC


def test_with_stops_required_the_orders_carry_one():
    bench = Bench(Demo(), trade_require_stop=True)
    reply = bench.run("模拟盘自测")
    assert reply.title == "模拟盘自测 · 通过", reply.text
    opened, _, resting = bench.exchange.named("place_order")
    assert opened["sl_trigger_px"] == "3802.4" and resting["sl_trigger_px"] == "3688.3"
    # The stop that came with the order is replaced by one with a take-profit: cancel, then place.
    assert calls(bench)[:3] == ["place_order", "cancel_stops", "place_stop"]
    assert empty(bench.exchange)


def test_a_broken_connection_on_the_first_order_is_looked_up_not_repeated():
    exchange = Demo()
    exchange.lost.add("place_order")
    bench = Bench(exchange)
    reply = bench.run("模拟盘自测")
    assert reply.title == "模拟盘自测 · 通过"
    assert len(exchange.named("place_order")) == 3


def test_a_slow_read_is_tried_again():
    exchange = Demo()
    exchange.reads_fail = 2
    bench = Bench(exchange)
    assert bench.run("模拟盘自测").title == "模拟盘自测 · 通过"


# ---------------------------------------------------------------------- refusals


def test_never_on_the_real_account():
    bench = Bench(Demo(), trading="live")
    reply = bench.say("模拟盘自测")
    assert "只在模拟盘上跑" in reply.text and "确认" not in reply.text
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"
    assert bench.sent == []


def test_with_trading_off_it_says_so():
    bench = Bench(Demo(), trading="off")
    assert "下单功能现在是关着的" in bench.say("模拟盘自测").text


@pytest.mark.parametrize(
    "cfg, words",
    [
        (dict(trade_actions=("stops", "close")), "没有开放：撤单、限价挂单、市价开仓和加仓"),
        (dict(trade_max_opens_per_day=1), "自测要开仓 2 次"),
        (dict(trade_max_order_usd=5.0), "超过了单笔上限"),
        (dict(trade_instruments=(BTC,)), "不在允许下单的品种里"),
    ],
)
def test_settings_that_stop_it_before_anything_is_sent(cfg, words):
    bench = Bench(Demo(), **cfg)
    reply = bench.say("模拟盘自测 XAU")
    assert words in reply.text, reply.text
    assert bench.sent == []


def test_not_while_trading_is_paused():
    bench = Bench(Demo())
    bench.journal.set_setting(PAUSED_SETTING, "1")
    assert "交易已暂停" in bench.say("模拟盘自测").text and bench.sent == []


def test_not_on_a_spot_only_account():
    exchange = Demo()
    exchange.config = replace(exchange.config, acct_lv="1")
    bench = Bench(exchange)
    assert "现货模式" in bench.say("模拟盘自测").text and bench.sent == []


@pytest.mark.parametrize("what", ["position", "order", "stop"])
def test_it_leaves_alone_an_instrument_that_already_holds_something(what):
    exchange = Demo()
    if what == "position":
        exchange.positions = [small()]
    elif what == "order":
        exchange.pending = [OrderInfo("5001", XAU, "buy", px=3700.0, sz=1.0, state="live")]
    else:
        exchange.orphans = [{"instId": XAU, "algoId": "8001"}]
    bench = Bench(exchange)
    reply = bench.say("模拟盘自测")
    assert "已经" not in reply.title and "现在有仓位、挂单或止盈止损单" in reply.text
    assert bench.sent == []
    assert bench.run("模拟盘自测 BTC").title == "模拟盘自测 · 通过"  # the other instrument is free


def test_what_changed_after_the_preview_is_checked_again_on_confirmation():
    bench = Bench(Demo())
    assert "确认 1111" in bench.say("模拟盘自测").text
    bench.exchange.positions = [small()]
    reply = bench.say("确认 1111")
    assert reply.text.startswith("没有执行。") and "现在有仓位" in reply.text
    assert bench.sent == []
    assert bench.journal.get_setting(SETTING) == ""  # a run that never began is not a failed run


# ---------------------------------------------------------------------- failures


def test_an_order_okx_refuses_stops_the_run_and_the_position_is_closed():
    exchange = Demo()
    exchange.failures["place_stop"] = [OkxError("51000", "Parameter closeFraction error")]
    bench = Bench(exchange)
    reply = bench.run("模拟盘自测")
    assert reply.title == "模拟盘自测 · 没有通过" and reply.template == "red"
    assert "停在第 2 步（挂止损和止盈）" in reply.text and "Parameter closeFraction error（代码 51000）" in reply.text
    assert reply.text.count("✅") == 1 and reply.text.count("❌") == 1
    assert calls(bench) == ["place_order", "place_stop", "close_position"]  # nothing after the failed step but the clean-up
    assert "已经收拾干净" in reply.text and empty(exchange)
    last = last_run(bench)
    assert last["ok"] is False and last["at"] == "第 2 步（挂止损和止盈）"
    events = bench.journal.events_between(0, 2_000_000_000)
    assert [event["summary"] for event in events] == ["[模拟盘] 下单自测：没有通过，停在第 2 步（挂止损和止盈）"]
    assert "后面的步骤没有跑" in reply.text
    line = next(line for line in bench.desk.status_lines() if line.startswith("**模拟盘自测**"))
    assert "没有通过，停在第 2 步（挂止损和止盈）" in line


def test_the_first_order_refused_leaves_nothing_to_clear():
    exchange = Demo()
    exchange.failures["place_order"] = [OkxError("51001", "Instrument ID does not exist")]
    bench = Bench(exchange)
    reply = bench.run("模拟盘自测")
    assert "停在第 1 步（市价开多）" in reply.text and "Instrument ID does not exist" in reply.text
    assert calls(bench) == ["place_order"] and "已经收拾干净" in reply.text


def test_a_request_okx_accepts_without_effect_is_caught_by_reading_back():
    exchange = Demo()
    exchange.deaf_to_amend = True
    waits: list[float] = []
    bench = Bench(exchange)
    bench.desk._sleep = waits.append
    reply = bench.run("模拟盘自测")
    assert "停在第 3 步（改止损）" in reply.text
    assert "读回来的结果不对" in reply.text and "止损 3,802.4，止盈 3,957.6" in reply.text
    assert waits.count(1.0) >= CHECK_TRIES - 1  # it waited for OKX before giving up
    assert calls(bench)[-1] == "close_position" and empty(exchange)


def test_a_failure_with_an_order_resting_cancels_it_and_closes():
    exchange = Demo()
    exchange.failures["cancel_order"] = [OkxError("50001", "Service temporarily unavailable")]
    bench = Bench(exchange)
    reply = bench.run("模拟盘自测")
    assert "停在第 7 步（撤单）" in reply.text
    assert calls(bench)[-3:] == ["cancel_order", "cancel_order", "close_position"]
    assert "已经收拾干净" in reply.text and empty(exchange)


def test_stop_orders_left_behind_by_the_close_are_found_and_cancelled():
    exchange = Demo()
    exchange.leave_stops = True
    bench = Bench(exchange)
    reply = bench.run("模拟盘自测")
    assert "停在第 8 步（平仓）" in reply.text and "应该什么都不剩，读到止盈止损单 1 张" in reply.text
    assert calls(bench)[-2:] == ["close_position", "cancel_stops"]
    assert "已经收拾干净" in reply.text and empty(exchange)


def test_what_could_not_be_cleared_is_said_plainly():
    exchange = Demo()
    refusal = OkxError("50001", "Service temporarily unavailable")
    exchange.failures["close_position"] = [refusal, refusal]
    bench = Bench(exchange)
    reply = bench.run("模拟盘自测")
    assert "停在第 8 步（平仓）" in reply.text
    assert "还留着仓位 1 个、止盈止损单 1 张" in reply.text and "手动处理" in reply.text
    assert "已经收拾干净" not in reply.text
    assert len(exchange.positions) == 1


def test_a_failed_run_does_not_hide_an_earlier_step_report():
    exchange = Demo()
    exchange.failures["amend_stop"] = [OkxError("51503", "Order modification failed")]
    bench = Bench(exchange)
    reply = bench.run("模拟盘自测")
    lines = reply.text.splitlines()
    assert lines[0] == f"**{XAU}：停在第 3 步（改止损）**"
    assert lines[1].startswith("- ✅ 第 1 步 市价开多") and lines[2].startswith("- ✅ 第 2 步 挂止损和止盈")
    assert lines[3].startswith("- ❌ 第 3 步（改止损）")
