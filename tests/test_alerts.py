"""Price alerts: what the owner may say, and when an alert fires."""
from __future__ import annotations

import pytest

from bot.alerts import KEEP_DAYS, AlertWatcher, PriceAlerts, _levels
from bot.journal import Journal
from bot.models import Candle, Ticker
from bot.okx_client import OkxNetworkError

from .helpers import Clock

XAU = "XAU-USDT-SWAP"
BTC = "BTC-USDT-SWAP"
START = 1_800_000_000.0  # a whole minute


class Market:
    """OKX as the alerts see it: a last price and one-minute candles, both set by the test."""

    def __init__(self):
        self.last = {XAU: 3850.0, BTC: 65000.0}
        self.candles_by_inst: dict[str, list[Candle]] = {}
        self.bars: dict[tuple[str, str], list[Candle]] = {}  # candles of other lengths, for close alerts
        self.error: Exception | None = None
        self.reads = 0

    def ticker(self, inst_id):
        self.reads += 1
        if self.error:
            raise self.error
        return Ticker(inst_id, self.last[inst_id])

    def candles(self, inst_id, bar="1H", limit=200):
        if self.error:
            raise self.error
        if bar != "1m":
            return self.bars.get((inst_id, bar), [])[-limit:]
        return self.candles_by_inst.get(inst_id, [])[-limit:]


class Names:
    NAMES = {"xau": XAU, "黄金": XAU, "btc": BTC, "比特币": BTC}

    def find_in_text(self, text, limit=3):
        lowered = text.lower()
        found = sorted((lowered.find(name), inst) for name, inst in self.NAMES.items() if name in lowered)
        ordered = []
        for _, inst in found:
            if inst not in ordered:
                ordered.append(inst)
        return ordered[:limit]


class Bench:
    def __init__(self, shared=True):
        self.journal = Journal(":memory:")
        self.market = Market()
        self.clock = Clock(START)
        self.sent: list[tuple[str, str]] = []
        self.send_error: Exception | None = None
        self.alerts = PriceAlerts(self.journal, self.market, Names(), lambda: XAU, lambda: shared, self.clock)
        self.jobs: list[dict] = []
        self.watcher = AlertWatcher(self.journal, self.market, self._send, self.clock, act=self.jobs.append)

    def _send(self, title, markdown):
        if self.send_error:
            raise self.send_error
        self.sent.append((title, markdown))

    def say(self, text, chat="oc_chat"):
        return self.alerts.handle(text, chat)


@pytest.mark.parametrize(
    "text, levels",
    [
        ("跌破 3800 提醒我", [("below", 3800.0)]),
        ("黄金跌破3800提醒我", [("below", 3800.0)]),
        ("提醒我 XAU 涨到 3900", [("above", 3900.0)]),
        ("到 3850 叫我", [("", 3850.0)]),
        ("3800 提醒我", [("", 3800.0)]),
        ("价格到3790的时候通知我一下", [("", 3790.0)]),
        ("3800以下提醒我", [("below", 3800.0)]),
        ("跌到3800以下告诉我", [("below", 3800.0)]),
        ("跌破3800点提醒我", [("below", 3800.0)]),
        ("跌破3800或者涨破3900都提醒我", [("below", 3800.0), ("above", 3900.0)]),
        ("XAU-USDT-SWAP 到 4000 提醒我", [("", 4000.0)]),
        ("明天8点提醒我", []),
        ("到8点提醒我", []),
        ("跌到5%提醒我", []),
        ("提醒我止损3800", []),
        ("10分钟后提醒我", []),
    ],
)
def test_the_levels_a_request_names(text, levels):
    assert _levels(text) == levels


@pytest.mark.parametrize(
    "text",
    [
        "为什么跌破3800没提醒我",
        "刚才到3800你提醒我了吗",
        "提醒我不要追高",
        "跌破3800要提醒我注意什么",
        "黄金今天怎么看",
        "提醒一下",
        "跌破3800就平仓",
    ],
)
def test_talk_about_alerts_is_not_an_alert(text):
    bench = Bench()
    assert bench.say(text) is None
    assert bench.journal.alerts() == []


def test_setting_an_alert():
    bench = Bench()
    reply = bench.say("跌破 3,800 提醒我")
    assert "好，XAU 跌破 3,800 时提醒你（现价 3,850）" in reply.text
    assert "私聊和群里各发一条" in reply.text and "不会下单" in reply.text and "取消提醒 1" in reply.text
    [alert] = bench.journal.alerts()
    assert (alert["inst_id"], alert["direction"], alert["price"], alert["chat"]) == (XAU, "below", 3800.0, "oc_chat")
    assert "在私聊里告诉你" in Bench(shared=False).say("跌破 3800 提醒我").text


