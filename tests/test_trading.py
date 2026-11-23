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
from bot.times import set_zone
from bot.trading import PAUSED_SETTING, Command, CommandParser, Reply, TradeDesk, new_confirmation_code

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
        self.algos: list[dict] = []  # trigger orders waiting, as OKX sends them
        self.orders: dict[str, OrderInfo] = {}
        self.changes: list[tuple[str, dict]] = []
        self.failures: dict[str, list[Exception]] = {}  # raised instead of doing the thing
        self.lost: set[str] = set()  # done, and then the connection breaks
        self.lookup_failures: list[Exception] = []
        self.triggered: dict[str, list[dict]] = {}  # OKX's triggered TP/SL orders, by order type
        self.closed: list[dict] = []  # OKX's position history
        self.history_failures: list[Exception] = []
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

    def pending_algos(self, ord_type: str) -> list[dict]:
        return [row for row in self.algos if row.get("ordType") in ord_type.split(",")]

    def order(self, inst_id: str, ord_id: str = "", cl_ord_id: str = "") -> OrderInfo:
        if self.lookup_failures:
            raise self.lookup_failures.pop(0)
        for order in self.orders.values():
            if (ord_id and order.ord_id == ord_id) or (cl_ord_id and order.cl_ord_id == cl_ord_id):
                return order
        raise OkxError("51603", "Order does not exist")

    def leverage(self, inst_id: str, mgn_mode: str) -> float:
        return self.lever

    def algo_history(self, ord_type: str) -> list[dict]:
        if self.history_failures:
            raise self.history_failures.pop(0)
        return list(self.triggered.get(ord_type, []))

    def position_history(self) -> list[dict]:
        return list(self.closed)

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
            sl_trigger_px=float(order["sl_trigger_px"]) if order.get("sl_trigger_px") else None,
            tp_trigger_px=float(order["tp_trigger_px"]) if order.get("tp_trigger_px") else None,
        )  # fmt: skip
        self.orders[info.ord_id] = info
        if not market:
            self.pending.append(info)
        self._end("place_order")
        return info.ord_id

    def place_trigger(self, **order) -> str:
        self._begin("place_trigger", order)
        self._next += 1
        row = {"ordType": "trigger", "algoId": str(self._next), "instId": order["inst_id"], "side": order["side"],
               "posSide": order.get("pos_side") or "net", "sz": order["sz"], "triggerPx": order["trigger_px"], "orderPx": "-1",
               "algoClOrdId": order["cl_ord_id"]}
        if order.get("sl_trigger_px"):
            row["attachAlgoOrds"] = [{"slTriggerPx": order["sl_trigger_px"]}]
        self.algos.append(row)
        self._end("place_trigger")
        return row["algoId"]

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
        self.algos = [row for row in self.algos if row["algoId"] not in gone]
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
        ("做多 XAU 止损 3816", Command("open", XAU, "long", "market", None, "risk", sl=Decimal(3816))),
        ("限价做多 XAU 3850 止损 3820", Command("open", XAU, "long", "limit", None, "risk", Decimal(3850), sl=Decimal(3820))),
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


def test_a_command_sent_with_the_quotes_it_was_copied_with_is_read_the_same():
    read = parser()
    plain = read.parse("止损 XAU 3806")
    for quoted in ("“止损 XAU 3806”", '"止损 XAU 3806"', "「止损 XAU 3806」", "‘止损 XAU 3806’"):
        assert read.parse(quoted) == plain, quoted


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
        ("做多 BTC 1手", "只用在黄金上"),
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
    assert "没有设账户总风险上限（TRADE_MAX_TOTAL_RISK_PCT）" in settings
    capped = Bench(trade_max_total_risk_pct=25.0).say("交易设置").text
    assert "所有止损都打到合计最多亏权益的 25%，加上新单会超过就不执行（TRADE_MAX_TOTAL_RISK_PCT）" in capped


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
    assert "止损 3,860.0（离现价 0.52%，触发后大约亏 2.00 USDT，占权益 0.1%，没超过你定的单笔风险 1%）" in preview.text
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


def test_an_opening_preview_measures_its_stop_against_his_per_trade_rule():
    text = Bench(risk_per_trade_pct=0.05).say("做多 XAU 100张 止损 3860").text
    assert "触发后大约亏 2.00 USDT，占权益 0.1%，超过了你定的单笔风险 0.05%" in text


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
    assert "市价加空" in preview.text and "逐仓" in preview.text
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


def roomy(**cfg):
    """Room for a 25% risk on the 2,000 USDT test account: a position of about 10 XAU fits."""
    values = dict(risk_per_trade_pct=25.0, trade_max_order_usd=60000.0, trade_max_position_usd=80000.0)
    values.update(cfg)
    return values


def test_an_open_with_only_a_stop_is_sized_by_the_owners_risk():
    bench = Bench(**roomy())
    preview = bench.say("做多 XAU 止损 3830")
    assert "数量 10000 张（合 10 XAU），按现价 3,880.0 约 38,800.00 USDT（名义价值）" in preview.text
    assert "数量按你定的单笔风险算：权益 2,000.00 USD 的 25% 是 500.00 USDT，止损离现价 50，最多开 10000 张（合 10 XAU）" in preview.text
    assert "触发后大约亏 500.00 USDT，占权益 25.0%，没超过你定的单笔风险 25%" in preview.text
    bench.say("确认 1111")
    assert bench.sent[0][1]["sz"] == "10000" and bench.sent[0][1]["sl_trigger_px"] == "3830"


