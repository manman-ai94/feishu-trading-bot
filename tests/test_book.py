"""Every waiting order reaches the AI, whoever placed it, and the AI may read the account itself."""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal

from bot.book import Book, book_text, read_book
from bot.closes import Closed, history_text
from bot.models import InstrumentSpec, Ticker
from bot.okx_client import OkxNetworkError
from bot.tools import Toolbox

from .helpers import FakeOkx, make_cfg, make_position, make_state

XAU = "XAU-USDT-SWAP"
UTC = timezone.utc


class Gold(FakeOkx):
    def ticker(self, inst_id: str) -> Ticker:
        return Ticker(inst_id, 3880.0)

    def instrument_spec(self, inst_id: str) -> InstrumentSpec:
        return InstrumentSpec(XAU, "SWAP", Decimal("0.001"), Decimal(1), "XAU", "USDT", "linear", Decimal(1), Decimal(1), Decimal("0.1"))


MANUAL_LIMIT = {
    "instType": "SWAP", "instId": XAU, "ordId": "901", "clOrdId": "", "tag": "", "ordType": "limit", "side": "buy",
    "posSide": "net", "px": "3850", "sz": "5000", "accFillSz": "0", "reduceOnly": "false", "lever": "100",
    "cTime": "1800000000000", "attachAlgoOrds": [{"slTriggerPx": "3820", "slTriggerPxType": "last", "slOrdPx": "-1", "tpTriggerPx": "3950", "tpOrdPx": "-1"}],
}  # fmt: skip
BOT_STOP = {
    "instType": "SWAP", "instId": XAU, "algoId": "a1", "algoClOrdId": "ftb0001", "tag": "ftbot", "ordType": "conditional",
    "side": "sell", "posSide": "net", "sz": "", "closeFraction": "1", "slTriggerPx": "3816", "slTriggerPxType": "last",
    "slOrdPx": "-1", "cTime": "1800000000000",
}  # fmt: skip
MANUAL_TRIGGER = {
    "instType": "SWAP", "instId": XAU, "algoId": "a2", "algoClOrdId": "", "tag": "", "ordType": "trigger", "side": "buy",
    "posSide": "net", "sz": "2000", "triggerPx": "3905", "triggerPxType": "mark", "orderPx": "-1", "cTime": "1800000000000",
}  # fmt: skip
TRAILING = {
    "instType": "SWAP", "instId": XAU, "algoId": "a3", "ordType": "move_order_stop", "side": "sell", "posSide": "net",
    "sz": "", "closeFraction": "1", "callbackRatio": "0.005", "activePx": "3920", "cTime": "1800000000000",
}  # fmt: skip


def gold_book() -> Gold:
    okx = Gold()
    okx.open_orders = [MANUAL_LIMIT]
    okx.algos = {"conditional,oco": [BOT_STOP], "trigger": [MANUAL_TRIGGER], "move_order_stop": [TRAILING]}
    return okx


def test_every_kind_of_waiting_order_is_written_out_with_who_placed_it():
    okx = gold_book()
    text = book_text(read_book(okx), okx, 2000.0, UTC)
    assert "挂着的普通委托（1 张" in text
    limit = next(line for line in text.splitlines() if "901" in line or "限价单" in line)
    assert "XAU-USDT-SWAP 限价单 买入 5 XAU（5,000 张），价格 3,850.00，离现价 3,880.00 低 30.00（0.77%）" in limit
    assert "名义价值约 19,250.00，按 100 倍约占保证金 192.50" in limit
    assert "成交后带止损 3,820.00（最新价触发，市价）、止盈 3,950.00（市价）" in limit
    assert "成交后打到止损约亏 150.00，占权益 7.5%" in limit and "在 OKX 里手动下的" in limit
    assert "XAU-USDT-SWAP 止盈止损单 卖出 整个仓位：止损 3,816.00（最新价触发，市价）" in text
    assert "机器人下的" in text
    assert "计划委托 买入 2 XAU：标记价到 3,905.00 时按市价，离现价 3,880.00 高 25.00（0.64%）" in text
    assert "移动止盈止损 卖出 整个仓位：回撤 0.5% 触发，激活价 3,920.00" in text


