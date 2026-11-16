"""The trade desk. These tests pin down what can reach OKX, and when."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from bot.journal import Journal
from bot.models import (
    AccountConfig,
    AccountSnapshot,
    AccountState,
    CurrencyBalance,
    InstrumentSpec,
    OrderInfo,
    StopOrder,
    Ticker,
)
from bot.okx_client import OkxError, OkxNetworkError
from bot.trading import PAUSED_SETTING, Command, CommandParser, TradeDesk

from .helpers import Clock, make_cfg, make_position

XAU = "XAU-USDT-SWAP"
BTC = "BTC-USDT-SWAP"
DOGE = "DOGE-USDT-SWAP"
SPECS = {
    XAU: InstrumentSpec(XAU, "SWAP", Decimal("0.001"), Decimal(1), "XAU", "USDT", "linear", Decimal(1), Decimal(1), Decimal("0.1")),
    BTC: InstrumentSpec(BTC, "SWAP", Decimal("0.01"), Decimal(1), "BTC", "USDT", "linear", Decimal("0.01"), Decimal("0.01"), Decimal("0.1")),
    DOGE: InstrumentSpec(DOGE, "SWAP", Decimal(1000), Decimal(1), "DOGE", "USDT", "linear", Decimal(1), Decimal(1), Decimal("0.00001")),
    "BTC-USD-SWAP": InstrumentSpec("BTC-USD-SWAP", "SWAP", Decimal(100), Decimal(1), "USD", "BTC", "inverse", Decimal(1), Decimal(1), Decimal("0.1")),
}  # fmt: skip


class FakeResolver:
    NAMES = {
        "xau": XAU, "黄金": XAU, "gold": XAU, "xau-usdt-swap": XAU,
        "btc": BTC, "比特币": BTC, "btc-usdt-swap": BTC,
        "eth": "ETH-USDT-SWAP", "以太坊": "ETH-USDT-SWAP", "以太": "ETH-USDT-SWAP",
        "doge": DOGE, "btc-usdt": "BTC-USDT", "btc-usd-swap": "BTC-USD-SWAP",
    }  # fmt: skip

    def resolve(self, name: str) -> str | None:
        return self.NAMES.get(name.strip().lower())


class FakeExchange:
    """Stands in for OkxTradeClient: an account, a market, and a record of every change it was asked for."""

    def __init__(self, positions=(), hedge: bool = False, permissions=("read_only", "trade")):
        self.positions = list(positions)
        self.last = {XAU: 3880.0, BTC: 65000.0, DOGE: 0.2, "BTC-USD-SWAP": 65000.0}
        self.config = AccountConfig("long_short_mode" if hedge else "net_mode", tuple(permissions), "203.0.113.7", "2")
        self.lever = 100.0
        self.available = 1000.0
        self.pending: list[OrderInfo] = []
        self.orders: dict[str, OrderInfo] = {}
        self.changes: list[tuple[str, dict]] = []
        self.failures: dict[str, list[Exception]] = {}  # raised instead of doing the thing
        self.lost: set[str] = set()  # done, and then the connection breaks
        self.lookup_failures: list[Exception] = []
        self._next = 7000

    # ------------------------------------------------------------------ reads

    def ticker(self, inst_id: str) -> Ticker:
        return Ticker(inst_id, self.last[inst_id])

    def instrument_spec(self, inst_id: str) -> InstrumentSpec:
        if inst_id not in SPECS:
            raise OkxError("no_data", f"no rules for {inst_id}")
        return SPECS[inst_id]

    def account_config(self) -> AccountConfig:
        return self.config

    def account_state(self) -> AccountState:
        balance = CurrencyBalance("USDT", eq=2000.0, eq_usd=2000.0, avail_eq=self.available)
        return AccountState(AccountSnapshot(total_eq=2000.0, currencies=(balance,)), list(self.positions))

    def pending_orders(self, inst_id: str | None = None) -> list[OrderInfo]:
        return [order for order in self.pending if not inst_id or order.inst_id == inst_id]

    def order(self, inst_id: str, ord_id: str = "", cl_ord_id: str = "") -> OrderInfo:
        if self.lookup_failures:
            raise self.lookup_failures.pop(0)
        for order in self.orders.values():
            if (ord_id and order.ord_id == ord_id) or (cl_ord_id and order.cl_ord_id == cl_ord_id):
                return order
        raise OkxError("51603", "Order does not exist")

    def leverage(self, inst_id: str, mgn_mode: str) -> float:
        return self.lever

    # ---------------------------------------------------------------- changes

    def _begin(self, name: str, details: dict) -> None:
        self.changes.append((name, details))
        queue = self.failures.get(name)
        if queue:
            raise queue.pop(0)

    def _end(self, name: str) -> None:
        if name in self.lost:
            self.lost.discard(name)
            raise OkxNetworkError("connection reset")

    def named(self, name: str) -> list[dict]:
        return [details for call, details in self.changes if call == name]

    def place_order(self, **order) -> str:
        self._begin("place_order", order)
        self._next += 1
        market = order["ord_type"] == "market"
        info = OrderInfo(
            ord_id=str(self._next), inst_id=order["inst_id"], side=order["side"], ord_type=order["ord_type"],
            px=None if market else float(order["px"]), sz=float(order["sz"]), filled_sz=float(order["sz"]) if market else 0.0,
            avg_px=self.last[order["inst_id"]] if market else None, state="filled" if market else "live",
            reduce_only=bool(order.get("reduce_only")), cl_ord_id=order["cl_ord_id"],
        )  # fmt: skip
        self.orders[info.ord_id] = info
        if not market:
            self.pending.append(info)
        self._end("place_order")
        return info.ord_id

    def close_position(self, inst_id: str, mgn_mode: str, pos_side: str | None = None, cl_ord_id: str = "") -> None:
        self._begin("close_position", {"inst_id": inst_id, "mgn_mode": mgn_mode, "pos_side": pos_side, "cl_ord_id": cl_ord_id})
        closed = [p for p in self.positions if p.inst_id == inst_id and (not pos_side or p.pos_side == pos_side)]
        if not closed:
            raise OkxError("51023", "Position does not exist")
        self.positions = [p for p in self.positions if p not in closed]
        self._next += 1
        position = closed[0]
        self.orders[str(self._next)] = OrderInfo(
            ord_id=str(self._next), inst_id=inst_id, side="sell" if position.direction == "long" else "buy", ord_type="market",
            sz=position.qty, filled_sz=position.qty, avg_px=self.last[inst_id], state="filled", reduce_only=True,
            cl_ord_id=cl_ord_id, pnl=position.upl,
        )  # fmt: skip
        self._end("close_position")

    def cancel_order(self, inst_id: str, ord_id: str) -> None:
        self._begin("cancel_order", {"inst_id": inst_id, "ord_id": ord_id})
        if not any(order.ord_id == ord_id for order in self.pending):
            raise OkxError("51400", "Cancellation failed as the order does not exist")
        self.pending = [order for order in self.pending if order.ord_id != ord_id]
        self._end("cancel_order")

    def _with_stops(self, inst_id: str, change) -> None:
        self.positions = [replace(p, stops=tuple(change(p.stops))) if p.inst_id == inst_id else p for p in self.positions]

    def place_stop(self, **stop) -> str:
        self._begin("place_stop", stop)
        self._next += 1
        new = StopOrder(
            str(self._next),
            float(stop["sl_trigger_px"]) if stop.get("sl_trigger_px") else None,
            float(stop["tp_trigger_px"]) if stop.get("tp_trigger_px") else None,
        )
        self._with_stops(stop["inst_id"], lambda stops: [*stops, new])
        self._end("place_stop")
        return new.algo_id

    def amend_stop(self, inst_id: str, algo_id: str, sl_trigger_px: str | None = None, tp_trigger_px: str | None = None) -> None:
        asked = {"inst_id": inst_id, "algo_id": algo_id, "sl_trigger_px": sl_trigger_px, "tp_trigger_px": tp_trigger_px}
        self._begin("amend_stop", {name: value for name, value in asked.items() if value is not None})

        def change(stops):
            return [
                StopOrder(
                    s.algo_id,
                    float(sl_trigger_px) if sl_trigger_px else s.sl_trigger_px,
                    float(tp_trigger_px) if tp_trigger_px else s.tp_trigger_px,
                )
                if s.algo_id == algo_id
                else s
                for s in stops
            ]

        self._with_stops(inst_id, change)
        self._end("amend_stop")

    def cancel_stops(self, items) -> None:
        self._begin("cancel_stops", {"items": list(items)})
        gone = {algo_id for _, algo_id in items}
        for inst_id in {inst_id for inst_id, _ in items}:
            self._with_stops(inst_id, lambda stops: [s for s in stops if s.algo_id not in gone])
        self._end("cancel_stops")


def gold(**overrides):
    """The friend's real position, more or less: 10 XAU long at 100x."""
    values = dict(
        qty=10000.0, avg_px=3844.8, mark_px=3880.8, upl=360.0, lever=100.0, liq_px=3713.07, notional_usd=38808.0,
        stops=(), ccy="USDT", pos_side="net",
    )  # fmt: skip
    values.update(overrides)
    return make_position(**values)