def test_the_fees_and_the_slippage_are_counted_in_the_size_and_the_loss():
    bench = Bench(**roomy(trade_fee_pct=0.05, trade_slippage_pct=0.02))
    preview = bench.say("做多 XAU 止损 3830")
    # Each XAU loses 50 to the stop, 3.88 in fees (0.05 % in and out) and 0.776 to slippage: 500 / 54.656 = 9.148 XAU.
    assert "止损离现价 50，加上开平手续费 3.88、滑点 0.776，每 1 XAU 最多亏 54.656，最多开 9148 张（合 9.148 XAU）" in preview.text
    assert "触发后大约亏 499.99 USDT，含手续费和滑点约 42.59（每 1 XAU：开平手续费 3.88、滑点 0.776）" in preview.text
    refused = Bench(**roomy(trade_fee_pct=0.05, trade_slippage_pct=0.02, trade_max_risk_pct=25.0)).say("做多 XAU 10个 止损 3830")
    assert "大约亏 546.56 USDT（含手续费和滑点）" in refused.text and "最多开 9148 张" in refused.text
    sizing = Bench(**roomy(trade_fee_pct=0.05, trade_slippage_pct=0.02)).say("算仓位 XAU 止损 3830")
    assert "加上开平手续费 3.88、滑点 0.776，每 1 XAU 亏 54.656 USDT" in sizing.text and "最多 9148 张" in sizing.text


def test_a_resting_limit_order_is_sized_from_its_own_price():
    bench = Bench(**roomy())
    preview = bench.say("限价做多 XAU 3850 止损 3800")
    assert "止损离限价 50，最多开 10000 张（合 10 XAU）" in preview.text
    bench.say("确认 1111")
    order = bench.sent[0][1]
    assert (order["ord_type"], order["px"], order["sz"], order["sl_trigger_px"]) == ("limit", "3850", "10000", "3800")


def test_a_risk_sized_open_over_a_limit_is_cut_to_the_limit():
    bench = Bench(**roomy())
    text = bench.say("做多 XAU 止损 3860").text  # 500 over 20 is 25 XAU, about 97,000 USDT
    assert "最多开 25000 张（合 25 XAU）" in text
    # 99% of 60,000 at 3,880 is 15.309 XAU: a hundredth spare for the price moving before the code
    assert "这超过了单笔上限 60,000，按上限减到 15309 张（合 15.309 XAU）" in text
    assert "数量 15309 张" in text and "确认 1111" in text
    assert bench.say("确认 1111").title.startswith("已执行")
    assert bench.sent[-1][1]["sz"] == "15309"


def test_a_risk_sized_open_is_cut_to_what_the_position_limit_leaves():
    bench = Bench(**roomy(trade_max_order_usd=200000.0))
    text = bench.say("做多 XAU 止损 3860").text
    assert "这超过了持仓上限 80,000 还剩的额度，按上限减到 20412 张" in text  # 99% of 80,000 at 3,880


def test_a_stated_size_over_a_limit_is_refused_with_the_most_it_may_be():
    bench = Bench(**roomy())
    text = bench.say("做多 XAU 20个 止损 3860").text
    assert "超过了单笔上限 60,000" in text and "这张单最多能开约 15463 张（合 15.463 XAU）" in text
    assert bench.sent == []


def test_a_risk_sized_open_says_how_it_was_sized_when_refused():
    bench = Bench(**roomy())
    assert "多单的止损要低于现价" in bench.say("做多 XAU 止损 3900").text
    assert "最小的 1 张也会亏约 0.05 USDT" in Bench(risk_per_trade_pct=0.0001).say("做多 XAU 止损 3830").text
    assert bench.sent == []


def test_a_cap_on_the_stop_refuses_a_bigger_open():
    bench = Bench(**roomy(trade_max_risk_pct=25.0))
    assert "TRADE_MAX_RISK_PCT=25），开仓必须带止损" in bench.say("做多 XAU 1个").text
    text = bench.say("做多 XAU 15个 止损 3830").text  # 750 USDT, 37.5% of 2,000
    assert "这张单止损触发大约亏 750.00 USDT，占权益 37.5%，超过了你设的单笔上限 25%" in text
    assert "按这个止损最多开 10000 张（合 10 XAU）" in text
    assert "确认" in bench.say("做多 XAU 10个 止损 3830").text
    assert "开仓的止损触发时最多亏权益的 25%" in bench.say("交易设置").text
    assert "没有设开仓止损的亏损上限" in Bench().say("交易设置").text
    assert bench.sent == []


def test_the_cap_allows_a_little_for_the_price_moving_before_the_code():
    bench = Bench(**roomy(trade_max_risk_pct=25.0))
    bench.say("做多 XAU 止损 3830")
    bench.exchange.last[XAU] = 3883.0  # 530 USDT now, 26.5%: within a tenth over the line
    assert bench.say("确认 1111").title.startswith("已执行")
    assert "最多开 9433 张" in bench.say("做多 XAU 止损 3830").text  # sized from 3,883 this time
    bench.exchange.last[XAU] = 3893.0  # 63 over 9.433 XAU is 594 USDT, 29.7%
    refusal = bench.say("确认 2222").text
    assert "价格在你确认前动了，按现价 3,893.0 算" in refusal and "占权益 29.7%，超过了你设的 25%" in refusal
    assert len(bench.exchange.named("place_order")) == 1


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
    assert "现在没有 XAU-USDT-SWAP 的仓位，没有可以平的" in bench.say("平仓 XAU").text
    assert "我不动它的仓位" in bench.say("平仓 ETH").text
    assert "现在没有可以平的仓位" in bench.say("全部平仓").text
    assert "现在没有仓位，没有可以平的" in bench.say("平仓").text  # no instrument named: no gap where it would be
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
    assert "要按这个数量开，发“限价做多 XAU 3873 止损 3768”" in reply.text
    assert "发“做多 XAU 止损 3768”" in bench.say("算仓位 XAU 止损 3768").text
    assert "要按这个数量开" not in bench.say("算仓位 XAU 入场 3900 止损 3768").text  # a limit there would fill at once
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


