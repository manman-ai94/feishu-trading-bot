"""The account on MT5 (EC Markets): read as OKX's was, traded through the terminal, refused on the wrong
account; and the trade desk on top of it, in lots."""
from __future__ import annotations

from decimal import Decimal

import pytest

from bot.instruments import InstrumentResolver
from bot.journal import Journal
from bot.mt5_client import Mt5Client, Mt5Link, Mt5TradeClient
from bot.okx_client import OkxError, OkxNetworkError
from bot.trading import TradeDesk
from tests.fake_mt5 import OFFSET, FakeMarket, FakeMt5
from tests.helpers import Clock, make_cfg

GOLD = "XAU-USD-SWAP"
START = 1_800_000_000.0


def client(fake: FakeMt5 | None = None, trade: str = "", clock: Clock | None = None):
    clock = clock or Clock(START)
    fake = fake or FakeMt5(clock)
    link = Mt5Link(module=fake, clock=clock)
    if trade:
        return Mt5TradeClient(FakeMarket(), link, {"XAU": "XAUUSD"}, real=trade == "live", clock=clock), fake
    return Mt5Client(FakeMarket(), link, {"XAU": "XAUUSD"}, clock=clock), fake


# ------------------------------------------------------------------ reading


def test_the_tickets_of_one_direction_are_one_position_with_one_stop():
    reader, fake = client()
    fake.open_ticket(0, 0.10, 3790.0, sl=3770.0, tp=3850.0, swap=-1.5)
    fake.open_ticket(0, 0.30, 3794.0, sl=3775.0, tp=3850.0)
    state = reader.account_state()
    [position] = state.positions
    assert (position.inst_id, position.direction, position.pos_side) == (GOLD, "long", "long")
    assert position.qty == pytest.approx(0.4) and position.avg_px == pytest.approx(3793.0)
    assert position.coins == pytest.approx(40.0) and position.coin_ccy == "XAU" and position.pnl_ccy == "USD"
    assert position.upl == pytest.approx(100 + 180 - 1.5)  # profits at the bid 3800, and the swap
    assert position.stop_loss == 3770.0 and position.take_profit == 3850.0  # the stop furthest away
    assert position.stops[0].algo_id == "p:XAUUSD:long"
    assert state.snapshot.total_eq == pytest.approx(10_000 + 278.5)
    assert state.snapshot.mgn_ratio == pytest.approx(state_level(fake) / 50.0)
    # Stop out at 50 % of the margin: the equity above it, spread over 40 oz.
    margin = 0.1 * 100 * 3790 / 100 + 0.3 * 100 * 3794 / 100
    assert position.liq_px == pytest.approx(3800 - (10_278.5 - margin * 0.5) / 40)


def state_level(fake: FakeMt5) -> float:
    return fake.account_info().margin_level


def test_a_ticket_without_a_stop_leaves_the_position_without_one_and_take_profits_that_differ_are_not_shown():
    reader, fake = client()
    fake.open_ticket(1, 0.1, 3810.0, sl=3830.0, tp=3700.0)
    fake.open_ticket(1, 0.1, 3812.0, tp=3720.0)
    [position] = reader.positions()
    assert position.direction == "short" and position.stops == ()
    fake.positions[1].sl = 3835.0
    [position] = reader.positions()
    assert position.stop_loss == 3835.0 and position.take_profit is None


def test_not_logged_in_says_so():
    reader, fake = client()
    fake.logged_in = False
    with pytest.raises(OkxError, match="还没登录"):
        reader.account_state()


def test_gold_is_read_from_mt5_and_everything_else_from_okx():
    reader, fake = client()
    ticker = reader.ticker(GOLD)
    assert (ticker.last, ticker.bid, ticker.ask) == (3800.0, 3800.0, 3800.3)
    assert reader.ticker("XAU-USDT-SWAP").last == 3800.0  # the old OKX id means the same gold
    assert reader.ticker("BTC-USDT-SWAP").last == 95000.0
    assert reader.funding_rate(GOLD) is None and reader.funding_rate("BTC-USDT-SWAP") == 0.0001
    reader.top_trader_ratio(GOLD)
    assert reader.market.asked[-1] == ("ratio", "XAU-USDT-SWAP")  # OKX's big traders in gold
    ids = [row["instId"] for row in reader.instruments("SWAP")]
    assert ids == ["BTC-USDT-SWAP", GOLD]
    assert InstrumentResolver(reader).resolve("黄金") == GOLD


