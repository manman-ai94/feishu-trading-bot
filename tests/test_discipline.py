"""The day's plan, the checklist and when the day stops (discipline.py), and the desk refusing what breaks them."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from bot.discipline import (
    Discipline, InstrumentPlan, day_bounds, day_figures, extract_plan, plan_due, trading_day,
)  # fmt: skip
from bot.closes import read_closed
from bot.journal import Journal
from bot.models import Candle, StopOrder
from tests.helpers import Clock, make_cfg, make_state
from tests.test_trading import XAU, Bench, FakeExchange, small

NY = ZoneInfo("America/New_York")
TUESDAY_10AM = datetime(2027, 1, 12, 10, 0, tzinfo=NY).timestamp()  # trading day Tuesday 01-12
LONG_PLAN = (InstrumentPlan(XAU, "long", (3860.0, 3840.0), (3920.0,)),)


def closed_row(opened: float, closed: float, pnl: float, close_type: str = "2", direction: str = "long") -> dict:
    return {
        "instId": XAU, "direction": direction, "type": close_type, "openAvgPx": "3880", "closeAvgPx": "3870",
        "closeTotalPos": "100", "pnl": str(pnl), "fee": "-0.5", "fundingFee": "0", "liqPenalty": "0",
        "realizedPnl": str(pnl - 0.5), "ccy": "USDT", "cTime": str(int(opened * 1000)), "uTime": str(int(closed * 1000)),
    }  # fmt: skip


class Market(FakeExchange):
    """The trading tests' exchange, with candles: a steady climb unless told otherwise."""

    def __init__(self, *args, step: float = 1.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.step = step
        self.candle_reads: list[str] = []

    def candles(self, inst_id: str, bar: str = "1H", limit: int = 200) -> list[Candle]:
        self.candle_reads.append(bar)
        last = self.last[inst_id]
        count = min(limit, 120)
        rows = []
        for i in range(count):
            close = last - (count - 1 - i) * self.step
            rows.append(Candle(i * 3_600_000, close - 0.5, close + 4.0, close - 4.0, close))
        return rows


def discipline(exchange=None, clock=None, journal=None, **cfg) -> tuple[Discipline, Journal, Clock, Market]:
    exchange = exchange or Market()
    journal = journal or Journal(":memory:")
    clock = clock or Clock(TUESDAY_10AM)
    values = dict(trade_instruments=(XAU,))
    values.update(cfg)
    return Discipline(make_cfg(**values), journal, exchange, clock=clock), journal, clock, exchange


# ------------------------------------------------------------------ the trading day


def test_the_trading_day_turns_at_five_in_new_york_in_winter_and_summer():
    assert trading_day(datetime(2027, 1, 12, 16, 59, tzinfo=NY).timestamp()) == date(2027, 1, 12)
    assert trading_day(datetime(2027, 1, 12, 17, 0, tzinfo=NY).timestamp()) == date(2027, 1, 13)
    start, end = day_bounds(date(2027, 1, 13))
    assert datetime.fromtimestamp(start, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M") == "01-13 06:00"
    assert end - start == 86400
    start, _ = day_bounds(date(2027, 6, 15))  # summer: an hour earlier in Beijing
    assert datetime.fromtimestamp(start, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M") == "06-15 05:00"


def test_plans_are_written_sunday_to_thursday_evening_for_monday_to_friday():
    sunday = datetime(2027, 1, 10, 17, 5, tzinfo=NY).timestamp()
    assert plan_due(sunday) == date(2027, 1, 11)
    assert plan_due(datetime(2027, 1, 14, 17, 5, tzinfo=NY).timestamp()) == date(2027, 1, 15)  # Thursday
    assert plan_due(datetime(2027, 1, 15, 17, 5, tzinfo=NY).timestamp()) is None  # Friday: the weekend has no plan
    assert plan_due(datetime(2027, 1, 16, 17, 5, tzinfo=NY).timestamp()) is None


# ------------------------------------------------------------------- the plan block


def test_the_plan_block_is_cut_out_and_read():
    text, items = extract_plan(
        '只做多：回踩 3860 看反弹。\n<plan>{"items":[{"inst":"xau-usdt-swap","bias":"long","support":[3860, "3840"],'
        '"resistance":[3920]}]}</plan>'
    )
    assert text == "只做多：回踩 3860 看反弹。"
    assert items == (InstrumentPlan(XAU, "long", (3860.0, 3840.0), (3920.0,)),)
    assert extract_plan('观望\n<plan>{"inst":"XAU-USDT-SWAP","bias":"none"}</plan>')[1] == (InstrumentPlan(XAU, "none"),)
    # A direction the program does not know, or a block cut short, is no plan at all, and never shown.
    assert extract_plan('看多\n<plan>{"inst":"XAU-USDT-SWAP","bias":"bullish"}</plan>') == ("看多", None)
    assert extract_plan('看多\n<plan>{"inst":"XAU-US') == ("看多", None)


# --------------------------------------------------------------- how the day stands


def test_the_day_counts_new_positions_losses_in_a_row_and_what_was_made():
    start, end = day_bounds(date(2027, 1, 12))
    closed = read_closed(_Records([
        closed_row(start - 3600, start + 600, -20.0),  # opened yesterday, lost today: counts as a loss, not an opening
        closed_row(start + 1000, start + 2000, 30.0),  # a win
        closed_row(start + 3000, start + 3500, -5.0, close_type="1"),  # half of the next one, lost
        closed_row(start + 3000, start + 4000, -10.0),  # its last half: the trade lost 15
        closed_row(start + 5000, start + 6000, -8.0),
    ]))  # fmt: skip
    held = small(c_time=int((start + 7000) * 1000))
    opened, streak, finished, realized, ccy = day_figures(closed, [held], start, end)
    assert (opened, streak, finished, ccy) == (4, 2, 4, "USDT")  # the win broke the run: two losses in a row
    assert realized == -20.5 + 29.5 - 5.5 - 10.5 - 8.5


class _Records:
    def __init__(self, rows):
        self.rows = rows

    def position_history(self):
        return self.rows


def test_a_new_position_is_refused_without_a_plan_and_against_it():
    rules, journal, clock, exchange = discipline()
    assert "还没有交易计划，不开新仓" in rules.refusal(XAU, "long")
    rules.keep(rules.today(), LONG_PLAN, "只做多")
    assert rules.refusal(XAU, "long") == ""
    assert "只做多（XAU），这张是做空，和计划相反" in rules.refusal(XAU, "short")
    assert "计划里没有 BTC" in rules.refusal("BTC-USDT-SWAP", "long")
    rules.keep(rules.today(), (InstrumentPlan(XAU, "both"),), "区间")
    assert rules.refusal(XAU, "short") == "" and rules.refusal(XAU, "long") == ""
    rules.keep(rules.today(), (InstrumentPlan(XAU, "none"),), "观望")
    assert "观望" in rules.refusal(XAU, "long")
    # Without PLAN_REQUIRED the plan does not hold anything back.
    loose, *_ = discipline(plan_required=False)
    assert loose.refusal(XAU, "short") == ""


def test_the_day_stops_after_three_losses_in_a_row_until_the_next_trading_day():
    rules, journal, clock, exchange = discipline()
    rules.keep(rules.today(), LONG_PLAN, "")
    start, _ = day_bounds(rules.today())
    exchange.closed = [closed_row(start + i * 1000, start + i * 1000 + 500, -10.0) for i in range(1, 4)]
    why = rules.refusal(XAU, "long")
    assert why.startswith("今天停手了：今天已经连亏 3 笔，到了你定的 3 笔。新的交易日 01-12 14:00（北京 01-13 06:00）开始")
    assert "平仓、撤单、改止损不受影响" in why
    assert rules.refusal(XAU, "long", adding=True) == why  # adding to a position is held back too
    private, public = rules.stop_alert()
    assert "连亏 3 笔" in private and "按规矩今天停手" in public and "-10" not in public
    assert rules.stop_alert() == ("", "")  # told once a day
    clock.advance(8 * 3600)  # past 17:00 New York: a new trading day, which needs its own plan
    assert "还没有交易计划" in rules.refusal(XAU, "long")


def test_the_day_stops_at_its_loss_limit_and_counts_new_positions_only():
    exchange = Market()
    rules, journal, clock, _ = discipline(exchange, plan_max_loss_pct=10.0, plan_max_trades=2)
    rules.keep(rules.today(), LONG_PLAN, "")
    start, _ = day_bounds(rules.today())
    exchange.closed = [closed_row(start + 1000, start + 1500, 15.0), closed_row(start + 2000, start + 2500, 30.0)]
    assert "今天已经开了 2 笔新仓，到了每天 2 笔的上限" in rules.refusal(XAU, "long")
    assert rules.refusal(XAU, "long", adding=True) == ""  # an addition is not a new position
    journal.record_snapshot(make_state([], total_eq=2000.0), ts=start + 60)  # the day began with 2000
    exchange.closed = [closed_row(start + 1000, start + 1500, -210.0)]  # 10% of 2000 is 200
    assert "已实现亏损 210.50 USDT，到了开盘时权益的 10%" in rules.refusal(XAU, "long")


def test_records_that_cannot_be_read_hold_new_positions_back():
    exchange = Market()
    rules, *_ = discipline(exchange)
    exchange.history_failures = [OSError("down")]

    def broken():
        raise __import__("bot.okx_client", fromlist=["OkxNetworkError"]).OkxNetworkError("down")

    exchange.position_history = broken
    assert "读不到 OKX 的平仓记录" in rules.refusal(XAU, "long")
    on_mt5, *_ = discipline(exchange, broker="mt5")
    assert "读不到 MT5 的平仓记录" in on_mt5.refusal(XAU, "long")
    assert "这次没读到 MT5" in "\n".join(on_mt5.status_lines())


# ---------------------------------------------------------------------- the checklist


def test_an_idea_that_passes_every_item_and_one_that_does_not():
    exchange = Market(step=1.0)  # a steady climb: 4-hour and daily trend up, last 3880
    rules, journal, clock, _ = discipline(exchange)
    rules.keep(rules.today(), (InstrumentPlan(XAU, "long", (3870.0,), (3950.0,)),), "")
    verdict = rules.check_setup(XAU, "long", 3872.0, 3860.0, 3900.0)
    assert "✓ 今天的计划：只做多，这个想法是做多，一致" in verdict
    assert "✓ 大周期：4 小时偏多" in verdict and "✓ 关键位：入场 3872 离最近的关键位 3870 差 2" in verdict
    assert "✓ 盈亏比：2.3:1" in verdict and verdict.endswith("结论：全部达标，可以把这个想法写出来。")
    poor = rules.check_setup(XAU, "long", 3872.0, 3860.0, 3885.0)
    assert "✗ 盈亏比：1.1:1" in poor and "结论：不达标，差 盈亏比。按规矩回答“观望”" in poor
    against = rules.check_setup(XAU, "short", 3872.0, 3885.0, 3840.0)
    assert "✗ 今天的计划" in against and "✗ 大周期" in against
    far = rules.check_setup(XAU, "long", 3800.0, 3790.0, 3830.0)
    assert "✗ 关键位" in far


def test_the_checklist_knows_the_data_releases_and_a_missing_plan():
    nfp = datetime(2026, 12, 4, 8, 15, tzinfo=NY).timestamp()  # a quarter of an hour before the jobs report
    rules, journal, clock, exchange = discipline(clock=Clock(nfp))
    verdict = rules.check_setup(XAU, "long", 3872.0, 3860.0, 3900.0)
    assert "✗ 今天的计划：今天还没有交易计划" in verdict
    assert "✗ 重要数据：美国非农就业" in verdict


def test_the_ai_is_told_the_plan_and_whether_the_day_still_allows_a_trade():
    rules, journal, clock, exchange = discipline()
    assert "还没有交易计划：不要给任何进场价" in rules.notes_for_ai()
    rules.keep(rules.today(), LONG_PLAN, "")
    note = rules.notes_for_ai()
    assert "XAU-USDT-SWAP 只做多（支撑 3860、3840；阻力 3920）" in note and "今天还能开 3 笔新仓，最近连亏 0 笔" in note
    assert "连亏" not in rules.notes_for_ai(with_status=False)


def test_the_plan_card_says_the_rules_and_until_when():
    rules, journal, clock, exchange = discipline()
    plan = rules.keep(rules.today(), LONG_PLAN, "如果回踩 3860 企稳……")
    card = rules.card(plan, amounts=True)
    assert "- **XAU：只做多**，支撑 3860、3840；阻力 3920" in card
    assert "今天最多开 3 笔新仓（加仓不算）；连亏 3 笔或当天已实现亏损到 500.00 USDT，开盘时权益的 25%，当天就停手" in card
    assert "有效到 01-12 14:00（北京 01-13 06:00）" in card and card.endswith("如果回踩 3860 企稳……")
    assert "500.00" not in rules.card(plan, amounts=False)
    assert rules.title(plan) == "交易计划 · 01-12 周二"
    assert rules.revisions_left() == 1
    again = rules.keep(rules.today(), LONG_PLAN, "第二版")
    assert rules.title(again) == "交易计划 · 01-12 周二（第 2 版）" and rules.revisions_left() == 0


# ------------------------------------------------------------- the desk holds the line


def test_the_desk_refuses_an_open_against_the_plan_but_never_a_close_or_a_stop():
    bench = Bench(exchange=Market(positions=[small(stops=(StopOrder("s1", sl_trigger_px=3800.0),))]), trading="live")
    rules = Discipline(bench.cfg, bench.journal, bench.exchange, clock=bench.clock)
    bench.desk.discipline = rules
    refused = bench.say("做多 XAU 100张 止损 3860")
    assert "还没有交易计划，不开新仓" in refused.text and bench.sent == []
    rules.keep(rules.today(), (InstrumentPlan(XAU, "short"),), "")
    assert "和计划相反" in bench.say("加仓 XAU 100张").text
    assert "确认 1111" in bench.say("止损 XAU 3810").text  # moving a stop is never held back
    assert "确认 2222" in bench.say("平仓 XAU").text
    rules.keep(rules.today(), LONG_PLAN, "")
    assert "确认 3333" in bench.say("加仓 XAU 100张 止损 3810").text
    assert any("交易计划" in line for line in bench.desk.status_lines())


# ---------------------------------------------------------------------- the reporter


def plan_desk(clock=None, **cfg):
    from bot.service import Reporter
    from tests.helpers import FakeAnalyst, FakeFeishu, FakeOkx

    clock = clock or Clock(TUESDAY_10AM)
    okx = FakeOkx(make_state([], total_eq=2000.0))
    journal = Journal(":memory:")
    feishu, analyst = FakeFeishu(), FakeAnalyst()
    reporter = Reporter(make_cfg(trade_instruments=(XAU,), **cfg), feishu, okx, analyst, journal, clock)
    reporter.discipline = Discipline(reporter._cfg, journal, okx, clock=clock)
    return reporter, feishu, analyst, okx, clock


def test_the_plan_is_written_once_shown_to_owner_and_group_and_redone_only_once():
    from tests.helpers import GROUP, OWNER

    reporter, feishu, analyst, _, _ = plan_desk()
    plan, why = reporter.write_plan()
    assert why == "" and plan.item(XAU).bias == "long" and plan.item(XAU).support == (3550.0,)
    assert [(sent.target, sent.title) for sent in feishu.sent] == [(OWNER, "交易计划 · 01-12 周二"), (GROUP, "交易计划 · 01-12 周二")]
    assert "只做多" in feishu.sent[0].body and "<plan>" not in feishu.sent[0].body
    assert analyst.named("day_plan")[0]["day"] == "01-12 周二"

    feishu.sent.clear()
    assert reporter.write_plan()[0].id == plan.id and feishu.sent == []  # asked again: the plan in force, not a new one

    analyst.plan = ("AI:观望", (InstrumentPlan(XAU, "none", (), ()),))
    again, why = reporter.write_plan("非农数据出来了", again=True)
    assert again.revision == 1 and again.item(XAU).bias == "none"
    assert analyst.named("day_plan")[1]["reason"] == "非农数据出来了" and analyst.named("day_plan")[1]["earlier"]
    assert feishu.sent[0].title == "交易计划 · 01-12 周二（第 2 版）"

    plan, why = reporter.write_plan("再改一次", again=True)
    assert plan is None and why.startswith("今天的计划已经重做过了，一天只能重做一次")


def test_a_plan_without_its_block_is_not_kept_and_the_scheduled_one_says_so():
    reporter, feishu, analyst, _, clock = plan_desk()
    analyst.plan = ("AI:没有块", ())
    clock.now = datetime(2027, 1, 15, 17, 5, tzinfo=NY).timestamp()
    reporter.scheduled_plan()  # Friday evening: the weekend has no plan
    assert analyst.named("day_plan") == []
    clock.now = datetime(2027, 1, 12, 17, 5, tzinfo=NY).timestamp()
    reporter.scheduled_plan()
    assert analyst.named("day_plan")[0]["day"] == "01-13 周三"
    assert feishu.sent[-1].title == "交易计划没有写成" and "没有计划时不能开新仓" in feishu.sent[-1].body
    assert reporter.discipline.plan(reporter.discipline.today()) is None


def test_after_a_close_the_day_stop_is_told_once_to_owner_and_group():
    from bot.monitor import Event
    from tests.helpers import GROUP, OWNER

    reporter, feishu, _, okx, _ = plan_desk()
    start, _ = day_bounds(reporter.discipline.today())
    okx.closed = [closed_row(start + i * 1000, start + i * 1000 + 500, -10.0) for i in range(1, 3)]
    closed = Event(kind="closed", title="平仓：XAU 多单", inst_id=XAU, to_group=True)
    reporter.notify_event(closed)
    assert all(sent.title != "今天停手：不再开新仓" for sent in feishu.sent)  # two in a row: not yet

    okx.closed.append(closed_row(start + 4000, start + 4500, -10.0))
    reporter.notify_event(closed)
    stops = [(sent.target, sent.body) for sent in feishu.sent if sent.title == "今天停手：不再开新仓"]
    assert [target for target, _ in stops] == [OWNER, GROUP]
    assert "连亏 3 笔" in stops[0][1] and "不接受开仓和加仓" in stops[0][1]

    feishu.sent.clear()
    reporter.notify_event(closed)
    assert all(sent.title != "今天停手：不再开新仓" for sent in feishu.sent)


def test_on_start_a_weekday_without_a_plan_gets_one_and_only_once():
    reporter, feishu, analyst, _, clock = plan_desk()
    reporter.plan_if_missing()
    reporter.plan_if_missing()
    assert len(analyst.named("day_plan")) == 1 and feishu.sent[0].title == "交易计划 · 01-12 周二"
    clock.now = datetime(2027, 1, 16, 10, 0, tzinfo=NY).timestamp()  # Saturday: no trading day to plan
    reporter.plan_if_missing()
    assert len(analyst.named("day_plan")) == 1
    reporter, _, analyst, _, _ = plan_desk(plan_required=False)
    reporter.plan_if_missing()
    assert analyst.named("day_plan") == []