def small(**overrides):
    """A position that fits inside the default limits: 0.2 XAU long."""
    return gold(**{**dict(qty=200.0, upl=7.2, notional_usd=776.16), **overrides})


class Bench:
    def __init__(self, exchange: FakeExchange | None = None, journal: Journal | None = None, trading="demo", day_change=None, **cfg):
        self.exchange = exchange or FakeExchange()
        self.journal = journal or Journal(":memory:")
        self.clock = Clock(1_800_000_000.0)
        self.codes = iter(["1111", "2222", "3333", "4444", "5555", "6666"])
        self.ids = iter(f"ftb{n:04d}" for n in range(1, 99))
        values = dict(trading=trading, trade_instruments=(XAU, BTC), okx_demo_api_key="k", okx_demo_secret_key="s", okx_demo_passphrase="p")
        values.update(cfg)
        self.cfg = make_cfg(**values)
        self.desk = TradeDesk(
            self.cfg, self.journal, reader=self.exchange, trader=self.exchange if trading != "off" else None,
            resolver=FakeResolver(), clock=self.clock, sleep=lambda seconds: None,
            new_code=lambda: next(self.codes), new_id=lambda: next(self.ids), day_change=day_change,
        )  # fmt: skip

    def say(self, text: str, refused_here: str = ""):
        return self.desk.handle(text, refused_here=refused_here)

    def run(self, text: str, code: str = "1111"):
        """Sends a command and confirms it."""
        preview = self.say(text)
        assert f"确认 {code}" in preview.text, preview.text
        return self.say(f"确认 {code}")

    @property
    def sent(self) -> list[tuple[str, dict]]:
        return self.exchange.changes

    def rows(self) -> list[dict]:
        return self.journal.orders_between(0, 2_000_000_000)


# ------------------------------------------------------------------ reading commands


def parser(instruments=(XAU, BTC)) -> CommandParser:
    return CommandParser(FakeResolver(), lambda: instruments)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("做多 XAU 100张", Command("open", XAU, "long", "market", Decimal(100), "contracts")),
        ("做空 BTC 0.01个", Command("open", BTC, "short", "market", Decimal("0.01"), "coins")),
        ("做多 XAU 500U", Command("open", XAU, "long", "market", Decimal(500), "usd")),
        ("做多 xau 500 usdt", Command("open", XAU, "long", "market", Decimal(500), "usd")),
        ("做多 0.01 BTC", Command("open", BTC, "long", "market", Decimal("0.01"), "coins")),
        ("买入 比特币 0.01个", Command("open", BTC, "long", "market", Decimal("0.01"), "coins")),
        ("帮我做多 XAU 100张", Command("open", XAU, "long", "market", Decimal(100), "contracts")),
        ("市价做空 黄金 0.5盎司", Command("open", XAU, "short", "market", Decimal("0.5"), "coins")),
        ("限价做多 XAU 100张 3850", Command("open", XAU, "long", "limit", Decimal(100), "contracts", Decimal(3850))),
        ("挂单做空 BTC 0.01个 68000", Command("open", BTC, "short", "limit", Decimal("0.01"), "coins", Decimal(68000))),
        ("挂多 XAU 100张 3850.5", Command("open", XAU, "long", "limit", Decimal(100), "contracts", Decimal("3850.5"))),
        ("做多 XAU 100张 限价 3850", Command("open", XAU, "long", "limit", Decimal(100), "contracts", Decimal(3850))),
        ("做多 XAU 100张 @3850", Command("open", XAU, "long", "limit", Decimal(100), "contracts", Decimal(3850))),
        (
            "做多 XAU 100张 止损 3860 止盈 3950",
            Command("open", XAU, "long", "market", Decimal(100), "contracts", sl=Decimal(3860), tp=Decimal(3950)),
        ),
        (
            "做多　ＸＡＵ　１００张，止损３８６０",  # full-width, as a Chinese keyboard types it
            Command("open", XAU, "long", "market", Decimal(100), "contracts", sl=Decimal(3860)),
        ),
        ("加仓 XAU 100张", Command("open", XAU, "same", "market", Decimal(100), "contracts")),
        ("平仓", Command("close")),
        ("平仓 XAU", Command("close", XAU)),
        ("市价平仓 XAU", Command("close", XAU)),
        ("平仓 XAU 全部", Command("close", XAU)),
        ("平多 XAU", Command("close", XAU, "long")),
        ("平空 btc", Command("close", BTC, "short")),
        ("平仓 XAU 一半", Command("close", XAU, size=Decimal("0.5"), unit="fraction")),
        ("平仓 xau 30%", Command("close", XAU, size=Decimal("0.3"), unit="fraction")),
        ("平仓 0.5 BTC", Command("close", BTC, size=Decimal("0.5"), unit="coins")),
        ("减仓 XAU 100张", Command("close", XAU, size=Decimal(100), unit="contracts", reduce=True)),
        ("全部平仓", Command("close", everything=True)),
        ("清仓", Command("close", everything=True)),
        ("挂单", Command("orders")),
        ("挂单 XAU", Command("orders", XAU)),
        ("委托", Command("orders")),
        ("撤单", Command("cancel")),
        ("撤单 XAU", Command("cancel", XAU)),
        ("取消挂单 btc", Command("cancel", BTC)),
        ("撤单 全部", Command("cancel", everything=True)),
        ("撤单 1234567890", Command("cancel", ord_id="1234567890")),
        ("止损 XAU 3860", Command("stop", XAU, sl=Decimal(3860))),
        ("止盈 XAU 3950", Command("stop", XAU, tp=Decimal(3950))),
        ("止损 XAU 3860 止盈 3950", Command("stop", XAU, sl=Decimal(3860), tp=Decimal(3950))),
        ("设置止损 3860", Command("stop", sl=Decimal(3860))),
        ("移动止损 XAU 到 3870", Command("stop", XAU, sl=Decimal(3870))),
        ("止损改到3860.5", Command("stop", sl=Decimal("3860.5"))),
        ("取消止损 XAU", Command("stop", XAU, clear_sl=True)),
        ("取消止盈", Command("stop", clear_tp=True)),
        ("取消止盈止损 XAU", Command("stop", XAU, clear_sl=True, clear_tp=True)),
        # The way people say it in a chat.
        ("把止损调到 3860", Command("stop", sl=Decimal(3860))),
        ("帮我把 XAU 的止损改成 3860", Command("stop", XAU, sl=Decimal(3860))),
        ("调整一下我的止损，放到 3860", Command("stop", sl=Decimal(3860))),
        ("把XAU止损挪到3860", Command("stop", XAU, sl=Decimal(3860))),
        ("止损上移到 3860", Command("stop", sl=Decimal(3860))),
        ("止损调整到 3860 止盈调到 3950", Command("stop", sl=Decimal(3860), tp=Decimal(3950))),
        ("请把止盈设置为 3950", Command("stop", tp=Decimal(3950))),
        ("改一下止盈 3950", Command("stop", tp=Decimal(3950))),
        ("把止损撤掉", Command("stop", clear_sl=True)),
        ("把 XAU 的止盈止损都取消", Command("stop", XAU, clear_sl=True, clear_tp=True)),
        ("确认 1234", Command("confirm", code="1234")),
        ("确认1234", Command("confirm", code="1234")),
        ("确认", Command("confirm")),
        ("取消", Command("abort")),
        ("停止交易", Command("pause")),
        ("恢复交易", Command("resume")),
        ("交易设置", Command("settings")),
        ("交易帮助", Command("help")),
    ],
)
def test_commands_are_read_exactly(text, expected):
    assert parser().parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "黄金现在怎么看",
        "仓位",
        "做多情绪很浓",
        "做多 XAU 100张 怎么样",  # a question, not an order
        "做多 XAU 100张？",
        "要不要做多 XAU 100张",
        "做多还是做空 XAU 100张",
        "止损被扫了",
        "我的止损被扫了",
        "我把止损设在3860了",
        "我今天止损了两次",
        "帮我看看止损",
        "止损太近了",
        "止损放在 3860 合适吗",
        "把止损调到哪里比较好",
        "平仓以后价格又涨回去了真是太可惜了",
        "挂单多不多",
        "今天做多 XAU 100张",  # the verb is not at the start
        "",
        "记住：做多 XAU 100张 之前先看美元指数",
    ],
)
def test_talk_about_trading_is_not_an_order(text):
    assert parser().parse(text) is None