def test_one_lot_is_a_contract_of_a_hundred_ounces():
    reader, _ = client()
    spec = reader.instrument_spec(GOLD)
    assert (spec.coins_per_contract, spec.lot_sz, spec.min_sz, spec.tick_sz, spec.unit) == (100, Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), "手")
    assert (spec.settle_ccy, spec.ct_type, spec.state) == ("USD", "linear", "live")


def test_a_broker_suffix_on_the_symbol_is_found():
    reader, fake = client()
    fake.symbols["XAUUSD.r"] = fake.symbols.pop("XAUUSD")
    assert reader.symbol(GOLD) == "XAUUSD.r"
    assert reader.inst_id_of("XAUUSD.r") == GOLD


def test_server_times_are_turned_into_utc_once_the_ticks_show_the_offset():
    clock = Clock(START)
    reader, fake = client(clock=clock)
    fake.rates = [{"time": int(START + OFFSET) - 3600, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "tick_volume": 10}]
    reader.ticker(GOLD)
    clock.advance(1)
    reader.ticker(GOLD)  # a new tick: the market moves, so its time is now
    assert reader.link.offset() == OFFSET
    [candle] = reader.candles(GOLD, "1H", 1)
    assert candle.ts == int((START - 3600) * 1000) and candle.confirmed is False


def test_until_the_market_moves_the_usual_offset_is_assumed():
    _, fake = client()
    link = Mt5Link(module=fake, clock=Clock(1_784_000_000.0))  # July: New York keeps summer time
    assert link.offset() == 3 * 3600
    assert Mt5Link(module=fake, clock=Clock(1_800_000_000.0)).offset() == 2 * 3600  # January


def test_closes_come_from_the_deals_and_tickets_closed_together_are_one_close():
    clock = Clock(START)
    reader, fake = client(clock=clock)
    first = fake.open_ticket(0, 0.1, 3790.0, sl=3770.0)
    second = fake.open_ticket(0, 0.2, 3796.0, sl=3770.0)
    clock.advance(600)
    fake.close_ticket(first, 3770.0, reason=4)
    fake.close_ticket(second, 3770.0, reason=4, swap=-2.0)
    [row] = reader.position_history()
    assert (row["instId"], row["direction"], row["type"]) == (GOLD, "long", "2")
    assert float(row["pnl"]) == pytest.approx(-200 - 520) and float(row["fundingFee"]) == -2.0
    assert float(row["realizedPnl"]) == pytest.approx(-722.0)
    assert float(row["openAvgPx"]) == pytest.approx(3794.0) and float(row["closeTotalPos"]) == pytest.approx(0.3)
    [trigger] = reader.algo_history("conditional")
    assert trigger["actualSide"] == "sl" and trigger["instId"] == GOLD
    assert reader.algo_history("oco") == []


def test_a_partial_close_is_type_one_and_the_rest_later_type_two():
    clock = Clock(START)
    reader, fake = client(clock=clock)
    ticket = fake.open_ticket(1, 0.2, 3800.0)
    clock.advance(60)
    fake.close_ticket(ticket, 3790.0, volume=0.1)
    clock.advance(60)
    fake.close_ticket(ticket, 3780.0, reason=5)
    rows = reader.position_history()
    assert [row["type"] for row in rows] == ["2", "1"]  # newest first
    assert float(rows[0]["pnl"]) == pytest.approx(200.0) and rows[0]["direction"] == "short"
    assert reader.algo_history("conditional")[0]["actualSide"] == "tp"


def test_stop_orders_are_trigger_orders_and_limit_orders_are_orders():
    trader, _ = client(trade="demo", fake=FakeMt5(Clock(START), real=False))
    trader.place_trigger(inst_id=GOLD, td_mode="cross", side="buy", sz="0.05", trigger_px="3830", cl_ord_id="ftb1", sl_trigger_px="3810")
    trader.place_order(inst_id=GOLD, td_mode="cross", side="buy", ord_type="limit", sz="0.1", cl_ord_id="ftb2", px="3780")
    [trigger] = trader.pending_algos("trigger")
    assert (trigger["side"], trigger["triggerPx"], trigger["sz"], trigger["algoClOrdId"], trigger["tag"]) == ("buy", "3830", "0.05", "ftb1", "ftbot")
    assert trigger["attachAlgoOrds"] == [{"slTriggerPx": "3810"}]
    [order] = trader.pending_orders(GOLD)
    assert (order.side, order.px, order.sz, order.ord_type, order.state) == ("buy", 3780.0, 0.1, "limit", "live")
    assert trader.pending_orders_raw()[0]["ordType"] == "limit"


