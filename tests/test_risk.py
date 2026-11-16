"""Risk measured against the market's own movement: the figures, the words, the thresholds and the alerts."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.config import load_config
from bot.journal import Journal
from bot.models import Candle, StopOrder
from bot.monitor import Monitor
from bot.okx_client import OkxNetworkError
from bot.reports import account_text, status_markdown
from bot.risk import (
    AccountRisk,
    RiskGauge,
    day_range,
    funding_line,
    leverage_line,
    liq_line,
    measure,
    readings,
    stop_line,
    weekend_lines,
)
from bot.scheduler import due_jobs, scheduled_jobs
from bot.service import Reporter

from .helpers import GROUP, OWNER, Clock, FakeAnalyst, FakeFeishu, FakeOkx, make_cfg, make_position, make_state
from .test_okx_client import client, ok

DAY_MS = 24 * 3600 * 1000
MONDAY = datetime(2026, 10, 5, tzinfo=timezone.utc)  # a Monday
RANGE = 94.5  # the daily range the examples are measured against
FUNDING = (0.00007, 8.0)


def gold(**overrides):
    """The account the research report looked at: 10 XAU long, no stop, liquidation 168 away."""
    values = dict(
        qty=10000.0, coins=10.0, coin_ccy="XAU", avg_px=3845.0, mark_px=3873.0, liq_px=3705.0, upl=280.0, lever=100.0,
        notional_usd=38730.0, stops=(), ccy="USDT",
    )  # fmt: skip
    values.update(overrides)
    return make_position(**values)


def with_stop(price: float, **overrides):
    return gold(stops=(StopOrder("a1", sl_trigger_px=price),), **overrides)


def account(*positions, equity: float = 1850.0, atr_day: float | None = RANGE, funding=FUNDING) -> AccountRisk:
    return AccountRisk(equity, tuple(measure(position, equity, atr_day, funding) for position in positions))


class Gauge:
    """Measures with a fixed daily range, so a test only has to move the position."""

    def __init__(self):
        self.broken = False

    def assess(self, state) -> AccountRisk:
        if self.broken:
            raise RuntimeError("no candles today")
        return account(*state.positions, equity=state.snapshot.total_eq)


def days(count: int, weekday_range: float, weekend_range: float, last_confirmed: bool = True) -> list[Candle]:
    candles = []
    for index in range(count):
        day = MONDAY + timedelta(days=index)
        half = (weekend_range if day.weekday() >= 5 else weekday_range) / 2
        candles.append(Candle(int(day.timestamp() * 1000), 3900.0, 3900.0 + half, 3900.0 - half, 3900.0))
    if not last_confirmed:
        candles[-1] = Candle(candles[-1].ts, 3900.0, 4400.0, 3400.0, 3900.0, confirmed=False)
    return candles


# ------------------------------------------------------------------ the daily range


def test_the_daily_range_is_not_pulled_down_by_quiet_weekends():
    assert day_range(days(42, 100.0, 20.0)) == pytest.approx(100.0)
    assert day_range(days(42, 100.0, 100.0)) == pytest.approx(100.0)
    assert day_range(days(42, 20.0, 100.0)) > 40.0  # a market that is wilder at the weekend: the larger figure still


def test_the_day_that_is_still_running_does_not_count():
    assert day_range(days(42, 100.0, 20.0, last_confirmed=False)) == pytest.approx(100.0)


def test_too_few_days_give_no_range():
    assert day_range(days(10, 100.0, 20.0)) is None
    assert day_range([]) is None


# ------------------------------------------------------------------- the figures


def test_the_figures_of_the_position_in_the_report():
    risk = measure(gold(), 1850.0, RANGE, FUNDING)
    assert risk.notional == pytest.approx(38730.0)
    assert risk.leverage == pytest.approx(20.94, abs=0.01)
    assert risk.liq_gap == pytest.approx(168.0) and risk.liq_atr == pytest.approx(1.78, abs=0.01)
    assert risk.liq_cost == pytest.approx(1680.0) and risk.liq_cost_pct == pytest.approx(90.8, abs=0.1)
    assert risk.stop_gap is None and risk.stop_cost is None and risk.stop_atr is None
    assert risk.funding_cost == pytest.approx(2.7111, abs=0.001)


def test_a_stop_is_priced_from_where_the_market_is_now():
    risk = measure(with_stop(3768.0), 1850.0, RANGE, FUNDING)
    assert risk.stop_gap == pytest.approx(105.0) and risk.stop_cost == pytest.approx(1050.0)
    assert risk.stop_cost_pct == pytest.approx(56.8, abs=0.1) and risk.stop_atr == pytest.approx(1.11, abs=0.01)
    # A stop above the mark price of a long locks a profit in: it costs nothing from here.
    locked = measure(with_stop(3880.0), 1850.0, RANGE, FUNDING)
    assert locked.stop_gap == 0 and locked.stop_cost == 0


def test_a_short_is_measured_the_other_way_round():
    short = gold(direction="short", liq_px=4041.0, stops=(StopOrder("a1", sl_trigger_px=3900.0),))
    risk = measure(short, 1850.0, RANGE, FUNDING)
    assert risk.liq_gap == pytest.approx(168.0) and risk.stop_gap == pytest.approx(27.0)
    assert risk.funding_cost == pytest.approx(-2.7111, abs=0.001)  # with a positive rate the shorts are paid


def test_what_cannot_be_known_stays_empty():
    risk = measure(gold(coins=None, notional_usd=None, liq_px=None), None, None, None)
    assert risk.notional is None and risk.leverage is None and risk.liq_atr is None and risk.funding_cost is None
    assert AccountRisk(None, (risk,)).leverage is None
    assert AccountRisk(0.0, (measure(gold(), 0.0, RANGE, None),)).leverage is None  # no equity: no ratio, no crash
    assert AccountRisk(1850.0, ()).leverage is None


def test_several_positions_add_up_to_one_leverage():
    both = account(gold(), gold(inst_id="BTC-USDT-SWAP", coins=0.01, mark_px=65000.0, liq_px=None))
    assert both.notional == pytest.approx(38730.0 + 650.0)
    assert both.leverage == pytest.approx(21.29, abs=0.01)
    assert both.of(gold()).leverage == pytest.approx(20.94, abs=0.01)
    assert both.of(gold(inst_id="ETH-USDT-SWAP")) is None


# --------------------------------------------------------------------- the words


def test_the_lines_carry_amounts_units_and_how_they_were_worked_out():
    risk = account(gold())
    assert leverage_line(risk) == "实际杠杆 20.9 倍（全部仓位的名义价值 38,730.00 ÷ 账户权益 1,850.00）"
    measured = risk.positions[0]
    assert liq_line(measured) == "预估强平价 3,705.00，离标记价 168.00（4.34%），相当于 1.8 天的正常波幅（日线 ATR 94.50）"
    assert stop_line(measured) == "没有止损：价格走到强平价，这个仓位亏约 1,680.00 USDT，占权益 91%"
    assert funding_line(measured) == (
        "资金费 (funding) 本期 +0.0070%，按现在的仓位每 8 小时付约 2.71 USDT，照这个费率一天约 8.13 USDT"
    )


def test_the_stop_line_says_what_the_stop_costs():
    measured = account(with_stop(3768.0)).positions[0]
    assert stop_line(measured) == "止损 3,768.00 触发时比现在少约 1,050.00 USDT，占权益 56.8%；止损离标记价 1.1 天的正常波幅"
    assert "有利一侧" in stop_line(account(with_stop(3880.0)).positions[0])


def test_lines_are_left_out_when_their_figures_are_missing():
    bare = measure(gold(liq_px=None, coins=None), 1850.0, None, None)
    assert liq_line(bare) == "" and funding_line(bare) == "" and stop_line(bare) == "没有止损"
    assert leverage_line(AccountRisk(None, (bare,))) == ""
    paid = measure(gold(direction="short", liq_px=4041.0), 1850.0, RANGE, FUNDING)
    assert "每 8 小时收约 2.71 USDT" in funding_line(paid)


def test_the_status_card_shows_the_measurements_to_the_owner_only():
    state = make_state([gold()], total_eq=1850.0)
    risk = account(gold())
    card = status_markdown(state, None, private=True, risk=risk)
    for expected in ("实际杠杆 20.9 倍", "相当于 1.8 天的正常波幅", "没有止损：价格走到强平价", "资金费 (funding)"):
        assert expected in card
    assert card.count("预估强平价") == 1  # the line with the range replaces the plain one
    public = status_markdown(state, None, private=False, risk=risk)
    assert "实际杠杆" not in public and "强平" not in public and "资金费" not in public
    assert "实际杠杆" not in status_markdown(state, None, private=True)  # without a gauge the card is as before


def test_the_ai_is_given_the_same_measurements():
    text = account_text(make_state([gold()], total_eq=1850.0), None, account(gold()))
    assert "实际杠杆 20.9 倍" in text and "相当于 1.8 天的正常波幅" in text and "没有止损：价格走到强平价" in text
    assert "实际杠杆" not in account_text(make_state([gold()], total_eq=1850.0), None)
    assert "程序按 OKX 的数字算好的" in text


def test_the_ai_knows_the_owners_risk_rule_and_how_orders_are_given():
    from bot.analyst import ANALYST_SYSTEM

    text = account_text(make_state([gold()], total_eq=1850.0), None, account(gold()), risk_pct=1.0)
    assert "交易员自己定的规则：一笔交易止损触发时最多亏权益的 1%，按现在的权益约 18.50 USD" in text
    assert "一律按“止损触发时账户比现在少多少”来对照" in text  # one basis, so answers do not contradict
    assert "不要说没有权限" in ANALYST_SYSTEM and "不要让他“去 @机器人”" in ANALYST_SYSTEM and "只发数字也行" in ANALYST_SYSTEM


# ----------------------------------------------------------------- the thresholds


def levels(risk: AccountRisk, **cfg) -> dict[str, int]:
    return {reading.key.split(":")[0]: reading.level for reading in readings(risk, make_cfg(**cfg))}


def test_each_reading_is_set_against_the_owners_lines():
    # The owner keeps 100x on the exchange and sizes by risk, so leverage has no line unless he sets one.
    assert levels(account(gold())) == {"liq": 2}  # 1.8 days from liquidation; no stop to price
    assert levels(account(with_stop(3768.0))) == {"liq": 2, "stop": 1}  # his rule: one trade risks 1% of the equity
    assert levels(account(gold(mark_px=3780.0))) == {"liq": 3}  # 75 away: under one day
    small = gold(qty=400.0, coins=0.4, liq_px=None, stops=(StopOrder("a1", sl_trigger_px=3827.0),))
    assert levels(account(small)) == {"stop": 0}  # a stop that costs just under 1%
    assert levels(account(gold()), alert_leverage=(5.0, 10.0)) == {"leverage": 2, "liq": 2}  # 20.9 times
    assert levels(account(small), alert_leverage=(5.0, 10.0)) == {"leverage": 0, "stop": 0}  # 0.84 times


def test_the_lines_can_be_moved_or_switched_off():
    risk = account(with_stop(3768.0))
    assert levels(risk, alert_leverage=(25.0,), alert_liq_atr=(1.5,), alert_stop_risk_pct=60.0) == {"leverage": 0, "liq": 0, "stop": 0}
    assert levels(risk, alert_leverage=(), alert_liq_atr=(), alert_stop_risk_pct=None) == {}


def test_a_reading_names_its_position_when_there_are_several():
    both = account(gold(), gold(inst_id="XAG-USDT-SWAP"))
    lines = [reading.line for reading in readings(both, make_cfg(alert_leverage=(5.0, 10.0)))]
    assert lines[1].startswith("XAU-USDT-SWAP 多单：预估强平价") and lines[2].startswith("XAG-USDT-SWAP 多单：")
    assert readings(account(gold()), make_cfg())[0].line.startswith("预估强平价")
    assert "你定的单笔风险上限：权益的 1%" in readings(account(with_stop(3768.0)), make_cfg())[1].line
    assert "你设的线：不到 3、2、1 天" in lines[1] and "你设的线：超过 5、10 倍" in lines[0]


def levels_from(tmp_path, text: str):
    for name in ("ALERT_LIQ_ATR", "ALERT_LEVERAGE", "ALERT_STOP_RISK_PCT", "WEEKEND_CHECK", "RISK_PER_TRADE_PCT", "WEEKEND_LEVERAGE"):
        os.environ.pop(name, None)
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    cfg, problems = load_config(path)
    return cfg, [item for item in problems if "ALERT_L" in item]


def test_the_thresholds_are_read_from_the_file(tmp_path):
    cfg, problems = levels_from(tmp_path, "")
    assert (cfg.alert_liq_atr, cfg.alert_leverage, cfg.alert_stop_risk_pct, cfg.weekend_check) == ((3.0, 2.0, 1.0), (), 1.0, True)
    assert (cfg.risk_per_trade_pct, cfg.weekend_leverage) == (1.0, None)
    cfg, problems = levels_from(tmp_path, "RISK_PER_TRADE_PCT=2\nWEEKEND_LEVERAGE=3\n")
    assert (cfg.risk_per_trade_pct, cfg.alert_stop_risk_pct, cfg.weekend_leverage) == (2.0, 2.0, 3.0)  # the alert follows the rule
    cfg, problems = levels_from(tmp_path, "RISK_PER_TRADE_PCT=2\nALERT_STOP_RISK_PCT=off\n")
    assert (cfg.risk_per_trade_pct, cfg.alert_stop_risk_pct) == (2.0, None)
    cfg, _ = levels_from(tmp_path, "RISK_PER_TRADE_PCT=25\n")
    assert (cfg.risk_per_trade_pct, cfg.alert_stop_risk_pct) == (25.0, 25.0)  # his call, small capital (2026-11-16)
    os.environ.pop("RISK_PER_TRADE_PCT", None)
    path = tmp_path / ".env"
    path.write_text("RISK_PER_TRADE_PCT=150\n", encoding="utf-8")
    cfg, problems = load_config(path)
    assert cfg.risk_per_trade_pct == 1.0 and any("不能超过 100" in item for item in problems)
    cfg, problems = levels_from(tmp_path, "ALERT_LIQ_ATR=1, 4，2\nALERT_LEVERAGE=10,3\nALERT_STOP_RISK_PCT=1\nWEEKEND_CHECK=false\n")
    assert (cfg.alert_liq_atr, cfg.alert_leverage, cfg.alert_stop_risk_pct, cfg.weekend_check) == ((4.0, 2.0, 1.0), (3.0, 10.0), 1.0, False)
    assert problems == []
    cfg, problems = levels_from(tmp_path, "ALERT_LIQ_ATR=off\nALERT_LEVERAGE=off\nALERT_STOP_RISK_PCT=off\n")
    assert (cfg.alert_liq_atr, cfg.alert_leverage, cfg.alert_stop_risk_pct) == ((), (), None)
    cfg, problems = levels_from(tmp_path, "ALERT_LIQ_ATR=three\nALERT_LEVERAGE=5,-1\n")
    assert (cfg.alert_liq_atr, cfg.alert_leverage) == ((3.0, 2.0, 1.0), ()) and len(problems) == 2
    assert "例如 5,10" in problems[1]


# ------------------------------------------------------------------------ the gauge


class Market(FakeOkx):
    def __init__(self, state=None):
        super().__init__(state)
        self.candle_reads = self.funding_reads = 0

    def candles(self, inst_id, bar="1H", limit=200):
        self.candle_reads += 1
        if self.market_error:
            raise self.market_error
        assert bar == "1Dutc"
        return days(42, 94.5, 20.0)

    def funding(self, inst_id):
        self.funding_reads += 1
        if self.market_error:
            raise self.market_error
        return FUNDING


def test_the_gauge_reads_the_market_and_keeps_what_it_read():
    okx, clock = Market(), Clock(1_800_000_000.0)
    gauge = RiskGauge(okx, clock)
    state = make_state([gold()], total_eq=1850.0)
    risk = gauge.assess(state)
    assert risk.positions[0].liq_atr == pytest.approx(1.78, abs=0.01) and risk.positions[0].funding_hours == 8.0
    gauge.assess(state)
    assert (okx.candle_reads, okx.funding_reads) == (1, 1)
    clock.advance(601)
    gauge.assess(state)
    assert (okx.candle_reads, okx.funding_reads) == (1, 2)  # the rate is read again sooner than the range
    clock.advance(3600)
    gauge.assess(state)
    assert okx.candle_reads == 2


def test_the_gauge_never_fails_a_card_because_the_market_cannot_be_read():
    okx, clock = Market(), Clock(1_800_000_000.0)
    gauge = RiskGauge(okx, clock)
    okx.market_error = OkxNetworkError("timed out")
    blind = gauge.assess(make_state([gold()], total_eq=1850.0)).positions[0]
    assert blind.atr_day is None and blind.funding_rate is None and blind.leverage == pytest.approx(20.94, abs=0.01)
    okx.market_error = None
    assert gauge.assess(make_state([gold()], total_eq=1850.0)).positions[0].atr_day == pytest.approx(94.5)
    okx.market_error = OkxNetworkError("timed out")
    clock.advance(7200)
    assert gauge.assess(make_state([gold()], total_eq=1850.0)).positions[0].atr_day == pytest.approx(94.5)  # the old figure, not none


def test_only_perpetuals_have_funding():
    okx = Market()
    gauge = RiskGauge(okx, Clock(1_800_000_000.0))
    assert gauge.funding("BTC-USDT") is None and okx.funding_reads == 0


def test_the_client_reads_the_rate_and_the_length_of_a_period():
    row = {"fundingRate": "0.00007", "fundingTime": "1800000000000", "nextFundingTime": str(1800000000000 + 4 * 3600 * 1000)}
    okx, _ = client({"/api/v5/public/funding-rate": [ok([row])]})
    assert okx.funding("XAU-USDT-SWAP") == (0.00007, 4.0)
    okx, _ = client({"/api/v5/public/funding-rate": [ok([{"fundingRate": "0.0001", "fundingTime": "", "nextFundingTime": ""}])]})
    assert okx.funding("XAU-USDT-SWAP") == (0.0001, 8.0)
    okx, _ = client({"/api/v5/public/funding-rate": [ok([{"fundingRate": ""}])]})
    assert okx.funding("XAU-USDT-SWAP") is None
    okx, _ = client({"/api/v5/public/funding-rate": [ok([])]})
    assert okx.funding("XAU-USDT-SWAP") is None


# ----------------------------------------------------------------------- the alerts


def watch(*positions, journal=None, **cfg):
    okx = FakeOkx(make_state(list(positions), total_eq=1850.0))
    events, clock, gauge = [], Clock(1_800_000_000.0), Gauge()
    journal = journal or Journal(":memory:")
    values = dict(alert_no_stop_minutes=None, alert_liq_distance_pct=None)
    values.update(cfg)
    monitor = Monitor(okx, make_cfg(**values), journal, events.append, clock, gauge=gauge)
    return monitor, okx, events, clock, journal, gauge


def risk_cards(events):
    return [event for event in events if event.kind == "risk_level"]


def test_by_default_the_card_leaves_leverage_alone():
    monitor, _, events, _, _, _ = watch(gold())
    monitor.poll_once()
    [card] = risk_cards(events)
    assert card.private_lines[0].startswith("预估强平价 3,705.00") and not any("实际杠杆" in line for line in card.private_lines)


def test_one_card_says_everything_that_stands_out_and_is_not_repeated():
    monitor, _, events, clock, _, _ = watch(gold(), alert_leverage=(5.0, 10.0))
    monitor.poll_once()
    [card] = risk_cards(events)
    assert card.title == "仓位风险警告" and card.warning and not card.to_group
    assert len(card.private_lines) == 3
    assert card.private_lines[0].startswith("实际杠杆 20.9 倍") and "价格每逆向走 1%，权益变动约 21%" in card.private_lines[0]
    assert card.private_lines[1].startswith("预估强平价 3,705.00")
    assert "由你决定" in card.private_lines[2]
    for _ in range(5):
        clock.advance(3600)
        monitor.poll_once()
    assert len(risk_cards(events)) == 1


def test_the_card_offers_the_commands_that_are_open_with_the_price_left_to_him():
    monitor, _, events, _, _, _ = watch(gold(), trading="live", trade_instruments=("XAU-USDT-SWAP",), trade_actions=("stops", "close", "cancel"))
    monitor.poll_once()
    [card] = risk_cards(events)
    assert card.private_lines[-1] == "要动手的话，私聊或在群里 @我发“止损 XAU 价格”或“平仓 XAU 一半”，我先回预览，你回确认码才执行。"
    monitor, _, events, _, _, _ = watch(gold(), trading="live", trade_instruments=("XAU-USDT-SWAP",), trade_actions=("stops",))
    monitor.poll_once()
    assert "“平仓 XAU 一半”" not in risk_cards(events)[0].private_lines[-1]
    monitor, _, events, _, _, _ = watch(gold())  # trading off: nothing to send
    monitor.poll_once()
    assert "由你决定" in risk_cards(events)[0].private_lines[-1]


def test_a_reading_that_gets_worse_is_reported_again():
    monitor, okx, events, clock, _, _ = watch(gold())
    monitor.poll_once()
    okx.state = make_state([gold(mark_px=3780.0)], total_eq=1850.0)  # 75 from liquidation: under one day's range
    clock.advance(60)
    monitor.poll_once()
    assert len(risk_cards(events)) == 1  # two cards within half an hour would be noise
    clock.advance(1800)
    monitor.poll_once()
    assert [card.title for card in risk_cards(events)] == ["仓位风险警告", "仓位风险紧急"]


def test_better_and_then_worse_again_is_news():
    monitor, okx, events, clock, _, _ = watch(gold())
    monitor.poll_once()
    okx.state = make_state([gold(qty=400.0, coins=0.4, liq_px=None)], total_eq=1850.0)  # cut to 0.4 XAU
    clock.advance(3600)
    monitor.poll_once()
    okx.state = make_state([gold()], total_eq=1850.0)
    clock.advance(3600)
    monitor.poll_once()
    assert len(risk_cards(events)) == 2


def test_nothing_is_said_inside_the_lines():
    small = gold(qty=400.0, coins=0.4, liq_px=3300.0, stops=(StopOrder("a1", sl_trigger_px=3840.0),))
    monitor, _, events, _, journal, _ = watch(small)
    monitor.poll_once()
    assert risk_cards(events) == [] and journal.get_setting("risk_levels") == ""


def test_a_restart_does_not_repeat_what_was_already_said():
    monitor, _, events, _, journal, _ = watch(gold())
    monitor.poll_once()
    again, _, later, _, _, _ = watch(gold(), journal=journal)
    again.poll_once()
    assert len(risk_cards(events)) == 1 and risk_cards(later) == []


def test_a_closed_position_is_forgotten():
    monitor, okx, events, clock, journal, _ = watch(gold())
    monitor.poll_once()
    okx.state = make_state([], total_eq=1850.0)
    clock.advance(3600)
    monitor.poll_once()
    assert journal.get_setting("risk_levels") == "{}"
    okx.state = make_state([gold()], total_eq=1850.0)
    clock.advance(3600)
    monitor.poll_once()
    assert len(risk_cards(events)) == 2


def test_a_gauge_that_breaks_does_not_stop_the_watching():
    monitor, okx, events, clock, _, gauge = watch(gold())
    gauge.broken = True
    monitor.poll_once()
    okx.state = make_state([], total_eq=1850.0)
    clock.advance(60)
    monitor.poll_once()
    assert risk_cards(events) == [] and [event.kind for event in events] == ["closed"]


def test_without_a_gauge_the_monitor_is_as_before():
    okx = FakeOkx(make_state([gold()], total_eq=1850.0))
    events = []
    Monitor(okx, make_cfg(alert_no_stop_minutes=None, alert_liq_distance_pct=None), Journal(":memory:"), events.append, Clock(1.8e9)).poll_once()
    assert events == []


def test_the_missing_stop_reminder_says_what_is_at_stake():
    monitor, _, events, clock, _, _ = watch(gold(), alert_no_stop_minutes=10.0, alert_leverage=(), alert_liq_atr=())
    monitor.poll_once()
    clock.advance(601)
    monitor.poll_once()
    [reminder] = [event for event in events if event.kind == "no_stop"]
    assert any("相当于 1.8 天的正常波幅" in line for line in reminder.private_lines)
    assert any(line.startswith("没有止损：价格走到强平价，这个仓位亏约 1,680.00 USDT") for line in reminder.private_lines)


# ----------------------------------------------------------------- before a weekend


def test_what_stands_out_before_a_weekend():
    lines = weekend_lines(account(gold()), make_cfg())
    assert len(lines) == 2  # no leverage line unless he sets one
    assert lines[0].startswith("XAU-USDT-SWAP 多单：没有止损") and "1.8 天" in lines[1]
    lines = weekend_lines(account(gold()), make_cfg(weekend_leverage=3.0))
    assert len(lines) == 3 and "高于你设的周末线 3 倍" in lines[0]
    tight = account(gold(qty=400.0, coins=0.4, liq_px=None, stops=(StopOrder("a1", sl_trigger_px=3800.0),)))
    [line] = weekend_lines(tight, make_cfg())
    assert "离标记价只有 0.8 天的正常波幅" in line
    calm = account(gold(qty=400.0, coins=0.4, liq_px=None, stops=(StopOrder("a1", sl_trigger_px=3700.0),)))
    assert weekend_lines(calm, make_cfg()) == []
    assert weekend_lines(account(gold(inst_id="BTC-USDT-SWAP")), make_cfg()) == []  # trades through the weekend like any day


def test_the_check_is_due_an_hour_before_gold_closes_on_friday():
    cfg = make_cfg(brief_morning=None, brief_evening=None, weekly_review=None)
    la = ZoneInfo("America/Los_Angeles")
    friday = datetime(2026, 11, 20, 13, 5, tzinfo=la)  # 21:05 UTC
    assert due_jobs(cfg, friday, lambda name: False) == ["weekend"]
    assert due_jobs(cfg, friday.replace(hour=12, minute=59), lambda name: False) == []
    assert due_jobs(cfg, friday, lambda name: name == "weekend") == []
    assert scheduled_jobs(cfg, friday.replace(day=19)) == []
    assert scheduled_jobs(make_cfg(brief_morning=None, brief_evening=None, weekly_review=None, weekend_check=False), friday) == []
    # Where Friday evening UTC is already Saturday morning, it runs then.
    hanoi = datetime(2026, 11, 21, 4, 10, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    assert due_jobs(make_cfg(brief_morning=None, brief_evening=None, weekly_review=None, timezone="Asia/Ho_Chi_Minh"), hanoi, lambda name: False) == ["weekend"]
    # In summer spot gold closes an hour earlier in UTC, and the check moves with it: 16:00 New York.
    june = datetime(2027, 6, 4, 13, 5, tzinfo=la)  # 20:05 UTC, 16:05 in New York
    assert due_jobs(cfg, june, lambda name: False) == ["weekend"]
    assert due_jobs(cfg, june.replace(hour=12, minute=55), lambda name: False) == []


def reporter_with(*positions, **cfg):
    values = dict(alert_no_stop_minutes=None, alert_liq_distance_pct=None, alert_leverage=(), alert_liq_atr=(), alert_stop_risk_pct=None)
    values.update(cfg)
    config = make_cfg(**values)
    feishu, okx, clock, journal = FakeFeishu(), FakeOkx(make_state(list(positions), total_eq=1850.0)), Clock(1_800_000_000.0), Journal(":memory:")
    reporter = Reporter(config, feishu, okx, FakeAnalyst(), journal, clock, gauge=Gauge())
    reporter.monitor = Monitor(okx, config, journal, reporter.notify_event, clock, gauge=Gauge())
    reporter.monitor.poll_once()
    return reporter, feishu


def test_the_weekend_card_goes_to_the_owner_and_the_shared_group():
    reporter, feishu = reporter_with(gold())
    reporter.send_weekend_check()
    owner, group = feishu.sent
    assert (owner.target, group.target) == (OWNER, GROUP)
    assert owner.title.startswith("周末前检查") and "周五 22:00 UTC 收盘" in owner.body and "没有止损" in owner.body
    assert "由你决定" in owner.body and group.body == owner.body


def test_the_weekend_card_stays_private_when_the_group_does_not_see_the_account():
    reporter, feishu = reporter_with(gold(), group_show_pnl=False)
    reporter.send_weekend_check()
    assert [sent.target for sent in feishu.sent] == [OWNER]


def test_no_weekend_card_when_nothing_stands_out():
    calm = gold(qty=400.0, coins=0.4, liq_px=None, stops=(StopOrder("a1", sl_trigger_px=3700.0),))
    for positions in ([calm], [], [gold(inst_id="BTC-USDT-SWAP")]):
        reporter, feishu = reporter_with(*positions)
        reporter.send_weekend_check()
        assert feishu.sent == []


def test_risk_cards_reach_the_group_only_when_the_account_is_shared():
    reporter, feishu = reporter_with(gold(), alert_leverage=(5.0, 10.0), alert_liq_atr=(3.0, 2.0, 1.0))
    assert [(sent.target, sent.title) for sent in feishu.sent] == [(OWNER, "仓位风险警告"), (GROUP, "仓位风险警告")]
    reporter, feishu = reporter_with(gold(), alert_leverage=(5.0, 10.0), group_show_pnl=False)
    assert [sent.target for sent in feishu.sent] == [OWNER]