@pytest.mark.parametrize(
    "text, hint",
    [
        ("做多 XAU", "数量和单位"),
        ("做多 XAU 100", "多出来一个数字 100"),
        ("做多 XAU 100张 3850", "多出来一个数字 3850"),
        ("做多 XAU 1手", "没有“手”"),
        ("做多 XAU 50%", "百分比"),
        ("做多 BTC ETH 1张", "一个品种"),
        ("做多 XAU 100张 200张", "数量写了不止一个"),
        ("做多 XAU 100张 止损 3860 止损 3850", "止损价写了不止一个"),
        ("做多 XAU 65,000张", "逗号"),
        ("限价做多 BTC 0.01个 65，000", "逗号"),
        ("做多 XAU 100张 尽快", "没看懂"),
        ("限价做多 XAU 100张", "限价单要写价格"),
        ("市价做多 XAU 100张 限价 3850", "市价单不用写价格"),
        ("限价做多 XAU 100张 3850 市价", "只写一种"),
        ("做多 BTC 0.5盎司", "盎司"),
        ("减仓 XAU", "减多少"),
        ("平仓 XAU 500U", "张、个 或 百分比"),
        ("平仓 XAU 一半 30%", "不止一个"),
        ("限价平仓 XAU", "只按市价"),
        ("平仓了", "没看懂"),
        ("撤单 那张", "撤哪张单"),
        ("止损 XAU", "要写价格"),
        ("止损 XAU 3860 3870", "止盈止损没看懂"),
        ("卖出 XAU 100张", "卖出"),
        ("反手 XAU", "分两步"),
    ],
)
def test_an_order_that_is_not_fully_understood_is_refused_not_guessed(text, hint):
    command = parser().parse(text)
    assert command is not None and command.kind == "unclear", command
    assert hint in command.note


def test_the_instrument_may_be_left_out_only_when_one_is_allowed():
    assert parser((XAU,)).parse("做多 100张") == Command("open", XAU, "long", "market", Decimal(100), "contracts")
    unclear = parser((XAU, BTC)).parse("做多 100张")
    assert unclear.kind == "unclear" and "要写品种" in unclear.note


# ------------------------------------------------------------------- off, and where


def test_with_trading_off_nothing_is_sent_and_the_owner_is_told_why():
    bench = Bench(trading="off")
    for text in ("做多 XAU 100张", "平仓 XAU", "撤单 全部", "止损 XAU 3860", "做多 XAU"):
        reply = bench.say(text)
        assert "关着" in reply.text and "没有执行" in reply.text
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"
    assert bench.sent == [] and bench.rows() == []
    assert bench.desk.mode == "off"


def test_help_settings_and_the_order_list_work_while_trading_is_off():
    exchange = FakeExchange([gold(stops=(StopOrder("a1", 3700.0, None),))])
    exchange.pending.append(OrderInfo("55", XAU, "buy", px=3850.0, sz=100.0, state="live"))
    bench = Bench(exchange, trading="off")
    assert "做多 XAU 100张" in bench.say("交易帮助").text and "TRADING=off" in bench.say("交易帮助").text
    settings = bench.say("交易设置").text
    assert "**下单功能**：已关闭" in settings and "单笔上限 1,000 USDT" in settings and "提现权限：没有" in settings
    listing = bench.say("挂单")
    assert "实盘" in listing.title
    assert "XAU-USDT-SWAP 买入 限价 3,850.0，100 张（合 0.1 XAU），订单号 55" in listing.text
    assert "XAU-USDT-SWAP 多单：止损 3,700.0，止盈 无" in listing.text


def test_a_blocked_setting_is_explained():
    bench = Bench(trading="off", trading_blocked="TRADING=demo 需要模拟盘的三项 key")
    assert "TRADING=demo 需要模拟盘的三项 key" in bench.say("做多 XAU 100张").text
    assert "保持关闭" in bench.say("交易设置").text


def test_the_settings_say_where_orders_are_taken_and_the_risk_rule():
    settings = Bench().say("交易设置").text
    assert "**群里**：你本人在登记的群里 @我 发的指令也执行，别人发的不执行" in settings
    assert "**单笔风险**：权益的 1%" in settings
    assert "GROUP_TRADING=false" in Bench(group_trading=False).say("交易设置").text
    assert "还没有登记群" in Bench(group_chat_id="").say("交易设置").text


def test_where_orders_are_not_taken_the_desk_says_so_and_does_nothing():
    bench = Bench()
    for text in ("做多 XAU 100张", "确认 1111", "全部平仓"):
        assert bench.say(text, refused_here="请到私聊里发。").text == "请到私聊里发。"
    assert bench.sent == []
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"
    assert bench.say("黄金现在怎么看", refused_here="请到私聊里发。") is None  # not an order: the rest of the bot answers


def test_anything_that_is_not_an_order_is_left_to_the_rest_of_the_bot():
    bench = Bench()
    assert bench.say("黄金现在怎么看") is None
    assert not bench.desk.is_command("仓位") and bench.desk.is_command("做多 XAU 100张")


# ------------------------------------------------------------ preview and confirmation


def test_a_market_order_is_previewed_and_sent_only_on_confirmation():
    bench = Bench()
    preview = bench.say("做多 XAU 100张 止损 3860 止盈 3950")

    assert preview.title == "下单预览 · 模拟盘" and preview.template == "orange"
    assert "**市价做多 XAU-USDT-SWAP**" in preview.text
    assert "数量 100 张（合 0.1 XAU），按现价 3,880.0 约 388.00 USDT（名义价值）" in preview.text
    assert "杠杆 100 倍（在 OKX 里设的），大约占用保证金 3.88 USDT，全仓" in preview.text
    assert "止损 3,860.0（离现价 0.52%，触发后大约亏 2.00 USDT）" in preview.text
    assert "止盈 3,950.0（离现价 1.80%，触发后大约赚 7.00 USDT）" in preview.text
    assert "下单后 XAU-USDT-SWAP 的多单约 388.00 USDT（上限 3,000）" in preview.text
    assert "回复 **确认 1111** 执行" in preview.text and "120 秒内有效" in preview.text
    assert bench.sent == []  # nothing has gone to OKX yet

    done = bench.say("确认 1111")

    assert bench.sent == [
        (
            "place_order",
            {
                "inst_id": XAU, "td_mode": "cross", "side": "buy", "ord_type": "market", "sz": "100",
                "sl_trigger_px": "3860", "tp_trigger_px": "3950", "cl_ord_id": "ftb0001",
            },
        )
    ]  # fmt: skip
    assert done.title == "已执行 · 模拟盘" and done.template == "green"
    assert "已成交 100 张（合 0.1 XAU），均价 3,880.0" in done.text and "订单号 7001" in done.text
    assert "随单带的止损 3,860.0，止盈 3,950.0，成交后自动生效" in done.text
    row = bench.rows()[0]
    assert (row["mode"], row["action"], row["result"], row["inst_id"]) == ("demo", "open", "ok", XAU)
    events = bench.journal.events_between(0, 2_000_000_000)
    assert events[0]["kind"] == "trade" and events[0]["summary"] == "[模拟盘] 市价做多 XAU-USDT-SWAP：已执行"


def test_a_live_preview_says_so_in_red():
    bench = Bench(trading="live")
    preview = bench.say("做多 XAU 100张")
    assert preview.title == "下单预览 · 实盘" and preview.template == "red"
    assert bench.say("确认 1111").title == "已执行 · 实盘"
    assert bench.journal.events_between(0, 2_000_000_000)[0]["summary"] == "市价做多 XAU-USDT-SWAP：已执行"


def test_a_code_works_once():
    bench = Bench()
    assert bench.run("做多 XAU 100张").title.startswith("已执行")
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"
    assert len(bench.sent) == 1


def test_a_wrong_code_does_nothing_and_three_of_them_void_the_preview():
    bench = Bench()
    bench.say("做多 XAU 100张")
    assert "确认码不对" in bench.say("确认 9999").text
    assert "带上" in bench.say("确认").text  # no code at all is not a wrong one
    assert "确认码不对" in bench.say("确认 0000").text
    assert "已作废" in bench.say("确认 1234").text
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"
    assert bench.sent == []


def test_a_preview_expires():
    bench = Bench()
    bench.say("做多 XAU 100张")
    bench.clock.advance(121)
    assert "过期" in bench.say("确认 1111").text
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"
    assert bench.sent == []


def test_the_owner_can_call_it_off():
    bench = Bench()
    bench.say("做多 XAU 100张")
    assert "已作废：市价做多 XAU-USDT-SWAP" in bench.say("取消").text
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"
    assert bench.say("取消").text == "现在没有等待确认的指令。"
    assert bench.sent == []


