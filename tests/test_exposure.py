"""The account's total risk: every stop hit at once, and what cannot be measured named rather than guessed."""
from __future__ import annotations

from decimal import Decimal

from bot.exposure import card_lines, measure
from bot.models import OrderInfo, StopOrder
from bot.reports import status_markdown

from .helpers import FakeAnalyst, FakeFeishu, make_cfg, make_position, make_state
from .test_book import Gold

XAU = "XAU-USDT-SWAP"
PER = Decimal("0.001")  # XAU per contract


def per(inst_id: str) -> Decimal | None:
    return PER if inst_id == XAU else None


def gold(**overrides):
    values = dict(qty=10000.0, mark_px=3880.0, liq_px=3700.0, coin_ccy="XAU", stops=(StopOrder("a1", 3850.0, None),))
    values.update(overrides)
    return make_position(**values)


def loss_of(state, orders=(), triggers=()) -> Decimal:
    return measure(state, list(orders), list(triggers), per).loss


def test_a_position_counts_from_its_mark_to_its_worst_stop():
    assert loss_of(make_state([gold()])) == Decimal(300)  # 30 x 10 XAU
    several = gold(stops=(StopOrder("a1", 3850.0, None), StopOrder("a2", 3840.0, None)))
    assert loss_of(make_state([several])) == Decimal(400)
    short = gold(direction="short", stops=(StopOrder("a1", 3900.0, None),), liq_px=4100.0)
    assert loss_of(make_state([short])) == Decimal(200)


def test_a_part_no_stop_covers_counts_to_the_liquidation_price():
    part = gold(stops=(StopOrder("a1", 3850.0, None, whole=False, size=4000.0),))
    exposure = measure(make_state([part]), [], [], per)
    assert exposure.loss == Decimal(30 * 4 + 180 * 6)
    assert "另有 6 没有止损（按到强平价 3,700 算）" in exposure.pieces[0].label


def test_a_bare_position_counts_to_liquidation_and_without_one_it_is_named():
    assert loss_of(make_state([gold(stops=())])) == Decimal(1800)
    exposure = measure(make_state([gold(stops=(), liq_px=None)]), [], [], per)
    assert exposure.loss == 0 and "没有止损，也读不到强平价" in exposure.unknown[0].label


def test_orders_waiting_to_open_count_from_their_price_to_their_stop_and_closing_ones_do_not():
    orders = [
        OrderInfo("1", XAU, "buy", px=3800.0, sz=2000.0, state="live", sl_trigger_px=3780.0),  # 2 XAU, 20: 40
        OrderInfo("2", XAU, "sell", px=3950.0, sz=2000.0, state="live", reduce_only=True),  # a take-profit: nothing
        OrderInfo("3", XAU, "sell", pos_side="long", px=3950.0, sz=2000.0, state="live"),  # closes a hedge long: nothing
        OrderInfo("4", XAU, "buy", px=3790.0, sz=1000.0, state="live"),  # no stop: named
    ]
    exposure = measure(make_state([]), orders, [], per)
    assert exposure.loss == Decimal(40)
    assert [p.label for p in exposure.unknown] == ["XAU-USDT-SWAP 挂单买 3,790（1），没带止损"]


def test_trigger_orders_count_from_the_trigger_to_their_stop():
    triggers = [
        {"instId": XAU, "side": "buy", "posSide": "net", "sz": "1000", "triggerPx": "3900", "orderPx": "-1",
         "attachAlgoOrds": [{"slTriggerPx": "3870"}]},
        {"instId": XAU, "side": "sell", "posSide": "net", "sz": "1000", "triggerPx": "3800", "orderPx": "-1"},
        {"instId": XAU, "side": "sell", "posSide": "net", "sz": "1000", "triggerPx": "3950", "reduceOnly": "true"},
    ]  # fmt: skip
    exposure = measure(make_state([]), [], triggers, per)
    assert exposure.loss == Decimal(30)
    assert [p.label for p in exposure.unknown] == ["XAU-USDT-SWAP 突破单卖 3,800（1），没带止损"]


def test_the_card_lines_give_the_total_its_share_the_cap_and_each_piece():
    orders = [OrderInfo("4", XAU, "buy", px=3790.0, sz=1000.0, state="live")]
    exposure = measure(make_state([gold()]), orders, [], per)
    lines = card_lines(exposure, Decimal(2000), 25.0)
    assert lines[0] == "**账户总风险（所有止损都打到）**：约 300 USDT，占权益 15.0%（你定的上限 25%）"
    assert lines[1] == "- XAU-USDT-SWAP 多单 10 XAU，止损 3,850：约 300"
    assert lines[2] == "- XAU-USDT-SWAP 挂单买 3,790（1），没带止损：算不出，没算进总数"
    assert card_lines(measure(make_state([]), [], [], per), Decimal(2000), 25.0) == []


def test_the_owners_card_and_the_ai_see_the_total_risk():
    from bot.service import Reporter

    okx = Gold()
    okx.state = make_state([gold()], total_eq=2000.0)
    reporter = Reporter(make_cfg(trade_max_total_risk_pct=25.0), FakeFeishu(), okx, FakeAnalyst(), journal=None)
    lines = reporter.exposure_lines(okx.state)
    assert lines[0].startswith("**账户总风险（所有止损都打到）**：约 300 USDT，占权益 15.0%")
    card = status_markdown(okx.state, None, private=True, exposure=lines)
    assert "账户总风险（所有止损都打到）" in card
    assert "账户总风险" not in status_markdown(okx.state, None, private=False, exposure=lines)  # a group without the amounts
    assert "账户总风险（所有止损都打到）：约 300 USDT" in reporter.account_for_ai(fresh=True)


def test_a_total_that_cannot_be_read_leaves_the_card_alone():
    from bot.service import Reporter

    okx = Gold()
    okx.state = make_state([gold()])
    okx.error = RuntimeError("down")
    reporter = Reporter(make_cfg(), FakeFeishu(), okx, FakeAnalyst(), journal=None)
    assert reporter.exposure_lines(okx.state) == []
