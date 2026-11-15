from __future__ import annotations

import pytest

from bot.indicators import atr, ema, recent_range, rsi
from bot.models import AccountSnapshot, Candle, Position, StopOrder, to_float

from .helpers import make_position


def test_to_float_treats_empty_and_garbage_as_missing():
    assert to_float("") is None
    assert to_float(None) is None
    assert to_float("abc") is None
    assert to_float("1.5") == 1.5


def test_position_in_net_mode_takes_direction_from_the_sign():
    row = {"instId": "XAU-USDT-SWAP", "instType": "SWAP", "posSide": "net", "mgnMode": "cross", "pos": "-3", "avgPx": "2650.5", "liqPx": ""}
    position = Position.from_okx(row)
    assert (position.direction, position.qty) == ("short", 3.0)
    assert position.liq_px is None
    assert position.key == ("XAU-USDT-SWAP", "cross", "short")


def test_position_in_hedge_mode_takes_direction_from_pos_side():
    row = {"instId": "XAU-USDT-SWAP", "posSide": "short", "mgnMode": "isolated", "pos": "2", "closeOrderAlgo": [{"algoId": "7", "slTriggerPx": "2700", "tpTriggerPx": ""}]}
    position = Position.from_okx(row)
    assert position.direction == "short"
    assert position.stops == (StopOrder("7", 2700.0, None),)


def test_empty_position_rows_are_dropped():
    assert Position.from_okx({"instId": "XAU-USDT-SWAP", "pos": "0"}) is None
    assert Position.from_okx({"instId": "XAU-USDT-SWAP", "pos": ""}) is None


def test_liquidation_distance():
    assert make_position(mark_px=2000.0, liq_px=1900.0).liq_distance_pct == pytest.approx(5.0)
    assert make_position(liq_px=None).liq_distance_pct is None


def test_nearest_stop_is_reported():
    stops = (StopOrder("1", sl_trigger_px=2600.0, tp_trigger_px=2750.0), StopOrder("2", sl_trigger_px=2630.0, tp_trigger_px=2700.0))
    long_position = make_position(stops=stops)
    assert (long_position.stop_loss, long_position.take_profit) == (2630.0, 2700.0)
    short_position = make_position(direction="short", stops=stops)
    assert (short_position.stop_loss, short_position.take_profit) == (2600.0, 2750.0)
    assert make_position(stops=()).stop_loss is None


def test_account_snapshot_parses_okx_strings():
    snapshot = AccountSnapshot.from_okx({"totalEq": "10000.5", "mgnRatio": "", "upl": "-12.3", "uTime": "1700000000000"})
    assert snapshot.total_eq == 10000.5
    assert snapshot.mgn_ratio is None
    assert snapshot.upl == -12.3
    assert snapshot.u_time == 1700000000000


def test_ema():
    assert ema([1, 2, 3], 5) is None
    assert ema([2, 4, 6], 3) == pytest.approx(4.0)  # just the seed average
    # seed (1+3)/2 = 2, alpha 2/3: 4*2/3 + 2*1/3 = 10/3
    assert ema([1, 3, 4], 2) == pytest.approx(10 / 3)


def test_rsi():
    assert rsi([1, 2, 3], 14) is None
    assert rsi(list(range(1, 30)), 14) == 100.0
    assert rsi([5.0] * 30, 14) == 50.0
    # period 2: first averages 0.5 / 0.5, then a +1 move gives 0.75 / 0.25, RS 3, RSI 75
    assert rsi([1, 2, 1, 2], 2) == pytest.approx(75.0)


def test_atr_uses_gaps_from_the_previous_close():
    candles = [
        Candle(0, 10, 11, 9, 10),
        Candle(1, 10, 12, 10, 11),  # true range 2
        Candle(2, 14, 15, 13, 14),  # gap up: max(2, |15-11|, |13-11|) = 4
    ]
    assert atr(candles, 2) == pytest.approx(3.0)
    assert atr(candles, 3) is None


def test_recent_range():
    candles = [Candle(0, 1, 9, 1, 1), Candle(1, 1, 5, 2, 1), Candle(2, 1, 6, 3, 1)]
    assert recent_range(candles, 2) == (6, 2)
    assert recent_range([], 5) is None


def test_prices_keep_the_decimals_that_matter():
    from bot.market import fmt_price

    assert fmt_price(67890.123) == "67,890.12" and fmt_price(2650) == "2,650.00" and fmt_price(100) == "100.00"
    assert fmt_price(65.4321) == "65.43" and fmt_price(1.23456) == "1.235"
    assert fmt_price(0.123456) == "0.1235" and fmt_price(0.00001234) == "0.00001234"
    assert fmt_price(0) == "0.00" and fmt_price(-0.5) == "-0.5000"


def test_candles_text_marks_the_open_candle_and_uses_local_time():
    from zoneinfo import ZoneInfo

    from bot.market import candles_text
    from bot.models import Candle

    candles = [Candle(1_800_000_000_000, 100.0, 110.0, 95.0, 105.0), Candle(1_800_003_600_000, 105.0, 106.0, 104.0, 105.5, confirmed=False)]
    text = candles_text("BTC-USDT-SWAP", "1H", candles, ZoneInfo("America/Los_Angeles"))
    lines = text.splitlines()
    assert "共 2 根" in lines[0] and "America/Los_Angeles" in lines[0]
    assert lines[1] == "01-15 00:00 开 100.00 高 110.00 低 95.00 收 105.00" and lines[2].endswith("收 105.50 *")
    assert candles_text("BTC-USDT-SWAP", "1D", candles, ZoneInfo("UTC")).splitlines()[1].startswith("2027-01-15 开")
    assert "没有" in candles_text("BTC-USDT-SWAP", "1H", [], ZoneInfo("UTC"))


def test_quote_line_needs_no_ai():
    from bot.market import quote_line

    from .helpers import FakeOkx

    assert quote_line(FakeOkx(), "BTC-USDT-SWAP") == "**BTC-USDT-SWAP** 2,650.00（24 小时 +1.92%，区间 2,590.00 到 2,660.00，资金费率 +0.0100%）"