def test_a_new_command_replaces_the_one_waiting():
    bench = Bench()
    bench.say("做多 XAU 100张")
    second = bench.say("做空 BTC 0.01个")
    assert "确认 2222" in second.text
    assert "确认码不对" in bench.say("确认 1111").text  # the first code died with its preview
    bench.say("确认 2222")
    assert [details["inst_id"] for _, details in bench.sent] == [BTC]


def test_a_command_that_is_not_understood_leaves_the_waiting_preview_alone():
    bench = Bench()
    bench.say("做多 XAU 100张")
    assert "没有执行任何操作" in bench.say("做多 XAU").text
    assert bench.say("确认 1111").title.startswith("已执行")


# ----------------------------------------------------------------------------- sizes


def test_a_size_in_usdt_is_turned_into_whole_contracts():
    bench = Bench()
    preview = bench.say("做多 XAU 500U")
    # 500 / (3880 * 0.001) = 128.86 contracts, rounded down
    assert "数量 128 张（合 0.128 XAU），按现价 3,880.0 约 496.64 USDT" in preview.text
    bench.say("确认 1111")
    assert bench.sent[0][1]["sz"] == "128"


def test_the_contract_count_is_fixed_at_the_preview():
    bench = Bench()
    bench.say("做多 XAU 500U")
    bench.exchange.last[XAU] = 3870.0  # at this price 500 U would be 129 contracts
    bench.say("确认 1111")
    assert bench.sent[0][1]["sz"] == "128"


def test_a_size_in_coins():
    bench = Bench()
    assert "数量 1.5 张（合 0.015 BTC），按现价 65,000.0 约 975.00 USDT" in bench.say("做多 BTC 0.015个").text
    bench.say("确认 1111")
    assert bench.sent[0][1]["sz"] == "1.5"
    assert "数量 0.5 张（合 0.005 BTC）" in bench.say("做多 0.005 BTC").text
    # Rounded down to the size step, never up.
    assert "数量 1.23 张（合 0.0123 BTC）" in bench.say("做多 BTC 0.01239个").text


def test_too_small_and_off_step_sizes_are_refused():
    bench = Bench()
    assert "数量太小" in bench.say("做多 XAU 3U").text
    assert "最少下 0.01 张，也就是 0.0001 BTC" in bench.say("做多 BTC 0.00001个").text
    assert "整数倍" in bench.say("做多 XAU 10.5张").text
    assert bench.sent == []


def test_prices_of_cheap_coins_keep_their_decimals():
    bench = Bench(trade_instruments=(DOGE,))
    preview = bench.say("做多 DOGE 2张 止损 0.19")
    assert "数量 2 张（合 2000 DOGE），按现价 0.20000 约 400.00 USDT" in preview.text
    assert "止损 0.19000" in preview.text


# ---------------------------------------------------------------------- limit orders


def test_a_limit_order_rests_until_the_price_comes():
    bench = Bench()
    preview = bench.say("限价做多 XAU 100张 3850.04 止损 3830")
    assert "**限价做多 XAU-USDT-SWAP**" in preview.text
    assert "按限价 3,850.0 约 385.00 USDT" in preview.text  # rounded to the price step
    assert "现价 3,880.0，限价比现价低 0.77%，价格到了才成交" in preview.text
    assert "止损 3,830.0（离限价 0.52%" in preview.text

    done = bench.say("确认 1111")
    assert bench.sent[0][1] == {
        "inst_id": XAU, "td_mode": "cross", "side": "buy", "ord_type": "limit", "sz": "100", "px": "3850",
        "sl_trigger_px": "3830", "cl_ord_id": "ftb0001",
    }  # fmt: skip
    assert "已挂单，等待成交：限价 3,850.0，100 张" in done.text
    assert "XAU-USDT-SWAP 买入 限价 3,850.0，100 张（合 0.1 XAU），订单号 7001" in bench.say("挂单").text


def test_a_limit_price_on_the_wrong_side_of_the_market_is_refused():
    bench = Bench()
    high = bench.say("限价做多 XAU 100张 3990")
    assert "比现价 3,880.0 高了 2.84%" in high.text and "市价单" in high.text
    low = bench.say("限价做空 XAU 100张 3800")
    assert "低了 2.06%" in low.text
    assert "确认" in bench.say("限价做多 XAU 100张 3900").text  # 0.5% through the market is allowed
    assert "会立刻成交" in bench.say("限价做多 XAU 100张 3900").text


def test_a_limit_price_far_from_the_market_is_taken_for_a_typo():
    bench = Bench()
    assert "像是输错了" in bench.say("限价做多 XAU 100张 385").text
    assert bench.sent == []


# ----------------------------------------------------------------------------- limits


def test_the_order_size_limit():
    bench = Bench()
    refusal = bench.say("做多 XAU 300张")
    assert "约 1,164.00 USDT，超过了单笔上限 1,000" in refusal.text and "飞书里改不了" in refusal.text
    assert "确认" in Bench(trade_max_order_usd=2000.0).say("做多 XAU 300张").text


def test_the_position_limit_counts_the_position_and_the_open_orders():
    exchange = FakeExchange([small(qty=500.0, notional_usd=1940.0)])
    exchange.pending.append(OrderInfo("55", XAU, "buy", px=3850.0, sz=200.0, state="live"))
    exchange.pending.append(OrderInfo("56", XAU, "sell", px=3950.0, sz=500.0, state="live", reduce_only=True))
    bench = Bench(exchange)
    refusal = bench.say("做多 XAU 100张")
    assert "会到约 3,098.00 USDT（现有 1,940.00，挂单 770.00，这张 388.00）" in refusal.text
    assert "TRADE_MAX_POSITION_USD" in refusal.text
    assert "确认" in bench.say("做多 XAU 70张").text


def test_the_friends_real_position_is_far_above_the_default_limits():
    bench = Bench(FakeExchange([gold()]))
    assert "超过了单个品种的持仓上限 3,000" in bench.say("加仓 XAU 100张").text


def test_the_number_of_opening_orders_per_day():
    bench = Bench(trade_max_opens_per_day=2)
    assert bench.run("做多 XAU 10张", "1111").title.startswith("已执行")
    assert bench.run("做多 XAU 10张", "2222").title.startswith("已执行")
    assert "今天已经开仓 2 次" in bench.say("做多 XAU 10张").text
    bench.exchange.positions = [small()]
    assert bench.run("平仓 XAU", "3333").title.startswith("已执行")  # getting out is not an opening order
    bench.clock.advance(24 * 3600)
    assert "确认" in bench.say("做多 XAU 10张").text


def test_the_daily_loss_line_stops_new_positions_on_the_real_account_only():
    assert "每日亏损线 200 USD" in Bench(trading="live", trade_daily_loss_limit_usd=200.0, day_change=lambda: -250.0).say("做多 XAU 10张").text
    assert "确认" in Bench(trading="live", trade_daily_loss_limit_usd=200.0, day_change=lambda: -150.0).say("做多 XAU 10张").text
    assert "确认" in Bench(trading="demo", trade_daily_loss_limit_usd=200.0, day_change=lambda: -250.0).say("做多 XAU 10张").text


def test_only_listed_linear_perpetuals_can_be_traded():
    bench = Bench()
    assert "不在允许下单的品种里（现在允许：XAU-USDT-SWAP、BTC-USDT-SWAP）" in bench.say("做多 ETH 1张").text
    assert "只支持永续合约" in Bench(trade_instruments=("BTC-USDT",)).say("做多 BTC-USDT 1个").text
    assert "币本位" in Bench(trade_instruments=("BTC-USD-SWAP",)).say("做多 BTC-USD-SWAP 1张").text
    assert bench.sent == []


def test_an_instrument_that_is_not_trading_is_refused(monkeypatch):
    monkeypatch.setitem(SPECS, XAU, replace(SPECS[XAU], state="suspend"))
    assert "不在正常交易状态" in Bench().say("做多 XAU 100张").text


def test_a_key_without_the_trade_permission_is_refused():
    bench = Bench(FakeExchange(permissions=("read_only",)))
    assert "没有交易权限" in bench.say("做多 XAU 100张").text
    bench.exchange.config = AccountConfig("net_mode", ("read_only", "trade"))
    assert "确认" in bench.say("做多 XAU 100张").text  # read again once the owner fixed the key