def test_a_closing_order_carries_no_stop_cost_and_hedge_sides_are_named():
    okx = Gold()
    okx.open_orders = [dict(MANUAL_LIMIT, side="sell", posSide="long")]
    text = book_text(read_book(okx), okx, 2000.0, UTC)
    assert "卖出平多" in text and "打到止损" not in text
    okx.open_orders = [dict(MANUAL_LIMIT, reduceOnly="true")]
    assert "（只减仓）" in book_text(read_book(okx), okx, 2000.0, UTC)


def test_an_empty_book_and_an_unreadable_part_are_said_plainly():
    assert book_text(Book([], []), Gold(), 2000.0, UTC) == "挂着的委托：没有。"

    class Flaky(Gold):
        def pending_algos(self, ord_type):
            if ord_type == "trigger":
                raise OkxNetworkError("timeout")
            return super().pending_algos(ord_type)

    okx = Flaky()
    okx.open_orders = [MANUAL_LIMIT]
    book = read_book(okx)
    assert book.missing == ("计划委托",)
    assert "这次没读到：计划委托。不要猜它们" in book_text(book, okx, 2000.0, UTC)


def test_the_account_tools_are_offered_only_where_the_account_may_be_shown():
    box = Toolbox(Gold(), resolver=None, knowledge=None, tz=UTC)
    names = lambda **kw: [tool["name"] for tool in box.definitions(**kw)]  # noqa: E731
    assert "get_account" not in names(account=True)  # nothing to read it with yet
    box.attach_account(lambda: "账户权益 2,000.00 USD", lambda days: f"{days} 天的平仓")
    assert "get_account" not in names() and "get_trade_history" not in names()
    offered = names(account=True)
    assert offered.index("get_account") + 1 == offered.index("get_trade_history") < offered.index("web_search")
    assert box.run("get_account", {}, account=True) == ("账户权益 2,000.00 USD", False)
    assert box.run("get_trade_history", {"days": 400}, account=True) == ("90 天的平仓", False)
    assert box.run("get_trade_history", {}, account=True) == ("7 天的平仓", False)
    assert box.run("get_account", {}) == ("这次不能看交易员的账户。", True)  # a group that does not share it


def test_the_ai_gets_the_orders_with_the_account_and_can_read_both_again():
    from bot.service import Reporter

    from .helpers import FakeAnalyst, FakeFeishu

    okx = gold_book()
    okx.state = make_state(positions=[make_position()])
    reporter = Reporter(make_cfg(), FakeFeishu(), okx, FakeAnalyst(), journal=None)
    text = reporter.account_for_ai(fresh=True)
    assert text.startswith("账户权益") and "挂着的普通委托（1 张" in text and "计划委托" in text
    okx.closed = []
    assert "没有平仓记录" in reporter.history_for_ai(7)


def test_the_closes_read_newest_first_with_how_each_closed():
    rows = [
        Closed(XAU, "long", "2", 3844.8, 3901.2, 10000.0, 564.0, -7.7, -1.2, None, 555.1, "USDT", 1_800_000_000.0),
        Closed(XAU, "long", "2", 3800.0, 3790.0, 1000.0, -10.0, -0.8, None, None, -10.8, "USDT", 1_799_000_000.0),
    ]
    text = history_text(rows, [], UTC, 0)
    first, second = text.splitlines()[1:]
    assert first.startswith("- 01-15 08:00 XAU-USDT-SWAP 多单，平了 10,000 张")
    assert "平仓方式：平仓单（不是止盈止损单触发的）" in first and "已实现盈亏 +555.10 USDT" in first
    assert "已实现盈亏 -10.80 USDT" in second
    assert history_text(rows, [], UTC, 1_900_000_000.0).startswith("这段时间没有平仓记录")


def test_an_order_that_does_not_read_as_expected_is_given_raw_and_the_rest_still_show():
    okx = Gold()
    okx.open_orders = [dict(MANUAL_LIMIT, attachAlgoOrds="garbled", cTime="not a time"), MANUAL_LIMIT]

    class NoSpecs(Gold):
        def instrument_spec(self, inst_id):
            raise KeyError(inst_id)

    text = book_text(read_book(okx), NoSpecs(), 2000.0, UTC)
    assert "挂着的普通委托（2 张" in text and text.count("在 OKX 里手动下的") + text.count("OKX 原样") == 2
    assert "（5,000 张）" not in text and "5,000 张" in text  # without the contract size the size stays in contracts