@pytest.mark.parametrize(
    "answer",
    ["取消 1111", "1111 不要执行", "别执行 1111", "不确认 1111", "1111取消", "算了 1111", "1111 先等等",
     "不确认", "确认取消", "2222 取消", "no 1111", "1111 wait", "１１１１ 停"],
)  # fmt: skip
def test_a_reply_with_the_code_and_a_no_in_it_voids_the_preview(answer):
    bench = Bench(FakeExchange([gold()]), trading="live", trade_actions=("stops", "close", "cancel"))
    bench.say("止损 XAU 3816")
    reply = bench.say(answer)
    assert reply.text.startswith("已作废") and "没有执行任何操作" in reply.text and "只回确认码" in reply.text
    assert bench.sent == []
    assert bench.say("1111") is None  # nothing waits any more: the code went with the preview
    assert bench.sent == []


def test_cancel_with_the_code_never_closes_the_position():
    bench = Bench(FakeExchange([gold()]), trading="live", trade_actions=("stops", "close", "cancel"))
    assert "确认 1111" in bench.say("平仓 XAU").text
    assert bench.say("取消 1111").text.startswith("已作废")
    assert bench.sent == []


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


def test_a_take_profit_on_the_losing_side_of_the_entry_is_called_an_early_stop():
    bench = Bench(FakeExchange([gold(stops=(StopOrder("a1", 3700.0, None),))]))
    preview = bench.say("止盈 XAU 3900")  # above the price, so OKX takes it; the entry is 3844.8: a gain
    assert "提前止损" not in preview.text
    bench = Bench(FakeExchange([gold(avg_px=3950.0, stops=(StopOrder("a1", 3700.0, None),))]))
    preview = bench.say("止盈 XAU 3900")
    assert "这笔交易 -500.00 USDT" in preview.text and "这个止盈在开仓均价亏钱的一边，打到时是亏着出场，其实是提前止损" in preview.text


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


def test_several_stops_on_one_position_are_merged_into_one_new_one_first():
    stops = (StopOrder("a1", 3700.0, 3950.0, whole=False, size=5000.0), StopOrder("a2", 3750.0, 3950.0, whole=False, size=5000.0))
    bench = Bench(FakeExchange([gold(stops=stops)]))
    preview = bench.say("止损 XAU 3860").text
    assert "合并止盈止损 XAU-USDT-SWAP 多单" in preview
    assert "现在有 2 张：止损 3,700.0、止盈 3,950.0（管 5 XAU）；止损 3,750.0、止盈 3,950.0（管 5 XAU）" in preview
    assert "止损 3,860.0（离现价" in preview and "止盈 3,950.0（离现价" in preview and "中间不会没有止损" in preview
    bench.say("确认 1111")
    assert bench.sent == [
        ("place_stop", {"inst_id": XAU, "td_mode": "cross", "side": "sell", "sl_trigger_px": "3860", "tp_trigger_px": "3950", "cl_ord_id": "ftb0001"}),
        ("cancel_stops", {"items": [(XAU, "a1"), (XAU, "a2")]}),
    ]


def test_merging_moves_a_whole_position_stop_and_takes_off_the_rest():
    stops = (StopOrder("w1", 3700.0, None), StopOrder("p1", 3750.0, None, whole=False, size=2000.0))
    bench = Bench(FakeExchange([gold(stops=stops)]))
    bench.run("止损 XAU 3860")
    assert bench.sent == [
        ("amend_stop", {"inst_id": XAU, "algo_id": "w1", "sl_trigger_px": "3860"}),
        ("cancel_stops", {"items": [(XAU, "p1")]}),
    ]


def test_merging_stops_at_different_prices_needs_the_new_stop_written():
    stops = (StopOrder("a1", 3700.0, None, whole=False, size=1.0), StopOrder("a2", 3750.0, None, whole=False, size=1.0))
    bench = Bench(FakeExchange([gold(stops=stops)]))
    assert "止损价不一样（3,700.0、3,750.0）。要合成一张，请一起写上止损：止损 XAU 价格 止盈 3950" in bench.say("止盈 XAU 3950").text
    bench.run("取消止盈止损 XAU")
    assert bench.sent == [("cancel_stops", {"items": [(XAU, "a1"), (XAU, "a2")]})]


def test_a_market_addition_leaves_one_stop_for_the_whole_position():
    bench = Bench(FakeExchange([small(stops=(StopOrder("p1", 3800.0, None, whole=False, size=200.0),))]))
    preview = bench.say("加仓 XAU 0.1个 止损 3850").text
    assert "市价加多 XAU-USDT-SWAP" in preview
    assert "合成一张止损：加仓成交后，整个仓位（原有 0.2 + 这次 0.1 XAU）都用止损 3,850.0，原来的止损（3,800.0）不再用" in preview
    bench.say("确认 1111")
    calls = [call for call, _ in bench.sent]
    assert calls == ["place_order", "place_stop", "cancel_stops"]
    assert "sl_trigger_px" not in bench.sent[0][1]  # the order itself carries no stop of its own
    assert bench.sent[1][1]["sl_trigger_px"] == "3850" and bench.sent[2][1] == {"items": [(XAU, "p1")]}


def test_a_limit_addition_keeps_its_own_stop_and_says_how_to_merge():
    bench = Bench(FakeExchange([small(stops=(StopOrder("w1", 3800.0, None),))]))
    preview = bench.say("限价加仓 XAU 0.1个 3870 止损 3850").text
    assert "要和原来的合成一张，成交后发“止损 XAU 3850”" in preview
    bench.say("确认 1111")
    assert [call for call, _ in bench.sent] == ["place_order"] and bench.sent[0][1]["sl_trigger_px"] == "3850"


def test_an_addition_that_cannot_put_the_whole_stop_on_says_so_loudly():
    exchange = FakeExchange([small(stops=(StopOrder("p1", 3800.0, None, whole=False, size=200.0),))])
    exchange.failures["place_stop"] = [OkxError("51000", "Parameter error")]
    bench = Bench(exchange)
    bench.say("加仓 XAU 0.1个 止损 3850")
    reply = bench.say("确认 1111").text
    assert "加仓单已经下了，但整仓止损没有全部做完" in reply
    assert [call for call, _ in bench.sent] == ["place_order", "place_stop"]  # the old stop stays on