def test_a_spot_mode_account_cannot_open_contracts_and_says_how_to_fix_it():
    bench = Bench()
    bench.exchange.config = AccountConfig("net_mode", ("read_only", "trade"), "", "1")
    refusal = bench.say("做多 XAU 100张")
    assert "现货模式" in refusal.text and "改成“合约模式”" in refusal.text and "请在 OKX 的模拟交易里进合约的交易页" in refusal.text
    assert "交易设置" in refusal.text and "交易模式" in refusal.text
    assert "确认" not in refusal.text and bench.sent == []
    assert "没有等待确认的指令" in bench.say("确认 1111").text
    bench.exchange.config = AccountConfig("net_mode", ("read_only", "trade"), "", "2")
    assert "确认" in bench.say("做多 XAU 100张").text  # read again once the account mode was changed


def test_a_spot_mode_account_can_still_cancel_and_look():
    bench = Bench()
    bench.exchange.config = AccountConfig("net_mode", ("read_only", "trade"), "", "1")
    assert "现在没有挂单" in bench.say("挂单").text
    assert "现货模式" not in bench.say("平仓 XAU").text  # nothing to close, and that is what it says


def test_opening_against_an_existing_position_is_refused_in_net_mode():
    bench = Bench(FakeExchange([small()]))
    refusal = bench.say("做空 XAU 100张")
    assert "现在有多单" in refusal.text and "平仓 XAU" in refusal.text
    assert bench.sent == []


def test_hedge_mode_names_the_position_side():
    bench = Bench(FakeExchange([small(pos_side="long")], hedge=True))
    bench.run("做空 XAU 100张")
    assert bench.sent[0][1]["pos_side"] == "short" and bench.sent[0][1]["side"] == "sell"


def test_adding_follows_the_position_and_its_margin_mode():
    bench = Bench(FakeExchange([small(direction="short", mgn_mode="isolated")]))
    preview = bench.say("加仓 XAU 100张")
    assert "市价做空" in preview.text and "逐仓" in preview.text
    bench.say("确认 1111")
    assert bench.sent[0][1]["side"] == "sell" and bench.sent[0][1]["td_mode"] == "isolated"
    assert "没有仓位，没法加仓" in Bench().say("加仓 XAU 100张").text


def test_a_new_position_uses_the_margin_mode_from_the_settings():
    bench = Bench(trade_margin_mode="isolated")
    bench.run("做多 XAU 100张")
    assert bench.sent[0][1]["td_mode"] == "isolated"


def test_a_stop_can_be_made_compulsory():
    bench = Bench(trade_require_stop=True)
    assert "必须带止损" in bench.say("做多 XAU 100张").text
    assert "确认" in bench.say("做多 XAU 100张 止损 3860").text


def test_stops_on_the_wrong_side_are_refused():
    bench = Bench()
    assert "多单的止损要低于现价 3,880.0，你写的是 3,900.0" in bench.say("做多 XAU 100张 止损 3900").text
    assert "多单的止盈要高于现价" in bench.say("做多 XAU 100张 止盈 3800").text
    assert "空单的止损要高于现价" in bench.say("做空 XAU 100张 止损 3800").text
    assert "空单的止盈要低于限价 3,900.0" in bench.say("限价做空 XAU 100张 3900 止盈 3950").text
    assert bench.sent == []


def test_thin_margin_is_pointed_out_but_left_to_okx():
    bench = Bench()
    bench.exchange.available = 2.0
    assert "可用保证金只有 2.00 USDT，可能不够" in bench.say("做多 XAU 100张").text


def test_a_pause_stops_new_positions_and_survives_a_restart():
    journal = Journal(":memory:")
    bench = Bench(FakeExchange([small()]), journal)
    bench.say("做多 XAU 100张")
    assert "已暂停" in bench.say("停止交易").text
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"  # the waiting preview went with it
    assert "交易已暂停" in bench.say("做多 XAU 100张").text
    assert journal.get_setting(PAUSED_SETTING) == "1"

    restarted = Bench(FakeExchange([small()]), journal)
    assert "交易已暂停" in restarted.say("做多 XAU 100张").text
    assert "开仓已暂停" in restarted.say("交易设置").text
    # Getting out stays possible.
    assert "确认" in restarted.say("平仓 XAU").text
    assert "确认" in restarted.say("止损 XAU 3860").text

    assert "已恢复" in restarted.say("恢复交易").text
    assert "确认" in restarted.say("做多 XAU 100张").text


# ------------------------------------------------------- the world moves before "确认"


def test_a_price_that_moved_too_far_voids_the_confirmation():
    bench = Bench()
    bench.say("做多 XAU 100张")
    bench.exchange.last[XAU] = 3905.0
    refusal = bench.say("确认 1111")
    assert "没有执行" in refusal.text and "动了 0.64%（预览时 3,880.0，现在 3,905.0）" in refusal.text
    assert bench.sent == []
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"


def test_a_small_move_is_fine():
    bench = Bench()
    bench.say("做多 XAU 100张")
    bench.exchange.last[XAU] = 3885.0
    assert bench.say("确认 1111").title.startswith("已执行")


def test_a_position_that_changed_voids_the_confirmation():
    bench = Bench(FakeExchange([small()]))
    bench.say("平仓 XAU")
    bench.exchange.positions = [small(qty=150.0)]
    assert "仓位或止盈止损变了" in bench.say("确认 1111").text
    assert bench.sent == []


def test_every_limit_is_checked_again_at_the_confirmation():
    bench = Bench()
    bench.say("做多 XAU 100张")
    bench.journal.set_setting(PAUSED_SETTING, "1")
    assert "没有执行。交易已暂停" in bench.say("确认 1111").text
    assert bench.sent == []


# ----------------------------------------------------------------------------- closing


def test_closing_a_whole_position():
    bench = Bench(FakeExchange([gold(stops=(StopOrder("a1", 3700.0, None),))]))
    preview = bench.say("平仓 XAU")
    assert "**市价平仓 XAU-USDT-SWAP 多单（全部）**" in preview.text
    assert "数量 10000 张（合 10 XAU），均价 3,844.8，标记价 3,880.8" in preview.text
    assert "按现价估算，浮动盈亏 +360.00 USDT" in preview.text
    assert "止盈止损和平仓挂单一并撤掉" in preview.text

    done = bench.say("确认 1111")
    assert bench.sent == [("close_position", {"inst_id": XAU, "mgn_mode": "cross", "pos_side": None, "cl_ord_id": "ftb0001"})]
    assert done.title == "已执行 · 模拟盘"
    assert "已成交 10000 张（合 10 XAU），均价 3,880.0" in done.text and "这笔的盈亏 +360.00 USDT（没扣手续费）" in done.text
    assert bench.rows()[0]["action"] == "close"


def test_closing_is_never_held_back_by_the_limits():
    # The position is thirteen times the position limit; getting out of it must still work.
    bench = Bench(FakeExchange([gold()]), trade_max_opens_per_day=0)
    assert bench.run("平仓").title.startswith("已执行")


def test_closing_part_of_a_position_only_ever_reduces_it():
    bench = Bench(FakeExchange([gold()]))
    preview = bench.say("平仓 XAU 一半")
    assert "**市价减仓 XAU-USDT-SWAP 多单**" in preview.text
    assert "平掉 5000 张（合 5 XAU），剩下 5000 张" in preview.text
    assert "这部分的浮动盈亏 +180.00 USDT" in preview.text
    bench.say("确认 1111")
    assert bench.sent == [
        (
            "place_order",
            {"inst_id": XAU, "td_mode": "cross", "side": "sell", "ord_type": "market", "sz": "5000", "reduce_only": True, "cl_ord_id": "ftb0001"},
        )
    ]


def test_part_sizes_in_percent_contracts_and_coins():
    bench = Bench(FakeExchange([gold()]))
    assert "平掉 3000 张" in bench.say("平仓 XAU 30%").text
    assert "平掉 100 张（合 0.1 XAU），剩下 9900 张" in bench.say("减仓 XAU 100张").text
    assert "平掉 2500 张（合 2.5 XAU）" in bench.say("减仓 XAU 2.5个").text
    assert "市价平仓 XAU-USDT-SWAP 多单（全部）" in bench.say("平仓 XAU 100%").text
    assert "只有 10000 张，平不了 20000 张" in bench.say("减仓 XAU 20000张").text


def test_a_short_is_closed_by_buying():
    bench = Bench(FakeExchange([small(direction="short")]))
    bench.run("平仓 XAU 一半")
    assert bench.sent[0][1]["side"] == "buy" and bench.sent[0][1]["reduce_only"] is True


def test_closing_in_hedge_mode_names_the_position_side():
    bench = Bench(FakeExchange([small(pos_side="long"), small(pos_side="short", direction="short")], hedge=True))
    assert "请写明要平哪一个" in bench.say("平仓 XAU").text
    bench.run("平空 XAU")
    assert bench.sent[0] == ("close_position", {"inst_id": XAU, "mgn_mode": "cross", "pos_side": "short", "cl_ord_id": "ftb0001"})
    bench.run("平多 XAU 一半", "2222")
    assert bench.sent[1][1]["pos_side"] == "long" and "reduce_only" not in bench.sent[1][1]