# ------------------------------------------------------------------ trading


def test_no_order_goes_to_an_account_of_the_wrong_kind_or_while_algo_trading_is_off():
    trader, fake = client(trade="live", fake=FakeMt5(Clock(START), real=False))
    with pytest.raises(OkxError, match="模拟账户"):
        trader.place_order(inst_id=GOLD, td_mode="cross", side="buy", ord_type="market", sz="0.1", cl_ord_id="ftb1")
    trader, fake = client(trade="demo", fake=FakeMt5(Clock(START), real=True))
    with pytest.raises(OkxError, match="实盘账户"):
        trader.place_order(inst_id=GOLD, td_mode="cross", side="buy", ord_type="market", sz="0.1", cl_ord_id="ftb1")
    trader, fake = client(trade="live")
    fake.algo_trading = False
    with pytest.raises(OkxError, match="算法交易"):
        trader.place_order(inst_id=GOLD, td_mode="cross", side="buy", ord_type="market", sz="0.1", cl_ord_id="ftb1")
    assert fake.requests == []
    assert trader.account_config().can_trade is False


def test_a_market_order_opens_a_ticket_and_is_read_back_by_its_ticket():
    trader, fake = client(trade="live")
    order_id = trader.place_order(
        inst_id=GOLD, td_mode="cross", side="buy", ord_type="market", sz="0.123", cl_ord_id="ftb1", sl_trigger_px="3780.004"
    )
    request = fake.requests[-1]
    assert (request["volume"], request["sl"], request["price"], request["type_filling"], request["comment"]) == (0.12, 3780.0, 3800.3, 0, "ftb1")
    order = trader.order(GOLD, ord_id=order_id)
    assert (order.state, order.filled_sz, order.avg_px, order.side) == ("filled", 0.12, 3800.3, "buy")
    assert trader.order(GOLD, cl_ord_id="ftb1").ord_id == order_id
    with pytest.raises(OkxError) as missing:
        trader.order(GOLD, cl_ord_id="ftb9")
    assert missing.value.code == "51603"


def test_the_stop_of_a_position_moves_on_every_ticket_and_keeps_the_take_profit():
    trader, fake = client(trade="live")
    fake.open_ticket(0, 0.1, 3790.0, sl=3770.0, tp=3850.0)
    fake.open_ticket(0, 0.2, 3796.0, sl=3775.0, tp=3850.0)
    trader.amend_stop(GOLD, "p:XAUUSD:long", sl_trigger_px="3785")
    assert [(row.sl, row.tp) for row in fake.positions] == [(3785.0, 3850.0), (3785.0, 3850.0)]
    trader.amend_stop(GOLD, "p:XAUUSD:long", sl_trigger_px="3785")  # nothing to change is not a failure
    assert trader.place_stop(inst_id=GOLD, td_mode="cross", side="sell", cl_ord_id="x", tp_trigger_px="3860") == "p:XAUUSD:long"
    assert [(row.sl, row.tp) for row in fake.positions] == [(3785.0, 3860.0), (3785.0, 3860.0)]
    trader.cancel_stops([(GOLD, "p:XAUUSD:long")])
    assert [(row.sl, row.tp) for row in fake.positions] == [(0.0, 0.0), (0.0, 0.0)]


def test_closing_a_position_closes_every_ticket_and_reads_back_as_one_order():
    trader, fake = client(trade="live")
    fake.open_ticket(0, 0.1, 3790.0)
    fake.open_ticket(0, 0.2, 3796.0)
    fake.open_ticket(1, 0.1, 3801.0)
    trader.close_position(GOLD, "cross", "long", cl_ord_id="ftb7")
    assert [row.type for row in fake.positions] == [1]
    order = trader.order(GOLD, cl_ord_id="ftb7")
    assert (order.state, order.filled_sz, order.side) == ("filled", pytest.approx(0.3), "sell")
    assert order.pnl == pytest.approx(100.0 + 80.0)  # both longs closed at the bid 3800


def test_a_reduce_only_order_closes_the_oldest_tickets_first():
    trader, fake = client(trade="live")
    old = fake.open_ticket(0, 0.1, 3790.0, ago=60)
    fake.open_ticket(0, 0.2, 3796.0)
    trader.place_order(inst_id=GOLD, td_mode="cross", side="sell", ord_type="market", sz="0.15", cl_ord_id="ftb3", reduce_only=True)
    assert old not in fake.positions and fake.positions[0].volume == pytest.approx(0.15)
    with pytest.raises(OkxError, match="只有"):
        trader.place_order(inst_id=GOLD, td_mode="cross", side="sell", ord_type="market", sz="1", cl_ord_id="ftb4", reduce_only=True)