def test_a_stop_that_would_trigger_at_once_is_refused():
    bench = Bench(FakeExchange([gold()]))
    assert "多单的止损要低于现价 3,880.0，你写的是 3,890.0" in bench.say("止损 XAU 3890").text
    assert "多单的止盈要高于现价" in bench.say("止盈 XAU 3870").text
    assert "现在没有 XAU-USDT-SWAP 的仓位" in Bench().say("止损 XAU 3860").text
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


def test_confirmation_codes_never_look_like_a_gold_price():
    codes = {new_confirmation_code() for _ in range(5000)}
    assert all(len(code) == 4 and code.isdigit() for code in codes)
    assert not any(3000 <= int(code) <= 4999 for code in codes)
    assert any(int(code) < 3000 for code in codes) and any(int(code) >= 5000 for code in codes)


def test_a_lot_of_gold_is_one_xau():
    command = parser().parse("做多 XAU 12手 止损 3768")
    assert (command.kind, command.unit, command.size) == ("open", "coins", Decimal(12))
    assert parser(instruments=(XAU,)).parse("平仓 2手").unit == "coins"  # only gold is traded: no instrument needed
    assert parser().parse("平仓 2手").kind == "unclear"  # gold or bitcoin: 手 means nothing on BTC
    bench = Bench(**roomy())
    assert "数量 12000 张（合 12 XAU）" in bench.say("限价做多 XAU 12手 3850 止损 3830").text


@pytest.mark.parametrize(
    "text, px, side, inst",
    [
        ("撤单 XAU 3788", Decimal(3788), "", XAU),
        ("撤单 3788", Decimal(3788), "", ""),
        ("撤单 XAU 挂在 3788 的单", Decimal(3788), "", XAU),
        ("撤单 买单", None, "buy", ""),
        ("撤单 XAU 卖单 3900", Decimal(3900), "sell", XAU),
    ],
)
def test_cancel_by_price_or_side_is_read(text, px, side, inst):
    command = parser().parse(text)
    assert (command.kind, command.px, command.direction, command.inst_id) == ("cancel", px, side, inst)


def test_cancelling_the_order_at_a_price():
    exchange = FakeExchange()
    resting(
        exchange,
        OrderInfo("590908157585625111", XAU, "buy", px=3788.0, sz=12000.0, state="live"),
        OrderInfo("590908157585625222", XAU, "buy", px=3755.0, sz=100.0, state="live"),
        OrderInfo("590908157585625333", XAU, "sell", px=3900.0, sz=100.0, state="live"),
    )
    bench = Bench(exchange)
    preview = bench.say("撤单 XAU 3755").text
    assert "撤单（1 张）" in preview and "590908157585625222" in preview and "625111" not in preview
    bench.say("确认 1111")
    assert bench.sent == [("cancel_order", {"inst_id": XAU, "ord_id": "590908157585625222"})]
    refusal = bench.say("撤单 XAU 3700").text
    assert "没有找到挂在 3700 的单" in refusal and "限价 3,788.0" in refusal
    assert "订单号 590908157585625333" in bench.say("撤单 卖单").text
    assert "590908157585625111" in bench.say("撤单 3788").text  # a short number is tried as a price too


# ------------------------------------------------------------------ where an order came from


def test_every_order_is_tagged_with_where_it_came_from():
    bench = Bench()
    bench.run("做多 XAU 0.1个 止损 3850")
    bench.say("算仓位 XAU 止损 3850")
    bench.run("做多 XAU 0.1个 止损 3850", code="2222")
    bench.clock.advance(31 * 60)
    bench.run("做多 XAU 0.1个 止损 3850", code="3333")  # the sizing was too long ago
    bench.desk.handle_understood("按刚才说的情景开多", "做多 XAU 0.1个 止损 3850")
    bench.say("4444")
    bench.desk.handle_understood("帮我买一个黄金，止损3850", "做多 XAU 0.1个 止损 3850")
    bench.say("5555")
    assert [row["source"] for row in bench.rows()] == ["own", "sized", "own", "plan", "words"]


def test_an_opening_preview_shows_how_orders_of_the_kind_have_done_once_there_are_enough():
    bench = Bench()
    for n, r in enumerate((1.5, -1.0, 2.0, -1.0)):
        bench.journal.record_review(inst_id=XAU, direction="long", closed_at=bench.clock() - 86400 * (n + 1), r_multiple=r, source="own")
    assert "这类单" not in bench.say("做多 XAU 0.1个 止损 3850").text
    bench.say("取消")
    bench.journal.record_review(inst_id=XAU, direction="long", closed_at=bench.clock() - 3600, r_multiple=0.5, source="own")
    bench.journal.record_review(inst_id=XAU, direction="long", closed_at=bench.clock() - 3600, r_multiple=-1.0, source="plan")
    assert "这类单（自己写的指令）最近 180 天平仓 5 笔，平均 +0.40R，赚 3 笔" in bench.say("做多 XAU 0.1个 止损 3850").text


# ------------------------------------------------------------------ total risk


def test_the_total_risk_counts_every_stop_and_a_risk_sized_open_is_cut_to_fit():
    exchange = FakeExchange([gold(stops=(StopOrder("w1", 3840.8, None),))])  # 10 XAU, 40 below: 408 at risk
    exchange.pending.append(OrderInfo("9", XAU, "buy", px=3800.0, sz=1000.0, state="live", sl_trigger_px=3780.0))  # 1 XAU, 20
    exchange.algos.append({"ordType": "trigger", "instId": XAU, "side": "buy", "posSide": "net", "sz": "1000", "triggerPx": "3900",
                           "orderPx": "-1", "attachAlgoOrds": [{"slTriggerPx": "3870"}]})  # 1 XAU, 30
    bench = Bench(exchange, **roomy(trade_max_total_risk_pct=25.0))
    preview = bench.say("限价做多 BTC 64000 止损 63000").text  # a new instrument: the cap is what is left of 500
    assert "账户现在所有止损都打到要亏约 450.00，占权益 22.5%，总风险上限 25% 还剩约 50.00，按它减到 5 张（合 0.05 BTC）" in preview
    assert "账户总风险（所有止损都打到）：现有约 450.00，加上这张约 50.00，合计约 500.00 USDT，占权益 25.0%" in preview