def test_closing_everything_leaves_other_instruments_alone():
    positions = [small(), small(inst_id=BTC, qty=1.0, direction="short", upl=-5.0), small(inst_id="ETH-USDT-SWAP")]
    bench = Bench(FakeExchange(positions))
    preview = bench.say("全部平仓")
    assert "市价平掉全部仓位（2 个）" in preview.text and "另有 1 个仓位不在允许下单的品种里" in preview.text
    done = bench.say("确认 1111")
    assert [details["inst_id"] for _, details in bench.sent] == [XAU, BTC]
    assert "平掉 XAU-USDT-SWAP 多单：已成交" in done.text and "平掉 BTC-USDT-SWAP 空单：已成交" in done.text
    assert [p.inst_id for p in bench.exchange.positions] == ["ETH-USDT-SWAP"]


def test_closing_what_is_not_there_or_not_allowed():
    bench = Bench(FakeExchange([small(inst_id="ETH-USDT-SWAP")]))
    assert "没有找到" in bench.say("平仓 XAU").text
    assert "我不动它的仓位" in bench.say("平仓 ETH").text
    assert "现在没有可以平的仓位" in bench.say("全部平仓").text
    assert "没有找到" in bench.say("平仓").text
    assert bench.sent == []


def test_one_refused_close_does_not_stop_the_others():
    bench = Bench(FakeExchange([small(), small(inst_id=BTC, qty=1.0)]))
    bench.exchange.failures["close_position"] = [OkxError("51000", "Parameter error")]
    done = bench.run("全部平仓")
    assert done.title == "只执行了一部分 · 模拟盘" and done.template == "yellow"
    assert "平掉 XAU-USDT-SWAP 多单：OKX 拒绝了，Parameter error（代码 51000）" in done.text
    assert "平掉 BTC-USDT-SWAP 多单：已成交" in done.text
    assert "请发“仓位”和“挂单”核对" in done.text
    assert bench.rows()[0]["result"] == "partial"


# -------------------------------------------------------------------------- cancelling


def resting(exchange: FakeExchange, *orders: OrderInfo) -> None:
    for order in orders:
        exchange.pending.append(order)
        exchange.orders[order.ord_id] = order


def test_cancelling_the_orders_of_one_instrument():
    exchange = FakeExchange()
    resting(
        exchange,
        OrderInfo("55", XAU, "buy", px=3850.0, sz=100.0, state="live"),
        OrderInfo("56", XAU, "buy", px=3840.0, sz=100.0, state="live"),
        OrderInfo("57", BTC, "sell", px=68000.0, sz=1.0, state="live"),
    )
    bench = Bench(exchange)
    preview = bench.say("撤单 XAU")
    assert "**撤单（2 张）**" in preview.text and "订单号 55" in preview.text and "订单号 57" not in preview.text
    done = bench.say("确认 1111")
    assert bench.sent == [("cancel_order", {"inst_id": XAU, "ord_id": "55"}), ("cancel_order", {"inst_id": XAU, "ord_id": "56"})]
    assert "已撤：订单号 55" in done.text and [order.ord_id for order in exchange.pending] == ["57"]


def test_cancelling_by_number_all_and_the_only_one():
    exchange = FakeExchange()
    resting(
        exchange,
        OrderInfo("555555", XAU, "buy", px=3850.0, sz=100.0, state="live"),
        OrderInfo("666666", BTC, "sell", px=68000.0, sz=1.0, state="live"),
        OrderInfo("777777", "ETH-USDT-SWAP", "buy", px=3000.0, sz=1.0, state="live"),
    )
    bench = Bench(exchange)
    ambiguous = bench.say("撤单")
    assert "现在有 2 张挂单，请写明撤哪张" in ambiguous.text and "订单号 555555" in ambiguous.text
    assert "没有找到订单号 999999" in bench.say("撤单 999999").text
    bench.run("撤单 666666")
    assert bench.sent == [("cancel_order", {"inst_id": BTC, "ord_id": "666666"})]
    everything = bench.say("撤单 全部")
    assert "撤单（1 张）" in everything.text and "另有 1 张挂单在不允许下单的品种上" in everything.text
    bench.say("确认 2222")
    assert [order.ord_id for order in exchange.pending] == ["777777"]
    assert "现在没有可以撤的挂单" in bench.say("撤单").text
    assert "我不动它的挂单" in bench.say("撤单 ETH").text


def test_an_order_that_filled_before_the_confirmation():
    exchange = FakeExchange()
    resting(exchange, OrderInfo("55", XAU, "buy", px=3850.0, sz=100.0, state="live"))
    bench = Bench(exchange)
    bench.say("撤单 XAU")
    exchange.pending.clear()
    assert "预览里的挂单已经不在了" in bench.say("确认 1111").text
    assert bench.sent == []


def test_new_orders_are_not_swept_up_by_an_older_preview():
    exchange = FakeExchange()
    resting(exchange, OrderInfo("55", XAU, "buy", px=3850.0, sz=100.0, state="live"))
    bench = Bench(exchange)
    bench.say("撤单 XAU")
    resting(exchange, OrderInfo("58", XAU, "buy", px=3800.0, sz=100.0, state="live"))
    bench.say("确认 1111")
    assert bench.sent == [("cancel_order", {"inst_id": XAU, "ord_id": "55"})]


# ------------------------------------------------------------------------------- stops


def test_a_first_stop_closes_the_whole_position():
    bench = Bench(FakeExchange([gold()]))
    preview = bench.say("止损 XAU 3860")
    assert "**设置止盈止损 XAU-USDT-SWAP 多单**" in preview.text
    assert "仓位 10000 张（合 10 XAU），均价 3,844.8，现价 3,880.0" in preview.text
    # Two figures, each with its starting point: from now, and from the entry.
    assert "止损 无 → 3,860.0（离现价 0.52%；触发时账户比现在少约 200.00 USDT，占权益 10.0%；从开仓均价算，这笔交易 +152.00 USDT）" in preview.text
    assert "止盈 无（不变）" in preview.text and "触发后按市价平掉整个仓位" in preview.text
    done = bench.say("确认 1111")
    assert bench.sent == [
        ("place_stop", {"inst_id": XAU, "td_mode": "cross", "side": "sell", "sl_trigger_px": "3860", "cl_ord_id": "ftb0001"})
    ]
    assert "已挂上：止损 3,860.0，触发后按市价平掉整个仓位" in done.text
    assert bench.rows()[0]["action"] == "stop"


def test_stop_loss_and_take_profit_in_one_go():
    bench = Bench(FakeExchange([gold(direction="short")]))
    bench.run("止损 XAU 3900 止盈 3800")
    assert bench.sent[0][1] == {
        "inst_id": XAU, "td_mode": "cross", "side": "buy", "sl_trigger_px": "3900", "tp_trigger_px": "3800", "cl_ord_id": "ftb0001",
    }  # fmt: skip


def test_the_size_calculator_follows_the_owners_risk_rule_and_sends_nothing():
    bench = Bench(FakeExchange([gold()]))
    reply = bench.say("算仓位 XAU 入场 3873 止损 3768")
    assert reply.title == "算仓位 · XAU-USDT-SWAP"
    assert "按你定的单笔风险 1%：权益 2,000.00 USD，这一笔止损触发时最多亏 20.00 USDT" in reply.text
    assert "多单，入场 3,873.0，止损 3,768.0，相差 105（2.71%）" in reply.text
    assert "最多 190 张（合 0.19 XAU），名义价值约 735.87 USDT，实际杠杆 0.37 倍，止损触发亏约 19.95 USDT" in reply.text
    assert "按你现在 100 倍的杠杆设置，占用保证金约 7.36 USDT" in reply.text
    assert "你现在的多单 10 XAU，按同样的止损距离要亏约 1,050.00 USDT，占权益 52.5%" in reply.text
    assert "不是开仓建议" in reply.text
    assert "空单，入场 3,880.0（现价）" in bench.say("算仓位 XAU 止损 3990").text
    assert bench.say("算仓位 XAU 3873 止损 3768").text == reply.text  # the entry may be written bare
    assert "单笔风险 2%" in Bench(FakeExchange([gold()]), risk_per_trade_pct=2.0).say("算仓位 XAU 止损 3768").text
    heavy = Bench(FakeExchange([gold()]), risk_per_trade_pct=25.0).say("算仓位 XAU 止损 3768").text
    assert "单笔风险 25%：权益 2,000.00 USD，这一笔止损触发时最多亏 500.00 USDT" in heavy
    assert "最多 4464 张（合 4.464 XAU）" in heavy and "超过了单个品种的持仓上限 3,000.00 USDT，按现在的限额开不了这么大" in heavy
    assert "超过了单笔上限 1,000.00 USDT，要分几次开" in bench.say("算仓位 XAU 止损 3841").text  # about 1,990 USDT
    assert "持仓上限 3,000.00 USDT" in bench.say("算仓位 XAU 止损 3870").text  # about 7,760 USDT
    assert bench.say("算仓位 XAU").text.startswith("算仓位要写品种和止损价")
    assert "止损价和入场价一样" in bench.say("算仓位 XAU 入场 3800 止损 3800").text
    assert bench.sent == []
    off = Bench(FakeExchange([gold()]), trading="off")
    assert off.say("算仓位 XAU 止损 3768").title == "算仓位 · XAU-USDT-SWAP"  # arithmetic, whatever the trading mode