def test_the_direction_of_a_bare_level_comes_from_the_price():
    bench = Bench()
    bench.say("到 3900 叫我")
    bench.say("3800 提醒我")
    assert [(a["direction"], a["price"]) for a in bench.journal.alerts()] == [("above", 3900.0), ("below", 3800.0)]


def test_an_alert_on_another_instrument_and_two_at_once():
    bench = Bench()
    assert "BTC 跌破 60,000" in bench.say("BTC 跌破 60000 提醒我").text
    reply = bench.say("跌破3800或者涨破3900都提醒我")
    assert "设了 2 条" in reply.text and "#2 XAU 跌破 3,800" in reply.text and "#3 XAU 涨破 3,900" in reply.text
    assert "一个品种" in bench.say("黄金和比特币跌破 3800 都提醒我").text


def test_an_alert_already_crossed_or_far_off_is_not_set():
    bench = Bench()
    crossed = bench.say("跌破 3900 提醒我").text
    assert "已经在 3,900 下面了" in crossed and "涨到 3900 提醒我" in crossed
    assert "太远了" in bench.say("跌破 380 提醒我").text
    assert "现价就是" in bench.say("3850 提醒我").text
    assert bench.journal.alerts() == []


def test_without_a_price_only_a_stated_direction_is_set():
    bench = Bench()
    bench.market.error = OkxNetworkError("down")
    assert "分不清 3,800 在上面还是下面" in bench.say("3800 提醒我").text
    assert "现价暂时读不到" in bench.say("跌破 3800 提醒我").text
    assert len(bench.journal.alerts()) == 1


def test_listing_and_cancelling():
    bench = Bench()
    assert "现在没有价格提醒" in bench.say("提醒").text
    bench.say("跌破 3800 提醒我")
    bench.say("涨到 3900 提醒我")
    listing = bench.say("提醒")
    assert listing.title == "价格提醒（2 条）"
    assert "#1 XAU 跌破 3,800" in listing.text and "还差 50" in listing.text
    assert "要取消哪条" in bench.say("取消提醒").text
    assert "已取消：#2 XAU 涨破 3,900" in bench.say("取消提醒 2").text
    assert "没有找到这条提醒" in bench.say("取消提醒 7").text
    assert "已取消：#1 XAU 跌破 3,800" in bench.say("不用提醒了").text  # the only one left
    assert "现在没有价格提醒，不用取消" in bench.say("取消提醒 全部").text
    bench.say("跌破 3800 提醒我")
    assert "已取消：#3" in bench.say("取消 3800 的提醒").text  # by its price


def test_an_alert_fires_once_on_the_last_price():
    bench = Bench()
    bench.say("跌破 3800 提醒我")
    assert bench.watcher.check_once() == 0
    bench.market.last[XAU] = 3799.5
    bench.clock.advance(10)
    assert bench.watcher.check_once() == 1
    [(title, body)] = bench.sent
    assert title == "价格提醒：XAU 跌破 3,800"
    assert "最低到过 3,799.5，现价 3,799.5" in body and "#1" in body and "没有下任何单" in body
    assert bench.watcher.check_once() == 0  # once only
    [alert] = bench.journal.alerts("fired")
    assert alert["done_price"] == 3799.5


def test_a_wick_between_two_reads_fires_and_one_from_before_does_not():
    bench = Bench()
    bench.market.candles_by_inst[XAU] = [Candle(int(START * 1000), 3850, 3852, 3790, 3850)]  # before it was set
    bench.clock.advance(5)
    bench.say("跌破 3800 提醒我")
    assert bench.watcher.check_once() == 0
    later = int(START * 1000) + 60_000
    bench.market.candles_by_inst[XAU].append(Candle(later, 3850, 3851, 3798, 3849))
    bench.clock.advance(60)
    assert bench.watcher.check_once() == 1
    assert "最低到过 3,798，现价 3,850" in bench.sent[0][1]


def test_an_alert_that_could_not_be_sent_is_tried_again():
    bench = Bench()
    bench.say("涨到 3900 提醒我")
    bench.market.last[XAU] = 3901.0
    bench.send_error = RuntimeError("feishu down")
    assert bench.watcher.check_once() == 0
    assert len(bench.journal.alerts()) == 1
    bench.send_error = None
    assert bench.watcher.check_once() == 1


def test_no_reads_without_alerts_and_none_lost_when_okx_is_down():
    bench = Bench()
    bench.watcher.check_once()
    assert bench.market.reads == 0
    bench.say("跌破 3800 提醒我")
    bench.market.error = OkxNetworkError("down")
    assert bench.watcher.check_once() == 0
    assert len(bench.journal.alerts()) == 1