def test_a_stated_size_over_the_total_risk_is_refused_with_what_still_fits():
    exchange = FakeExchange([gold(stops=(StopOrder("w1", 3840.8, None),))])
    bench = Bench(exchange, **roomy(trade_max_total_risk_pct=25.0))
    refusal = bench.say("限价做多 BTC 0.2个 64000 止损 63000").text
    assert "加上这张会超过你定的账户总风险 25%（TRADE_MAX_TOTAL_RISK_PCT）。按这个止损最多还能开 10 张（合 0.1 BTC）" in refusal
    assert bench.sent == []


def test_an_open_alone_over_the_total_risk_is_measured_by_itself_not_by_a_zero():
    bench = Bench(**roomy(trade_max_total_risk_pct=25.0))
    refusal = bench.say("做多 XAU 15个 止损 3830").text  # 750 USDT at the stop, nothing else at risk
    assert "这张单打到止损要亏约 750.00 USDT，占权益 37.5%，超过了你定的账户总风险 25%（约 500.00 USDT" in refusal
    assert "要亏约 0.00" not in refusal and "按这个止损最多还能开 10000 张（合 10 XAU）。可以少开一点" in refusal


# ------------------------------------------------------------------ a refused open, mended in a word


def test_a_stop_sent_after_an_open_refused_for_lacking_one_completes_that_open():
    bench = Bench(**roomy(trade_max_risk_pct=25.0))
    refusal = bench.say("做多 XAU 1个").text
    assert "开仓必须带止损" in refusal and "补一句“止损 价格”（可以连止盈一起写），我就按这张单出预览" in refusal
    preview = bench.say("止损 3830 止盈 3950").text
    assert preview.startswith("接着刚才被拦下的那张开仓单，加上止损 3830、止盈 3950：")
    assert "数量 1000 张" in preview and "确认 1111" in preview
    assert bench.say("确认 1111").title.startswith("已执行")
    order = bench.exchange.named("place_order")[0]
    assert (order["sz"], order["sl_trigger_px"], order["tp_trigger_px"]) == ("1000", "3830", "3950")


def test_a_refused_open_is_mended_only_while_fresh_and_never_instead_of_a_held_position():
    bench = Bench(**roomy(trade_max_risk_pct=25.0))
    bench.say("做多 XAU 1个")
    bench.clock.advance(11 * 60)
    assert bench.say("止损 3830").text.startswith("现在没有仓位，没有可以设止盈止损的")  # too late: a stop, as ever
    held = Bench(FakeExchange([small()]), **roomy(trade_max_risk_pct=25.0))
    assert "开仓必须带止损" in held.say("做多 XAU 1个").text
    preview = held.say("止损 3830").text  # there is a position: the stop is for it
    assert "接着刚才" not in preview and "确认 1111" in preview
    other = Bench(**roomy(trade_max_risk_pct=25.0))
    other.say("做多 XAU 1个")
    other.say("做空 XAU 1个 止损 3990")  # a new open lets the refused one go, whatever comes of it
    other.say("取消")
    assert "接着刚才" not in other.say("止损 3830").text


def test_at_the_cap_reopens_an_open_refused_for_a_limit_with_what_fits():
    exchange = FakeExchange([gold(stops=(StopOrder("w1", 3840.8, None),))])
    bench = Bench(exchange, **roomy(trade_max_total_risk_pct=25.0))
    refusal = bench.say("限价做多 BTC 0.2个 64000 止损 63000").text
    assert refusal.endswith("回“按上限”，我就按 10 张重新出预览。10 分钟内有效。")
    preview = bench.say("就按上限开吧").text
    assert preview.startswith("按上限把数量改成 10 张，重新出预览：") and "确认 1111" in preview
    assert bench.say("按上限") is None  # used once; now it is just words, for the rest of the bot
    big = Bench(**roomy(trade_max_order_usd=5000.0))
    assert "回“按上限”，我就按 1288 张" in big.say("做多 XAU 2个 止损 3830").text  # 5,000 / 3,880 = 1.288 XAU
    assert "按上限把数量改成 1288 张" in big.say("按上限").text


def test_at_the_cap_means_nothing_without_a_refusal_or_with_other_words():
    bench = Bench(**roomy(trade_max_total_risk_pct=25.0))
    assert bench.say("按上限") is None
    bench.say("做多 XAU 15个 止损 3830")
    for words in ("按上限吗", "不按上限", "用最大杠杆", "按上限 5 张", "上限是多少"):
        assert bench.say(words) is None or "按上限把数量改成" not in bench.say(words).text, words
    assert bench.desk.is_command("按最多的开")


def test_an_order_the_ai_wrote_out_is_mended_the_same_way():
    bench = Bench(**roomy(trade_max_risk_pct=25.0))
    first = bench.desk.handle_understood("帮我买一个黄金", "做多 XAU 1个")
    assert "补一句“止损 价格”" in first.text
    assert bench.say("止损 3830").text.startswith("接着刚才被拦下的那张开仓单，加上止损 3830：")


# ------------------------------------------------------------------ two orders in one sentence


def test_two_steps_in_one_sentence_are_previewed_one_after_the_other():
    bench = Bench(FakeExchange([gold()]), **roomy())
    first = bench.desk.handle_understood("减仓减5 xau 然后止损设在3730", "减仓 XAU 5个\n止损 XAU 3730")
    assert first.text.startswith("我把“减仓减5 xau 然后止损设在3730”理解为：**减仓 XAU 5个**，然后 **止损 XAU 3730**")
    assert "这张执行完，我接着出第二张预览：**止损 XAU 3730**，同样要你回确认码。" in first.text
    done = bench.say("确认 1111")
    assert done.title.startswith("已执行") and "**第二步：止损 XAU 3730**" in done.text and "确认 2222" in done.text
    assert bench.say("确认 2222").title.startswith("已执行")
    assert [name for name, _ in bench.sent][-1] in ("place_stop", "amend_stop")


