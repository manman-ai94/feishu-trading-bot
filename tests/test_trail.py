"""Trailing stops: break-even at 1R, then following the best price; only towards profit; the owners' own
move back wins; the switch; and the desk's move without a confirmation code."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from bot.models import StopOrder
from bot.monitor import Event
from bot.okx_client import OkxError
from bot.service import Reporter
from bot.trading import Refused
from bot.trail import TrailingStops
from tests.helpers import GROUP, OWNER, Clock, FakeAnalyst, FakeFeishu, FakeOkx, make_cfg
from tests.test_discipline import Market
from tests.test_trading import XAU, Bench, gold

# Market's candles are 8 apart from high to low and 1 apart from close to close: the 1-hour ATR is 8,
# so the trail is max(16, 1R), a move must gain 4, and a stop stays at least 4 from the price.
ATR = 8.0


def long_item(**overrides) -> dict:
    return {"avg": 3800.0, "r": 20.0, "best": 3800.0, "set": None, "off": False, **overrides}


# ------------------------------------------------------------------ the arithmetic


def test_nothing_moves_before_the_position_has_been_one_r_in_profit():
    assert TrailingStops.target(long_item(best=3819.0), True, 3815.0, 3780.0, ATR) == (None, "")


def test_at_one_r_the_stop_goes_to_the_entry_plus_the_fees():
    price, why = TrailingStops.target(long_item(best=3820.0), True, 3818.0, 3780.0, ATR)
    assert price == pytest.approx(3803.8)  # 3800 and 0.1%: in and out at the taker rate
    assert why.startswith("浮盈到过 1R（20），止损移到保本：开仓均价 3800 加手续费")


def test_further_on_the_stop_follows_the_best_price_at_the_wider_of_two_atr_and_one_r():
    price, why = TrailingStops.target(long_item(best=3850.0), True, 3845.0, 3780.0, ATR)
    assert price == 3830.0 and why == "跟随最好价 3850，距离 20（2 倍 1 小时 ATR 和 1R 取大的）"  # 1R beats 16
    price, why = TrailingStops.target(long_item(best=3850.0), True, 3845.0, 3780.0, 15.0)
    assert price == 3820.0 and "距离 30" in why  # 2 ATR beats 1R


def test_a_move_too_small_or_too_close_to_the_price_waits():
    assert TrailingStops.target(long_item(best=3850.0), True, 3845.0, 3827.0, ATR) == (None, "")  # gains 3, not 4
    assert TrailingStops.target(long_item(best=3850.0), True, 3833.0, 3780.0, ATR) == (None, "")  # 3 from the price


def test_a_short_is_the_mirror_image():
    item = {"avg": 3800.0, "r": 20.0, "best": 3770.0, "set": None, "off": False}
    price, why = TrailingStops.target(item, False, 3775.0, 3820.0, ATR)
    assert price == 3790.0 and "跟随最好价 3770" in why
    price, why = TrailingStops.target({**item, "best": 3780.0}, False, 3785.0, 3820.0, ATR)
    assert price == pytest.approx(3796.2) and "减手续费" in why


def test_a_stop_already_past_the_entry_when_first_seen_just_follows_at_two_atr():
    price, why = TrailingStops.target(long_item(r=None, best=3850.0), True, 3845.0, 3810.0, ATR)
    assert price == 3834.0 and why == "跟随最好价 3850，距离 16（2 倍 1 小时 ATR）"


# ------------------------------------------------------------------ following a position


class Trail:
    """A desk on the demo account, its exchange, and the trailing stops on top."""

    def __init__(self, stop: float | None = 3824.8, mark: float = 3850.0, **cfg):
        stops = (StopOrder("a1", stop, None),) if stop is not None else ()
        self.bench = Bench(Market([gold(qty=200.0, avg_px=3844.8, mark_px=mark, stops=stops)]), **cfg)
        self.exchange = self.bench.exchange
        self.exchange.last[XAU] = mark
        self.told: list[tuple[str, str]] = []
        self.notes: list[tuple[str, str]] = []
        self.trail = TrailingStops(
            self.bench.cfg, self.bench.journal, self.exchange, self.bench.desk,
            lambda title, text: self.told.append((title, text)), lambda inst, line: self.notes.append((inst, line)),
            clock=self.bench.clock,
        )  # fmt: skip

    def at(self, mark: float) -> list[str]:
        self.exchange.last[XAU] = mark
        self.exchange.positions = [replace(p, mark_px=mark) for p in self.exchange.positions]
        self.bench.clock.advance(30)
        return self.trail.tick()

    def stop_to(self, price: float) -> None:
        self.exchange.positions = [replace(p, stops=(StopOrder("a1", price, None),)) for p in self.exchange.positions]

    @property
    def stop(self) -> float | None:
        return self.exchange.positions[0].stop_loss

    def state(self) -> dict:
        return json.loads(self.bench.journal.get_setting("trail_state", "") or "{}")


def test_a_position_goes_to_break_even_then_follows_and_never_back():
    trail = Trail()
    assert trail.at(3850.0) == []  # 5.2 in profit, 1R is 20
    assert trail.state()[f"{XAU}|cross|long"]["r"] == pytest.approx(20.0)

    assert trail.at(3866.0) == ["XAU 多单 止损 3824.8 → 3848.6"]  # 1R: entry 3844.8 and the fees
    assert trail.exchange.named("amend_stop") == [{"inst_id": XAU, "algo_id": "a1", "sl_trigger_px": "3848.6"}]
    assert trail.notes[-1][0] == XAU and trail.notes[-1][1].startswith("为什么：浮盈到过 1R（20），止损移到保本")
    assert trail.bench.rows()[-1]["source"] == "trail"

    assert trail.at(3900.0) == ["XAU 多单 止损 3848.6 → 3880"]  # the best price less 1R
    assert "跟随最好价 3900" in trail.notes[-1][1]
    assert trail.at(3890.0) == []  # the price came back: the stop stays where it is
    assert trail.stop == 3880.0
    assert trail.told == []  # the moves themselves are told by the stop-change card


def test_the_owners_moving_the_stop_back_ends_the_trailing_of_that_position():
    trail = Trail()
    trail.at(3866.0)
    trail.stop_to(3830.0)  # further from the price than where it was moved to
    assert trail.at(3870.0) == []
    assert trail.told == [("移动止损：XAU 多单不再自动移", trail.told[0][1])]
    assert "从我移到的 3848.6 被改回了 3830" in trail.told[0][1]
    assert trail.at(3950.0) == [] and trail.stop == 3830.0
    assert "止损被手动改回过，这一单不再自动移" in trail.trail.status_text()


def test_a_position_without_a_stop_is_left_alone_and_a_closed_one_forgotten():
    trail = Trail(stop=None)
    assert trail.at(3900.0) == [] and trail.state() == {} and trail.exchange.changes == []
    trail = Trail()
    trail.at(3850.0)
    trail.exchange.positions = []
    trail.at(3850.0)
    assert trail.state() == {}


def test_a_position_added_to_starts_afresh_from_its_new_average_and_stop():
    trail = Trail()
    trail.at(3866.0)
    trail.exchange.positions = [replace(p, avg_px=3860.0, stops=(StopOrder("a1", 3845.0, None),)) for p in trail.exchange.positions]
    trail.at(3866.0)
    item = trail.state()[f"{XAU}|cross|long"]
    assert item["avg"] == 3860.0 and item["r"] == pytest.approx(15.0) and item["set"] is None


def test_the_switch_stops_and_starts_it_and_says_where_each_position_is():
    trail = Trail()
    assert trail.trail.handle("停止移动止损").startswith("移动止损关了")
    assert trail.at(3900.0) == [] and trail.exchange.changes == []
    assert trail.trail.status_text().startswith("移动止损现在关着")
    assert trail.trail.handle("开启移动止损。").startswith("移动止损开了")
    trail.at(3850.0)
    assert "XAU 多单：1R = 20，浮盈到 1R 再动" in trail.trail.status_text()
    assert trail.trail.handle("移动止损怎么设置") is None  # a question is the AI's
    assert Trail(trail_stop=False).trail.enabled() is False  # off in .env until switched on


def test_nothing_is_sent_where_stops_may_not_be_changed():
    trail = Trail(trade_actions=("close", "cancel"))
    assert trail.at(3900.0) == [] and trail.exchange.changes == []
    trail = Trail(trading="off")
    assert trail.at(3900.0) == []


def test_a_move_okx_refuses_is_told_once_an_hour():
    trail = Trail()
    trail.exchange.failures["amend_stop"] = [OkxError("51000", "bad price"), OkxError("51000", "bad price")]
    assert trail.at(3866.0) == [] and trail.stop == 3824.8
    assert trail.told[0][0] == "移动止损没移成：XAU 多单" and "bad price" in trail.told[0][1]
    assert trail.notes[-1] == (XAU, "")  # the reason is taken back: no card is coming
    trail.at(3866.0)
    assert len(trail.told) == 1
    trail.bench.clock.advance(3600)
    assert trail.at(3866.0) == ["XAU 多单 止损 3824.8 → 3848.6"]


# ------------------------------------------------------------------ the desk's move


def test_the_desk_moves_a_stop_only_towards_profit_and_keeps_the_take_profit():
    bench = Bench(Market([gold(qty=200.0, stops=(StopOrder("a1", 3800.0, 3950.0),))]))
    with pytest.raises(Refused, match="已经不比"):
        bench.desk.move_stop(XAU, "long", 3790.0)
    result, lines, price = bench.desk.move_stop(XAU, "long", 3820.04)
    assert (result, price) == ("ok", 3820.0)
    assert bench.sent == [("amend_stop", {"inst_id": XAU, "algo_id": "a1", "sl_trigger_px": "3820"})]
    assert bench.exchange.positions[0].take_profit == 3950.0
    with pytest.raises(Refused, match="仓位已经没了"):
        bench.desk.move_stop(XAU, "short", 3820.0)


def test_the_desk_will_not_merge_stops_whose_take_profits_differ():
    stops = (StopOrder("a1", 3800.0, 3950.0), StopOrder("a2", 3790.0, 3990.0))
    bench = Bench(Market([gold(qty=200.0, stops=stops)]))
    with pytest.raises(Refused, match="止盈不一样"):
        bench.desk.move_stop(XAU, "long", 3820.0)
    assert bench.sent == []


# ------------------------------------------------------------------ the card that tells the move


def test_the_stop_change_card_says_it_was_the_trailing_stop_and_why():
    feishu = FakeFeishu()
    clock = Clock(1_800_000_000.0)
    reporter = Reporter(make_cfg(), feishu, FakeOkx(), FakeAnalyst(), None, clock)
    reporter.expect_stop(XAU, "为什么：跟随最好价 3900")
    event = Event(kind="stop_changed", title=f"止盈止损变动：{XAU} 多单", inst_id=XAU, private_lines=("止损 3848.6 → 3880",), to_group=True)
    reporter.notify_event(event)
    cards = [(sent.target, sent.title) for sent in feishu.sent]
    assert cards == [(OWNER, f"移动止损：{XAU} 多单"), (GROUP, f"移动止损：{XAU} 多单")]
    assert all("为什么：跟随最好价 3900" in sent.body for sent in feishu.sent)

    feishu.sent.clear()
    reporter.notify_event(event)  # the note is used once: a later change by hand is not called a trailing move
    assert feishu.sent[0].title == f"止盈止损变动：{XAU} 多单"

    reporter.expect_stop(XAU, "为什么：…")
    clock.advance(601)  # a note nobody acted on goes stale
    feishu.sent.clear()
    reporter.notify_event(event)
    assert feishu.sent[0].title == f"止盈止损变动：{XAU} 多单"