def test_a_refusal_is_told_in_words_and_no_answer_is_an_unknown_outcome():
    trader, fake = client(trade="live")
    fake.fail = [10016]
    with pytest.raises(OkxError, match="止损或止盈不对") as refused:
        trader.place_order(inst_id=GOLD, td_mode="cross", side="buy", ord_type="market", sz="0.1", cl_ord_id="ftb1", sl_trigger_px="3799")
    assert refused.value.code == "10016"
    fake.fail = [10012]
    with pytest.raises(OkxNetworkError):
        trader.place_order(inst_id=GOLD, td_mode="cross", side="buy", ord_type="market", sz="0.1", cl_ord_id="ftb1")
    fake.silent = True
    with pytest.raises(OkxNetworkError):
        trader.place_order(inst_id=GOLD, td_mode="cross", side="buy", ord_type="market", sz="0.1", cl_ord_id="ftb1")


def test_a_trigger_order_below_the_price_to_buy_is_a_limit_order_and_cancelled_as_a_stop():
    trader, fake = client(trade="live")
    algo_id = trader.place_trigger(inst_id=GOLD, td_mode="cross", side="buy", sz="0.1", trigger_px="3700", cl_ord_id="ftb5")
    assert fake.orders[0].type == 2  # buy limit: OKX's trigger below the price buys when it gets there
    trader.cancel_stops([(GOLD, algo_id)])
    assert fake.orders == [] and fake.history_orders[-1].state == 2


# ------------------------------------------------------------------ the desk on MT5


class Mt5Bench:
    def __init__(self, **cfg):
        self.clock = Clock(START)
        self.fake = FakeMt5(self.clock)
        link = Mt5Link(module=self.fake, clock=self.clock)
        market = FakeMarket()
        reader = Mt5Client(market, link, {"XAU": "XAUUSD"}, clock=self.clock)
        trader = Mt5TradeClient(market, link, {"XAU": "XAUUSD"}, real=True, clock=self.clock)
        values = dict(
            broker="mt5", trading="live", watch_instruments=(GOLD,), trade_instruments=(GOLD,), plan_required=False,
            trade_max_order_usd=1_000_000.0, trade_max_position_usd=2_000_000.0,
        )  # fmt: skip
        values.update(cfg)
        self.cfg = make_cfg(**values)
        codes = iter(["1111", "2222", "3333"])
        ids = iter(f"ftb{n:04d}" for n in range(1, 99))
        self.desk = TradeDesk(
            self.cfg, Journal(":memory:"), reader=reader, trader=trader, resolver=InstrumentResolver(reader, self.clock),
            clock=self.clock, sleep=lambda seconds: None, new_code=lambda: next(codes), new_id=lambda: next(ids),
        )  # fmt: skip

    def say(self, text: str):
        return self.desk.handle(text)


def test_an_order_in_lots_goes_to_mt5_after_the_code():
    bench = Mt5Bench()
    preview = bench.say("做多 XAU 0.1手 止损 3780")
    assert "0.1 手（合 10 XAU）" in preview.text and "确认 1111" in preview.text
    done = bench.say("确认 1111")
    assert "已成交 0.1 手（合 10 XAU），均价 3,800.30" in done.text
    [ticket] = bench.fake.positions
    assert (ticket.volume, ticket.sl, ticket.comment) == (0.1, 3780.0, "ftb0001")


def test_contracts_are_not_a_unit_on_mt5():
    bench = Mt5Bench()
    reply = bench.say("做多 XAU 100张")
    assert "“手”" in reply.text and bench.fake.requests == []


def test_a_size_from_the_stop_counts_the_spread_and_the_slippage():
    bench = Mt5Bench(risk_per_trade_pct=1.0, trade_slippage_pct=0.02)
    preview = bench.say("做多 XAU 止损 3780")
    # 1 % of 10 000 is 100; each ounce loses 20 to the stop, 0.3 to the spread and 0.76 to slippage: 4.74 oz.
    assert "点差 0.3" in preview.text and "滑点 0.76" in preview.text
    assert "0.04 手（合 4 XAU）" in preview.text
    without = Mt5Bench(risk_per_trade_pct=1.0)
    without.fake.set_price(3800.0, 3800.0)  # no spread, and no slippage allowed for: 20 an ounce, 5 oz
    assert "0.05 手（合 5 XAU）" in without.say("做多 XAU 止损 3780").text