def test_a_stop_price_a_partial_close_left_out_becomes_the_second_step():
    bench = Bench(FakeExchange([gold()]), **roomy())
    first = bench.desk.handle_understood("减仓减5 xau 然后止损设在3730", "减仓 XAU 5个")  # the AI wrote one line
    assert "我接着出第二张预览：**止损 XAU 3730**" in first.text
    assert "**第二步：止损 XAU 3730**" in bench.say("确认 1111").text
    whole = Bench(FakeExchange([gold()]), **roomy())
    assert "第二张" not in whole.desk.handle_understood("全平了，止损 3730 不用了", "平仓 XAU").text  # nothing left to stop


def test_a_second_step_goes_with_the_first_when_that_is_called_off_or_refused():
    bench = Bench(FakeExchange([gold()]), **roomy())
    bench.desk.handle_understood("减仓减5 xau 然后止损设在3730", "减仓 XAU 5个\n止损 XAU 3730")
    bench.say("取消")
    assert bench.say("确认 1111").text == "现在没有等待确认的指令。" and bench.sent == []
    empty = Bench(**roomy())
    refused = empty.desk.handle_understood("减仓 5 个然后止损 3730", "减仓 XAU 5个\n止损 XAU 3730").text
    assert "第二步“止损 XAU 3730”也没有做" in refused


# ------------------------------------------------------------------ cancellations set for later


def waiting_orders(*orders):
    exchange = FakeExchange()
    for n, px in enumerate(orders or (3800.0,), 9):
        exchange.pending.append(OrderInfo(str(n), XAU, "buy", px=px, sz=1000.0, state="live"))
    return exchange


def test_a_cancel_set_for_a_time_is_confirmed_once_and_run_by_the_bot_when_due():
    bench = Bench(waiting_orders())
    preview = bench.say("周五 13:30 撤单 XAU")
    assert preview.title == "定时撤单预览 · 模拟盘"
    assert "**到 洛杉矶 01-15 周五 13:30（北京 01-16 周六 05:30）撤单（1 张）**" in preview.text
    assert "那一刻不再问你" in preview.text and "不会下任何新单" in preview.text and "确认 1111" in preview.text
    done = bench.say("确认 1111")
    assert done.title == "定时撤单已设好" and "取消提醒 1" in done.text and bench.sent == []
    [job] = bench.journal.alerts()
    assert (job["direction"], job["price"], job["action"]) == ("at", 1_800_048_600.0, "cancel")
    ran = bench.desk.run_job(job)
    assert ran.title.startswith("定时撤单 #1：已执行") and [name for name, _ in bench.sent] == ["cancel_order"]


def test_the_alert_watcher_runs_a_scheduled_cancel_once_when_it_is_due():
    from bot.alerts import AlertWatcher

    bench = Bench(waiting_orders())
    bench.say("30分钟后撤单 XAU")
    bench.say("确认 1111")
    told = []
    watcher = AlertWatcher(bench.journal, bench.exchange, lambda title, text: None, bench.clock, act=lambda job: told.append(bench.desk.run_job(job)))
    assert watcher.check_once() == 0 and bench.sent == []
    bench.clock.advance(30 * 60)
    assert watcher.check_once() == 1 and [name for name, _ in bench.sent] == ["cancel_order"]
    assert watcher.check_once() == 0 and len(told) == 1 and told[0].title.startswith("定时撤单 #1：已执行")


def test_the_ways_to_say_when():
    bench = Bench(waiting_orders())
    bench.say("30分钟后撤单 XAU")
    bench.say("确认 1111")
    bench.desk.handle("北京时间 周五 13:30 撤单 XAU", sender="ou_a")  # already past in Beijing: next Friday
    bench.desk.handle("确认 2222", sender="ou_a")
    set_zone(bench.journal, "ou_b", "Asia/Shanghai")  # "我用北京时间"
    reply = bench.desk.handle("明天下午3点撤单 XAU", sender="ou_b")
    assert "到 北京 01-16 周六 15:00（洛杉矶 01-15 周五 23:00）" in reply.text
    bench.desk.handle("确认 3333", sender="ou_b")
    assert [job["price"] for job in bench.journal.alerts()] == [1_800_001_800.0, 1_800_595_800.0, 1_800_082_800.0]


def test_a_cancel_set_on_a_price_waits_for_the_price():
    bench = Bench(waiting_orders(3800.0, 3790.0))
    preview = bench.say("跌破 3530 撤单 XAU 3800")
    assert "**XAU 跌破 3530 时撤单（1 张）**" in preview.text
    bench.say("确认 1111")
    [job] = bench.journal.alerts()
    assert (job["inst_id"], job["direction"], job["price"], job["action"]) == (XAU, "below", 3530.0, "cancel")
    bench.exchange.pending[:] = [order for order in bench.exchange.pending if order.ord_id != "9"]  # it filled meanwhile
    ran = bench.desk.run_job(job)
    assert ran.title == "定时撤单 #1：没有执行" and "已经不在了" in ran.text and bench.sent == []


def test_only_cancellations_are_set_for_later_and_a_time_gone_by_is_refused():
    bench = Bench(waiting_orders())
    assert "定时" not in (bench.say("周五 13:30 平仓 XAU") or Reply("")).text
    assert "读不准（或者已经过了）" in bench.say("今天 0点 撤单 XAU").text
    assert "只写一个" in bench.say("周五 13:30 跌破 3530 撤单 XAU").text
    assert "1 分钟以后、30 天以内" in bench.say("0.5分钟后撤单 XAU").text
    assert bench.say("撤单 XAU 3800").title.startswith("下单预览")  # no time, no price: now, as ever
    assert bench.journal.alerts() == []


