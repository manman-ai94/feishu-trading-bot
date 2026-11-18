"""Half an hour before a US data release: one card when there is something on the account it could move."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot import econ
from bot.econ import ET, ReleaseWatcher, due, last_day, releases
from bot.journal import Journal
from bot.models import Candle, OrderInfo, StopOrder

from .helpers import make_position, make_state

LA = ZoneInfo("America/Los_Angeles")
XAU = "XAU-USDT-SWAP"
CPI = datetime(2026, 12, 10, 8, 30, tzinfo=ET).timestamp()


class Okx:
    def __init__(self):
        self.orders: list[OrderInfo] = []
        self.candle_calls = 0

    def pending_orders(self, inst_id=None):
        return list(self.orders)

    def candles(self, inst_id, bar="1H", limit=200):
        self.candle_calls += 1
        assert bar == "1H"
        return [Candle(i * 3_600_000, 3850, 3860, 3840, 3850) for i in range(limit)]  # every hour 20 wide


class Bench:
    def __init__(self, positions=(), now=CPI - 29 * 60):
        self.journal = Journal(":memory:")
        self.okx = Okx()
        self.state = make_state(list(positions))
        self.now = now
        self.sent: list[tuple[str, str]] = []
        self.wishes: list[str] = []
        self.watcher = ReleaseWatcher(
            self.journal, self.okx, lambda: self.state, lambda t, m: self.sent.append((t, m)), LA,
            clock=lambda: self.now, note_wish=self.wishes.append,
        )  # fmt: skip


def gold(stop=3820.0, direction="long"):
    return make_position(inst_id=XAU, direction=direction, mark_px=3850.0, coins=2.0, coin_ccy="XAU", stops=(StopOrder("a1", stop),))


def test_the_calendar_is_in_eastern_time():
    found = {r.key: r for r in releases()}
    assert found["cpi-2026-12-10"].at == CPI
    assert found["fomc-2026-12-09"].et() == "14:00" and found["nfp-2027-07-02"].et() == "08:30"
    assert datetime.fromtimestamp(found["nfp-2027-03-05"].at, tz=ET).utcoffset().total_seconds() == -5 * 3600
    assert datetime.fromtimestamp(found["cpi-2027-04-13"].at, tz=ET).utcoffset().total_seconds() == -4 * 3600  # summer time
    assert [r.key for r in due(CPI - 10 * 60)] == ["cpi-2026-12-10"]
    assert due(CPI - 31 * 60) == [] and due(CPI) == []
    assert last_day().isoformat() == "2027-12-10"


def test_a_position_gets_one_card_with_its_stop_in_hours_of_movement():
    bench = Bench([gold()])
    assert bench.watcher.check_once() == 1
    [(title, body)] = bench.sent
    assert title == "数据公布前提醒：美国 CPI（通胀）"
    assert body.startswith("美国 CPI（通胀）在 12-10 05:30 公布（美东 08:30），还有约 29 分钟。")
    assert "XAU-USDT-SWAP 多单 2 XAU，标记价 3,850，止损 3,820（离标记价 30，约 1.5 个 1 小时 ATR）" in body
    assert body.endswith("只是提醒，我没有动任何单。")
    bench.now += 60
    assert bench.watcher.check_once() == 0  # once per release


def test_no_stop_and_waiting_orders_are_named():
    bench = Bench([make_position(inst_id=XAU, direction="short", mark_px=3850.0, coins=1.0, coin_ccy="XAU", stops=())])
    bench.okx.orders = [OrderInfo("1", XAU, "buy", px=3800.0, sz=1000.0, state="live")]
    bench.watcher.check_once()
    body = bench.sent[0][1]
    assert "XAU-USDT-SWAP 空单 1 XAU，标记价 3,850，**没有止损**" in body
    assert "**等成交的挂单**\n- XAU-USDT-SWAP 限价买 3,800（1,000 张）" in body


def test_nothing_on_the_account_means_no_card_until_there_is():
    bench = Bench()
    assert bench.watcher.check_once() == 0 and bench.okx.candle_calls == 0
    bench.okx.orders = [OrderInfo("1", XAU, "buy", px=3800.0, sz=1.0, state="live")]
    bench.now += 60
    assert bench.watcher.check_once() == 1
    assert "**持仓**" not in bench.sent[0][1]


def test_outside_the_half_hour_nothing_is_sent():
    bench = Bench([gold()], now=CPI - 45 * 60)
    assert bench.watcher.check_once() == 0
    bench.now = CPI + 60
    assert bench.watcher.check_once() == 0


def test_a_calendar_about_to_run_out_is_put_on_the_requests_once():
    bench = Bench(now=datetime(2027, 11, 20, tzinfo=ET).timestamp())
    bench.watcher.check_once()
    bench.watcher.check_once()
    assert bench.wishes == ["数据公布日历只排到 2027-12-10，要补上之后的 CPI、非农和议息日期"]
    early = Bench(now=datetime(2027, 10, 1, tzinfo=ET).timestamp())
    early.watcher.check_once()
    assert early.wishes == []
    assert econ.STALE_DAYS == 30