def test_an_old_alert_expires_and_the_owner_is_told():
    bench = Bench()
    bench.say("跌破 3800 提醒我")
    bench.clock.advance(KEEP_DAYS * 86400 + 1)
    bench.watcher.check_once()
    assert bench.journal.alerts() == [] and bench.journal.alerts("expired")
    assert bench.sent[0][0] == "价格提醒已过期"


def test_alerts_survive_a_restart():
    bench = Bench()
    bench.say("跌破 3800 提醒我")
    watcher = AlertWatcher(bench.journal, bench.market, bench._send, bench.clock)  # a new process, the same journal
    bench.market.last[XAU] = 3790.0
    assert watcher.check_once() == 1


# ------------------------------------------------------------------ on a candle's close, no twins, jobs

HOUR_MS = 3_600_000


def test_a_close_alert_waits_for_the_candle_to_close_beyond_the_level():
    bench = Bench()
    bench.market.last[XAU] = 3590.0
    bench.clock.advance(20 * 60)  # 20 minutes into the hour
    reply = bench.say("1小时收盘回到3605下方提醒我")
    assert reply.text.startswith("好，XAU 1小时收盘在 3,605 下方 时提醒你") and "按 1小时 K 线收盘价判断" in reply.text
    [alert] = bench.journal.alerts()
    assert (alert["bar"], alert["direction"], alert["price"]) == ("1H", "below", 3605.0)
    hour = int(START * 1000)
    bench.market.bars[(XAU, "1H")] = [Candle(hour - HOUR_MS, 3600, 3610, 3580, 3590), Candle(hour, 3590, 3600, 3585, 3592, confirmed=False)]
    assert bench.watcher.check_once() == 0  # the candle that closed before it was set does not count; this one is still open
    bench.market.bars[(XAU, "1H")] = [Candle(hour, 3590, 3612, 3585, 3608), Candle(hour + HOUR_MS, 3608, 3609, 3607, 3608, confirmed=False)]
    bench.clock.advance(41 * 60)
    assert bench.watcher.check_once() == 0  # closed at 3,608: above the level
    bench.market.bars[(XAU, "1H")].append(Candle(hour + HOUR_MS, 3608, 3609, 3588, 3591))
    bench.market.bars[(XAU, "1H")].pop(1)
    bench.clock.advance(60 * 60)
    assert bench.watcher.check_once() == 1
    title, body = bench.sent[0]
    assert title == "收盘提醒：XAU 1小时收盘在 3,605 下方" and "1小时 K 线收在 3,591" in body and "没有下任何单" in body


@pytest.mark.parametrize(
    "text, bar, direction, price",
    [
        ("4小时收在 3600 上方提醒我", "4H", "above", 3600.0),
        ("1H收盘跌破3605提醒我", "1H", "below", 3605.0),
        ("15分钟K线收盘站上 3620 通知我", "15m", "above", 3620.0),
        ("XAU 1小时线收盘涨破 3900 提醒我", "1H", "above", 3900.0),
    ],
)
def test_the_ways_to_name_a_candle_close(text, bar, direction, price):
    bench = Bench()
    bench.say(text)
    [alert] = bench.journal.alerts()
    assert (alert["bar"], alert["direction"], alert["price"]) == (bar, direction, price)


def test_the_same_alert_is_not_set_twice():
    bench = Bench()
    bench.say("跌破 3800 提醒我")
    reply = bench.say("黄金跌破3800提醒我")
    assert reply.text == "#1 XAU 跌破 3,800 已经设过了，不再重复设。" and len(bench.journal.alerts()) == 1
    assert "好，" in bench.say("1小时收盘跌破 3800 提醒我").text  # on a close it is another alert
    assert len(bench.journal.alerts()) == 2


def test_a_job_runs_once_when_its_time_or_price_comes_and_shows_in_the_list():
    bench = Bench()
    bench.journal.add_alert(XAU, "at", START + 600, said="XAU 3,588 那张挂单", ts=START, action="cancel", data="{}")
    bench.journal.add_alert(XAU, "below", 3800.0, said="XAU 3,588 那张挂单", ts=START, action="cancel", data="{}")
    listing = bench.say("提醒").text
    assert "#1 到 " in listing and "撤单：XAU 3,588 那张挂单" in listing and "#2 XAU 跌破 3,800 撤单：" in listing
    assert bench.watcher.check_once() == 0
    bench.clock.advance(600)
    assert bench.watcher.check_once() == 1 and [job["id"] for job in bench.jobs] == [1]
    bench.market.last[XAU] = 3799.0
    assert bench.watcher.check_once() == 1 and [job["id"] for job in bench.jobs] == [1, 2]
    assert bench.watcher.check_once() == 0 and bench.sent == []  # a job tells through its own runner