def test_what_cannot_be_measured_is_named_and_a_bare_position_counts_to_its_liquidation():
    exchange = FakeExchange([gold()])  # no stop: counted to its liquidation price 3,713.07
    exchange.pending.append(OrderInfo("9", XAU, "buy", px=3800.0, sz=1000.0, state="live"))
    bench = Bench(exchange, **roomy())
    preview = bench.say("限价做多 BTC 0.01个 64000 止损 63000").text
    assert "现有约 1,677.30" in preview  # (3,880.8 - 3,713.07) x 10
    assert "没算进去（算不出）：XAU-USDT-SWAP 挂单买 3,800（1），没带止损" in preview


def test_a_rough_liquidation_price_and_a_stop_beyond_it():
    bench = Bench(**roomy())
    preview = bench.say("做多 XAU 10个 止损 3600").text
    assert "粗估强平价约 3,680.0（全仓，按账户权益亏光算；实际强平会早一点，以 OKX 显示为准）" in preview
    assert "止损在粗估强平价外面" in preview


# ------------------------------------------------------------------ breakout orders


@pytest.mark.parametrize(
    "text, direction, trigger, sl",
    [
        ("涨破 3900 做多 XAU 止损 3870", "long", Decimal(3900), Decimal(3870)),
        ("突破3900做多XAU 止损3870", "long", Decimal(3900), Decimal(3870)),
        ("站上 3900 就做多 黄金 1个 止损 3870", "long", Decimal(3900), Decimal(3870)),
        ("跌破 3800 做空 XAU 止损 3830", "short", Decimal(3800), Decimal(3830)),
        ("等价格跌破3800再做空 XAU 止损 3830 止盈 3700", "short", Decimal(3800), Decimal(3830)),
    ],
)
def test_a_breakout_is_read_as_a_trigger_order(text, direction, trigger, sl):
    command = parser().parse(text)
    assert (command.kind, command.ord_type, command.direction, command.px, command.sl) == ("open", "trigger", direction, trigger, sl)


def test_a_breakout_the_wrong_way_round_is_explained():
    command = parser().parse("跌破 3800 做多 XAU 止损 3780")
    assert command.kind == "unclear" and "不是突破单" in command.note and "限价做多 品种 3800 止损 价格" in command.note
    assert parser().parse("涨破 3900 限价做多 XAU 止损 3870").note.startswith("突破单到了触发价就按市价成交")


def test_a_breakout_order_is_previewed_sized_from_the_trigger_and_sent_as_a_trigger_order():
    bench = Bench(**roomy())
    preview = bench.say("涨破 3900 做多 XAU 止损 3870").text
    assert "**突破做多 XAU-USDT-SWAP（涨破 3900）**" in preview
    assert "止损离触发价 30，最多开" in preview
    assert "突破单（计划委托）：最新价涨到 3,900.0（离现价 3,880.0 0.52%）时按市价做多" in preview
    reply = bench.say("确认 1111").text
    [(call, args)] = bench.sent
    assert call == "place_trigger" and args["trigger_px"] == "3900" and args["side"] == "buy" and args["sl_trigger_px"] == "3870"
    assert "已挂上突破单：价格涨到 3,900.0 时按市价做多，成交后带止损 3,870.0" in reply
    listing = bench.say("挂单").text
    assert "XAU-USDT-SWAP 突破单做多 触发价 3,900.0，成交后带止损 3,870.0" in listing and "计划委托号 7001" in listing
    bench.run("撤单 XAU 3900", code="2222")
    assert bench.sent[-1] == ("cancel_stops", {"items": [(XAU, "7001")]})
    assert bench.exchange.algos == []


def test_a_breakout_trigger_already_passed_is_refused():
    bench = Bench(**roomy())
    assert "突破做多的触发价要高于现价 3,880.0，你写的是 3,870.0" in bench.say("涨破 3870 做多 XAU 止损 3850").text
    assert bench.sent == []


def test_a_waiting_breakout_counts_in_the_position_limit_and_the_total_risk():
    exchange = FakeExchange()
    exchange.algos.append({"ordType": "trigger", "algoId": "5", "instId": XAU, "side": "buy", "posSide": "net", "sz": "10000",
                           "triggerPx": "3900", "orderPx": "-1", "attachAlgoOrds": [{"slTriggerPx": "3870"}]})
    bench = Bench(exchange, **roomy(trade_max_position_usd=40000.0))
    refusal = bench.say("限价做多 XAU 1个 3850 止损 3820").text
    assert "挂单 39,000.00" in refusal and "超过了单个品种的持仓上限 40,000" in refusal


# ------------------------------------------------------------------ ladders


@pytest.mark.parametrize(
    "text, tiers, size, unit, sl",
    [
        ("分档做多 XAU 3850 3830 3810 止损 3790", (3850, 3830, 3810), None, "risk", 3790),
        ("分3档做多 XAU 3850到3810 止损 3790", (3850, 3830, 3810), None, "risk", 3790),
        ("分 2 档做空 黄金 3900-3920 止损 3950", (3900, 3920), None, "risk", 3950),
        ("分档做多 XAU 3个 3850 3830 3810 止损 3790", (3850, 3830, 3810), Decimal(3), "coins", 3790),
        ("限价做多 XAU 3850 3830 止损 3800", (3850, 3830), None, "risk", 3800),
    ],
)
def test_a_ladder_is_read_with_its_prices(text, tiers, size, unit, sl):
    command = parser().parse(text)
    assert command.kind == "open" and command.ord_type == "limit", command
    assert command.tiers == tuple(Decimal(t) for t in tiers) and command.px is None
    assert (command.size, command.unit, command.sl) == (size, unit, Decimal(sl))


@pytest.mark.parametrize(
    "text, hint",
    [
        ("分档做多 XAU 3850 3830 3810", "分档挂单要带止损"),
        ("分档做多 XAU 3850 止损 3790", "要写两个以上的价格"),
        ("分6档做多 XAU 3850到3800 止损 3790", "最多分 5 档"),
        ("分档做多 XAU 3850 3850 止损 3790", "价格要各不相同"),
        ("分3档做多 XAU 3850 3830 止损 3790", "说了分 3 档，写的是 2 个价格"),
        ("做多 XAU 3850到3810 止损 3790", "要说分几档"),
    ],
)
def test_a_ladder_that_is_not_complete_is_explained(text, hint):
    command = parser().parse(text)
    assert command.kind == "unclear" and hint in command.note, command