def test_the_stop_of_a_position_is_moved_on_mt5():
    bench = Mt5Bench()
    bench.fake.open_ticket(0, 0.1, 3790.0, sl=3770.0)
    bench.fake.open_ticket(0, 0.1, 3792.0, sl=3770.0)
    preview = bench.say("止损 XAU 3785")
    assert "确认 1111" in preview.text, preview.text
    bench.say("确认 1111")
    assert [row.sl for row in bench.fake.positions] == [3785.0, 3785.0]


def test_the_desk_says_mt5_where_it_would_say_okx():
    bench = Mt5Bench()
    bench.fake.fail = [10019]
    bench.say("做多 XAU 0.1手 止损 3780")
    done = bench.say("确认 1111")
    assert "MT5 拒绝了：保证金不够" in done.text and "OKX" not in done.text
    assert all("OKX" not in line for line in bench.desk.status_lines())


def test_a_lot_on_okx_is_still_one_xau_and_on_mt5_a_hundred_ounces():
    from bot.trading import CommandParser

    class Names:
        def resolve(self, name):
            return GOLD if name.lower() in ("xau", "黄金") else None

    on_mt5 = CommandParser(Names(), lambda: (GOLD,), lots=True).parse("做多 XAU 0.2手")
    on_okx = CommandParser(Names(), lambda: (GOLD,)).parse("做多 XAU 0.2手")
    assert (on_mt5.size, on_mt5.unit) == (Decimal("0.2"), "contracts")
    assert (on_okx.size, on_okx.unit) == (Decimal("0.2"), "coins")


def test_the_ai_is_told_the_account_is_on_mt5_and_writes_orders_in_lots():
    from bot.analyst import ANALYST_SYSTEM, COMMAND_SYSTEM, Analyst

    on_mt5 = Analyst(make_cfg(broker="mt5"), client=object())
    assert on_mt5._for_broker(ANALYST_SYSTEM).endswith(MT5_WORDS)
    command = on_mt5._for_broker(COMMAND_SYSTEM)
    assert "做多 XAU 0.1手" in command and "100张" not in command and "黄金 1 手 = 100 盎司" in command
    assert on_mt5._market_label.startswith("实时行情数据（黄金来自 MT5")
    on_okx = Analyst(make_cfg(), client=object())
    assert on_okx._for_broker(ANALYST_SYSTEM) == ANALYST_SYSTEM and on_okx._for_broker(COMMAND_SYSTEM) == COMMAND_SYSTEM


MT5_WORDS = "上面说到 OKX 账户、在 OKX 里操作、下到 OKX 的地方，现在都指 MT5。"


def test_the_app_builds_the_mt5_clients_and_none_that_trade_while_trading_is_off():
    from bot.app import build_demo_trader, build_okx, build_trader

    cfg = make_cfg(broker="mt5", trading="live")
    reader = build_okx(cfg)
    assert isinstance(reader, Mt5Client) and reader.carries(GOLD) and not reader.carries("BTC-USDT-SWAP")
    trader = build_trader(cfg, reader)
    assert isinstance(trader, Mt5TradeClient) and trader.link is reader.link
    assert build_trader(make_cfg(broker="mt5", trading="off"), reader) is None
    assert build_demo_trader(make_cfg(broker="mt5", trading="live", okx_demo_api_key="k", okx_demo_secret_key="s", okx_demo_passphrase="p")) is None


def test_nothing_to_list_is_an_empty_list_and_a_lost_terminal_is_found_again():
    reader, fake = client()
    fake.history_deals_get = lambda start, end, **kwargs: None  # the package's way of saying "none"
    assert reader.position_history() == []
    calls = []
    fake.positions_get = lambda **kwargs: None
    fake.last_error = lambda: (-10004, "No IPC connection")
    with pytest.raises(OkxError, match="No IPC connection"):
        reader.account_state()
    fake.initialize = lambda **kwargs: calls.append(kwargs) or True
    fake.positions_get = lambda **kwargs: ()
    reader.account_state()
    assert len(calls) == 1 and calls[0]["timeout"] == 20_000


def test_a_close_and_a_new_position_straight_after_are_two_things():
    clock = Clock(START)
    reader, fake = client(clock=clock)
    ticket = fake.open_ticket(0, 0.1, 3790.0)
    clock.advance(60)
    fake.close_ticket(ticket, 3800.0)
    fake.open_ticket(0, 0.1, 3800.5)
    [row] = reader.position_history()
    assert row["type"] == "2"