def test_a_stop_too_tight_for_even_the_smallest_size_is_said_so():
    bench = Bench(FakeExchange([]))
    text = bench.say("算仓位 XAU 止损 1000").text
    assert "最小的 1 张也会亏约 2.88 USDT" not in text  # 1 lot of 0.001 XAU over 2,880 is 2.88: still under 20
    bench = Bench(FakeExchange([]), risk_per_trade_pct=0.1)
    assert "最小的 1 张也会亏约 2.88 USDT，占权益 0.14%，超过了 0.1%" in bench.say("算仓位 XAU 止损 1000").text


@pytest.mark.parametrize("answer", ["1111", "确认1111执行", "确认 1111 执行", "好 1111", "确认码 1111", "１１１１"])
def test_the_code_counts_however_it_is_put(answer):
    bench = Bench(FakeExchange([gold()]))
    bench.say("止损 XAU 3860")
    assert bench.say(answer).title.startswith("已执行")
    assert [name for name, _ in bench.sent] == ["place_stop"]


def test_a_wrong_code_is_answered_as_one_and_a_number_alone_is_nothing_without_a_preview():
    bench = Bench(FakeExchange([gold()]))
    assert bench.say("1111") is None  # nothing waiting: just a number, for the rest of the bot
    bench.say("止损 XAU 3860")
    assert bench.say("2222").text.startswith("确认码不对")
    assert bench.say("确认2222执行").text.startswith("确认码不对")
    assert bench.say("黄金 3860 那里的支撑强不强 1111 这个位置呢") is None  # too long to be an answer to the card
    assert bench.say("1111吗?") is None
    assert bench.sent == []
    assert bench.desk.is_command("1111")  # while a preview waits, someone else's "1111" is turned away as an order


def test_a_command_written_out_by_the_ai_is_previewed_like_any_other():
    bench = Bench(FakeExchange([gold(stops=(StopOrder("a1", 3808.0, 3938.0),))]))
    assert "XAU-USDT-SWAP 多单，标记价 3,880.8：止损 3,808.0，止盈 3,938.0" in bench.desk.positions_text()
    reply = bench.desk.handle_understood("把我的止损减两点", "止损 XAU 3806")
    assert reply.text.startswith("我把“把我的止损减两点”理解为：**止损 XAU 3806**")
    assert "止损 3,808.0 → 3,806.0" in reply.text and "确认 1111" in reply.text and "不是这个意思就回“取消”" in reply.text
    assert bench.sent == []
    assert bench.say("1111").title.startswith("已执行")
    assert bench.sent == [("amend_stop", {"inst_id": XAU, "algo_id": "a1", "sl_trigger_px": "3806"})]


def test_the_ai_can_never_confirm_cancel_or_switch_anything():
    bench = Bench(FakeExchange([gold()]))
    bench.say("止损 XAU 3860")
    for written in ("确认 1111", "1111", "取消", "停止交易", "恢复交易", "模拟盘自测", "交易设置", "黄金怎么看", ""):
        assert bench.desk.handle_understood("随便一句话", written) is None
    assert bench.sent == []
    assert bench.say("1111").title.startswith("已执行")  # the preview the owner asked for is still his to confirm


def test_asked_to_move_a_stop_without_a_price_the_desk_shows_where_it_is_now():
    bench = Bench(FakeExchange([gold(stops=(StopOrder("a1", 3700.0, None),))]))
    for text in ("帮我调整一下止损", "调整一下我的止损", "把止损调高一点", "止损 XAU"):
        reply = bench.say(text).text
        assert reply.startswith("要写价格，例如：止损 XAU 3860、把止损调到 3860")
        assert "XAU-USDT-SWAP 多单，标记价 3,880.8：止损 3,700.0，止盈 没有" in reply and "没有执行任何操作" in reply
    assert bench.sent == []
    assert "现在没有持仓" in Bench(FakeExchange([])).say("调整一下止损").text


def test_moving_an_existing_stop_amends_it():
    bench = Bench(FakeExchange([gold(stops=(StopOrder("a1", 3700.0, 3950.0),))]))
    preview = bench.say("移动止损 XAU 到 3860")
    assert "**修改止盈止损 XAU-USDT-SWAP 多单**" in preview.text
    assert "止损 3,700.0 → 3,860.0" in preview.text and "止盈 3,950.0（不变）" in preview.text
    done = bench.say("确认 1111")
    assert bench.sent == [("amend_stop", {"inst_id": XAU, "algo_id": "a1", "sl_trigger_px": "3860"})]
    assert "已改好：止损 3,860.0" in done.text
    assert "止盈止损已经是这个价位了" in bench.say("止损 XAU 3860").text


def test_adding_a_take_profit_to_a_lone_stop_loss_replaces_the_order():
    bench = Bench(FakeExchange([gold(stops=(StopOrder("a1", 3700.0, None),))]))
    preview = bench.say("止盈 XAU 3950")
    assert "止损 3,700.0（不变）" in preview.text and "止盈 无 → 3,950.0" in preview.text
    assert "中间有一两秒这个仓位没有止盈止损" in preview.text
    bench.say("确认 1111")
    assert bench.sent == [
        ("cancel_stops", {"items": [(XAU, "a1")]}),
        ("place_stop", {"inst_id": XAU, "td_mode": "cross", "side": "sell", "sl_trigger_px": "3700", "tp_trigger_px": "3950", "cl_ord_id": "ftb0001"}),
    ]


def test_a_replacement_that_fails_halfway_is_shouted_about():
    bench = Bench(FakeExchange([gold(stops=(StopOrder("a1", 3700.0, None),))]))
    bench.exchange.failures["place_stop"] = [OkxError("51280", "Trigger price is invalid")]
    done = bench.run("止盈 XAU 3950")
    assert done.title == "只执行了一部分 · 模拟盘"
    assert "已撤掉 1 张止盈止损单" in done.text
    assert "挂上新的止盈止损单：OKX 拒绝了，Trigger price is invalid（代码 51280）" in done.text
    assert "现在这个仓位没有止盈止损，请马上重新设一次" in done.text
    assert bench.exchange.positions[0].stops == ()


def test_removing_stops():
    both = (StopOrder("a1", 3700.0, 3950.0),)
    bench = Bench(FakeExchange([gold(stops=both)]))
    bench.run("取消止盈止损 XAU")
    assert bench.sent == [("cancel_stops", {"items": [(XAU, "a1")]})]

    bench = Bench(FakeExchange([gold(stops=both)]))
    assert "止损 3,700.0 → 取消" in bench.say("取消止损 XAU").text
    bench.say("确认 1111")
    assert [name for name, _ in bench.sent] == ["cancel_stops", "place_stop"]
    assert bench.sent[1][1]["tp_trigger_px"] == "3950" and "sl_trigger_px" not in bench.sent[1][1]

    bench = Bench(FakeExchange([gold(stops=(StopOrder("a1", 3700.0, None),))]))
    assert "没有止盈可以取消" in bench.say("取消止盈 XAU").text
    bench.run("取消止损 XAU")
    assert bench.sent == [("cancel_stops", {"items": [(XAU, "a1")]})]
    assert "本来就没有止盈止损单" in Bench(FakeExchange([gold()])).say("取消止损 XAU").text


def test_several_stop_orders_on_one_position_are_not_guessed_between():
    stops = (StopOrder("a1", 3700.0, None), StopOrder("a2", 3750.0, None))
    bench = Bench(FakeExchange([gold(stops=stops)]))
    assert "有 2 张止盈止损单，我分不清你要改哪一张" in bench.say("止损 XAU 3860").text
    bench.run("取消止盈止损 XAU")
    assert bench.sent == [("cancel_stops", {"items": [(XAU, "a1"), (XAU, "a2")]})]