def test_a_ladder_shares_the_per_trade_risk_and_goes_out_as_one_limit_order_per_tier():
    bench = Bench(**roomy())
    preview = bench.say("分档做多 XAU 3850 3830 3810 止损 3790").text
    # 500 USDT over 60 + 40 + 20 = 120 per XAU in every tier: 4.166 XAU a tier
    assert "**分档限价做多 XAU-USDT-SWAP（3 档）**" in preview
    assert "3 档限价多单，每档 4166 张（合 4.166 XAU），合计 12498 张（合 12.498 XAU），约 47,867.34 USDT（名义价值），全部成交的均价 3,830.0" in preview
    assert "3 档全部成交再打到止损才亏这么多，每档 4166 张（合 4.166 XAU）" in preview
    assert "第 1 档 限价 3,850.0：4166 张（合 4.166 XAU），打到止损亏约 249.96 USDT" in preview
    assert "第 3 档 限价 3,810.0：4166 张（合 4.166 XAU），打到止损亏约 83.32 USDT" in preview
    assert "全部成交再打到止损，合计大约亏 499.92 USDT，占权益 25.0%，没超过你定的单笔风险 25%" in preview
    assert "几档都成交后想合成一张整仓止损，发“止损 XAU 3790”" in preview
    bench.exchange.last[XAU] = 3875.0  # the sizes stay as previewed
    reply = bench.say("确认 1111")
    assert reply.title.startswith("已执行")
    sent = [(args["ord_type"], args["px"], args["sz"], args["sl_trigger_px"]) for call, args in bench.sent if call == "place_order"]
    assert sent == [("limit", "3850", "4166", "3790"), ("limit", "3830", "4166", "3790"), ("limit", "3810", "4166", "3790")]
    assert "第 1 档 3850：" in reply.text and "第 3 档 3810：" in reply.text
    assert len(bench.exchange.pending) == 3
    [row] = bench.rows()
    assert row["action"] == "open" and row["result"] == "ok" and row["summary"].startswith("分档限价做多")


def test_a_short_ladder_rests_above_the_market_nearest_first():
    bench = Bench(**roomy())
    preview = bench.say("分3档做空 XAU 3940到3900 止损 3960").text
    assert "第 1 档 限价 3,900.0" in preview and "第 3 档 限价 3,940.0" in preview
    bench.say("确认 1111")
    assert [args["px"] for _, args in bench.sent] == ["3900", "3920", "3940"]
    assert all(args["side"] == "sell" for _, args in bench.sent)


def test_a_stated_ladder_size_is_split_evenly():
    bench = Bench(**roomy())
    preview = bench.say("分档做多 XAU 10个 3850 3830 3810 止损 3790").text
    assert "一共 10 XAU 平分成 3 档，按最小单位取整后每档 3333 张（合 3.333 XAU）" in preview
    assert "合计大约亏 399.96 USDT" in preview


def test_a_ladder_with_a_tier_through_the_market_or_a_stop_inside_it_is_refused():
    bench = Bench(**roomy())
    assert "有一档 3,950.0 比现价 3,880.0 高了 1.80%" in bench.say("分档做多 XAU 3950 3850 止损 3800").text
    assert "多单的止损要低于最远一档 3,830.0" in bench.say("分档做多 XAU 3850 3830 止损 3840").text
    assert "多单的止盈要高于最近一档 3,850.0" in bench.say("分档做多 XAU 3850 3830 止损 3800 止盈 3840").text
    assert bench.sent == []


def test_a_ladder_is_cut_to_the_position_limit_and_the_total_risk():
    exchange = FakeExchange([gold(stops=(StopOrder("w1", 3840.8, None),))])  # 10 XAU, 400 at risk
    bench = Bench(exchange, **roomy(trade_max_total_risk_pct=25.0))
    preview = bench.say("分档做多 XAU 3850 3830 3810 止损 3790").text
    assert "**分档限价加多 XAU-USDT-SWAP（3 档）**" in preview
    # 80,000 less the 38,808 held, 99% of it, over 11.49 XAU of price: 3,549 a tier
    assert "这超过了持仓上限 80,000 还剩的额度，减到每档 3549 张（合 3.549 XAU）" in preview
    assert "这超过了账户总风险上限 25% 还剩的额度，减到每档 833 张（合 0.833 XAU）" in preview
    assert "账户总风险（所有止损都打到）：现有约 400.00，加上这张约 99.96，合计约 499.96 USDT，占权益 25.0%" in preview


def test_a_stated_ladder_over_the_total_risk_is_refused():
    exchange = FakeExchange([gold(stops=(StopOrder("w1", 3840.8, None),))])
    bench = Bench(exchange, **roomy(trade_max_total_risk_pct=25.0))
    refusal = bench.say("分档做多 XAU 3个 3850 3830 3810 止损 3790").text
    assert "合起来超过你定的账户总风险 25%（TRADE_MAX_TOTAL_RISK_PCT）。按这个止损每档最多 833 张（合 0.833 XAU）" in refusal
    assert bench.sent == []


def test_a_tier_okx_refuses_does_not_stop_the_others():
    bench = Bench(**roomy())
    bench.exchange.failures["place_order"] = [OkxError("51008", "Insufficient margin")]
    bench.say("分档做多 XAU 3850 3830 3810 止损 3790")
    reply = bench.say("确认 1111")
    assert reply.title.startswith("只执行了一部分")
    assert "第 1 档 3850：OKX 拒绝了，Insufficient margin（代码 51008）" in reply.text
    assert len(bench.exchange.pending) == 2
    assert "整仓止损没有全部做完" not in reply.text