def test_a_stop_that_would_trigger_at_once_is_refused():
    bench = Bench(FakeExchange([gold()]))
    assert "多单的止损要低于现价 3,880.0，你写的是 3,890.0" in bench.say("止损 XAU 3890").text
    assert "多单的止盈要高于现价" in bench.say("止盈 XAU 3870").text
    assert "没有找到" in Bench().say("止损 XAU 3860").text
    assert bench.sent == []


def test_the_instrument_can_be_left_out_when_there_is_one_position():
    bench = Bench(FakeExchange([gold()]))
    assert "设置止盈止损 XAU-USDT-SWAP 多单" in bench.say("止损 3860").text


# -------------------------------------------------------------------- when things break


def test_okx_saying_no_is_passed_on_in_its_own_words():
    bench = Bench()
    bench.exchange.failures["place_order"] = [OkxError("51008", "Order failed. Insufficient USDT margin in account")]
    done = bench.run("做多 XAU 100张")
    assert done.title == "没有执行 · 模拟盘" and done.template == "grey"
    assert "OKX 拒绝了：Order failed. Insufficient USDT margin in account（代码 51008）" in done.text
    assert bench.rows()[0]["result"] == "rejected"
    assert bench.journal.count_opens(0, 2_000_000_000, "demo") == 0  # a refused order is not an opening


def test_a_connection_lost_after_the_order_arrived_is_reported_as_done():
    bench = Bench()
    bench.exchange.lost.add("place_order")
    done = bench.run("做多 XAU 100张")
    assert done.title == "已执行 · 模拟盘" and "已成交 100 张" in done.text
    assert len(bench.exchange.named("place_order")) == 1


def test_a_connection_lost_before_the_order_arrived_is_never_answered_by_sending_it_again():
    bench = Bench()
    bench.exchange.failures["place_order"] = [OkxNetworkError("timeout")]
    done = bench.run("做多 XAU 100张")
    assert done.title == "结果不确定 · 模拟盘" and done.template == "red"
    assert "OKX 那边查不到这张单，多半没有下出去" in done.text and "我不会自动重试" in done.text
    assert len(bench.exchange.named("place_order")) == 1
    assert bench.rows()[0]["result"] == "unknown"
    assert bench.journal.count_opens(0, 2_000_000_000, "demo") == 1  # counted, to stay on the safe side


def test_when_okx_cannot_even_be_asked_the_owner_is_told_to_look():
    bench = Bench()
    bench.exchange.failures["place_order"] = [OkxNetworkError("timeout")]
    bench.exchange.lookup_failures = [OkxNetworkError("timeout")] * 3
    done = bench.run("做多 XAU 100张")
    assert "不确定这张单有没有到 OKX" in done.text and "请发“仓位”和“挂单”核对" in done.text
    assert len(bench.exchange.named("place_order")) == 1


def test_a_lost_close_is_checked_against_the_positions():
    bench = Bench(FakeExchange([small()]))
    bench.exchange.lost.add("close_position")
    assert "仓位已经不在了，平仓生效了" in bench.run("平仓 XAU").text

    bench = Bench(FakeExchange([small()]))
    bench.exchange.failures["close_position"] = [OkxNetworkError("timeout")]
    done = bench.run("平仓 XAU")
    assert done.title.startswith("结果不确定") and "不确定平仓有没有生效" in done.text
    assert len(bench.exchange.named("close_position")) == 1


def test_okx_being_down_at_preview_time_changes_nothing():
    bench = Bench()
    bench.exchange.ticker = lambda inst_id: (_ for _ in ()).throw(OkxNetworkError("down"))
    assert bench.say("做多 XAU 100张").text == "现在连不上 OKX，没有执行任何操作。请稍后再试。"
    bench.exchange.ticker = lambda inst_id: (_ for _ in ()).throw(OkxError("50001", "Service temporarily unavailable"))
    assert "没有执行任何操作：Service temporarily unavailable（代码 50001）" in bench.say("做多 XAU 100张").text
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"


def test_an_order_whose_fill_cannot_be_read_back_is_still_reported_as_accepted():
    bench = Bench()
    bench.say("做多 XAU 100张")
    bench.exchange.lookup_failures = [OkxNetworkError("timeout")]
    done = bench.say("确认 1111")
    assert done.title.startswith("已执行") and "OKX 已接受这张单（订单号 7001），成交情况暂时没查到" in done.text


# ---------------------------------------------------------------------------- settings


def test_the_settings_show_limits_and_what_the_key_may_do():
    bench = Bench(FakeExchange(permissions=("read_only", "trade", "withdraw")), trade_daily_loss_limit_usd=300.0)
    bench.run("做多 XAU 10张")
    text = bench.say("交易设置").text
    assert "**下单功能**：模拟盘" in text and "没有暂停" in text
    assert "**允许下单的品种**：XAU-USDT-SWAP、BTC-USDT-SWAP" in text
    assert "每天最多开仓 20 次，今天已开 1 次" in text
    assert "当天权益少了 300 USD 以后不再开新仓" in text
    assert "交易权限：有" in text and "提现权限：有。机器人用不到，建议在 OKX 里关掉" in text
    assert "已绑定 IP" in text and "203.0.113.7" not in text
    assert "单向持仓" in text and "飞书里改不了" in text
    assert "账户模式：合约模式" in text
    assert "**账户资金**（模拟盘）" in text and "总权益约 2,000.00 USD" in text
    assert "- USDT：权益 2,000.00，可用保证金 1,000.00" in text and "- 持仓 0 个" in text


def test_the_settings_say_when_the_account_cannot_trade_contracts():
    bench = Bench()
    bench.exchange.config = AccountConfig("net_mode", ("read_only", "trade"), "", "1")
    text = bench.say("交易设置").text
    assert "账户模式：现货模式。" in text and "改成“合约模式”" in text


def test_the_settings_show_no_funds_while_trading_is_off():
    assert "账户资金" not in Bench(trading="off").say("交易设置").text


# ------------------------------------------- opening the real account step by step


def test_only_the_actions_that_are_open_are_carried_out():
    bench = Bench(FakeExchange([gold()]), trading="live", trade_actions=("stops",))
    for text, name in (
        ("做多 XAU 100张", "市价开仓和加仓"),
        ("加仓 XAU 100张", "市价开仓和加仓"),
        ("限价做多 XAU 100张 3850", "限价挂单"),
        ("平仓 XAU", "平仓和减仓"),
        ("全部平仓", "平仓和减仓"),
        ("撤单 XAU", "撤单"),
    ):
        reply = bench.say(text)
        assert f"“{name}”现在没有开放，这条指令没有执行。现在只开放：设置和取消止盈止损。" in reply.text, reply.text
        assert "TRADE_ACTIONS" in reply.text and "确认" not in reply.text
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。"  # a refusal leaves nothing to confirm
    assert bench.sent == []  # nothing of it reached OKX

    done = bench.run("止损 XAU 3860")  # the one thing that is open works as ever
    assert [name for name, _ in bench.sent] == ["place_stop"] and done.title == "已执行 · 实盘"
    bench.run("取消止损 XAU", code="2222")
    assert [name for name, _ in bench.sent] == ["place_stop", "cancel_stops"]


def test_looking_is_always_allowed_and_the_settings_say_what_is_open():
    bench = Bench(FakeExchange([gold()]), trading="live", trade_actions=("stops", "close"))
    settings = bench.say("交易设置").text
    assert "**开放的操作**：现在只开放：设置和取消止盈止损、平仓和减仓。没有开放：撤单、限价挂单、市价开仓和加仓" in settings
    assert "现在只开放：设置和取消止盈止损、平仓和减仓。" in bench.say("交易帮助").text
    assert bench.say("挂单") is not None and "没有开放" not in bench.say("挂单").text
    assert "确认 1111" in bench.say("平仓 XAU").text

    everything = Bench().say("交易设置").text
    assert "**开放的操作**：全部" in everything and "没有开放" not in everything
    assert "只开放" not in Bench().say("交易帮助").text


def test_with_nothing_open_every_order_command_is_refused():
    bench = Bench(FakeExchange([gold()]), trade_actions=())
    for text in ("做多 XAU 100张", "平仓 XAU", "止损 XAU 3860", "撤单 XAU"):
        assert "现在没有开放任何下单操作" in bench.say(text).text
    assert bench.sent == [] and "没有开放任何下单操作" in bench.say("交易设置").text


def test_an_order_with_stops_attached_counts_as_an_opening_order():
    bench = Bench(trade_actions=("stops",))
    assert "“市价开仓和加仓”现在没有开放" in bench.say("做多 XAU 100张 止损 3860").text
    bench = Bench(trade_actions=("limit",))
    assert "确认 1111" in bench.say("限价做多 XAU 100张 3850 止损 3800").text
    assert "“市价开仓和加仓”现在没有开放" in bench.say("做多 XAU 100张").text
