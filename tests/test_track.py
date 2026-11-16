"""The track record: reading the AI's levels, scoring them against candles, and the card."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from bot.analyst import ANALYST_SYSTEM, Analyst
from bot.journal import Journal
from bot.models import Candle
from bot.okx_client import OkxError
from bot.track import BAR_SECONDS, LEVELS_NOTE, Levels, Scenario, Scorer, extract_levels, parse_levels, price_at, score_call, scorecard_markdown

from .helpers import Clock, make_cfg
from .test_analyst import FakeClient, message

XAU = "XAU-USDT-SWAP"
T0 = 1_800_000_000 - 1_800_000_000 % BAR_SECONDS  # the start of a 15-minute candle
BLOCK = (
    '<levels>{"inst":"XAU-USDT-SWAP","bias":"long","scenarios":['
    '{"side":"long","trigger":3900,"target":3950,"invalid":3860},'
    '{"side":"short","trigger":3860,"target":3820,"invalid":3900}]}</levels>'
)
LONG = Scenario("long", 3900.0, 3950.0, 3860.0)
SHORT = Scenario("short", 3860.0, 3820.0, 3900.0)
UTC = ZoneInfo("UTC")


# ------------------------------------------------------------------ the block


def test_the_block_is_cut_off_and_read():
    text, levels = extract_levels("**结论**：偏多。\n站稳 3900 看 3950。\n" + BLOCK)
    assert text == "**结论**：偏多。\n站稳 3900 看 3950。"
    assert levels == Levels(XAU, "long", (LONG, SHORT))
    assert LONG.reward_r == 1.25


def test_sources_after_the_block_stay():
    text, levels = extract_levels("偏多。\n" + BLOCK + "\n\n**来源**\n- [路透](https://example.com/a)")
    assert text == "偏多。\n\n**来源**\n- [路透](https://example.com/a)" and levels is not None


def test_an_answer_without_a_block_is_left_alone():
    assert extract_levels("非农周五 21:30 公布。") == ("非农周五 21:30 公布。", None)


@pytest.mark.parametrize(
    "broken",
    [
        "<levels>not json</levels>",
        '<levels>{"inst":"XAU-USDT-SWAP","bias":"long","scenarios":[{"side":"long","trig',  # cut short by the length limit
        '<levels>["a list"]</levels>',
        '<levels>{"inst":"黄金","bias":"long","scenarios":[]}</levels>',
    ],
)
def test_a_block_that_cannot_be_read_is_still_never_shown(broken):
    text, levels = extract_levels("偏多。\n" + broken + "\n\n（内容较长，被截断了。）")
    assert levels is None and "levels" not in text and "inst" not in text
    assert text.startswith("偏多。") and text.endswith("（内容较长，被截断了。）")


def test_levels_in_the_wrong_order_are_dropped():
    rows = [
        {"side": "long", "trigger": 3900, "target": 3880, "invalid": 3860},  # target below the trigger
        {"side": "short", "trigger": 3860, "target": 3820, "invalid": 3850},  # invalidation on the wrong side
        {"side": "long", "trigger": "x", "target": 3950, "invalid": 3860},
        {"side": "long", "trigger": 3900, "target": 3950, "invalid": -1},
        {"side": "sideways", "trigger": 3900, "target": 3950, "invalid": 3860},
        {"side": "long", "trigger": 3900, "target": 3950, "invalid": 3860},
        {"side": "long", "trigger": 3900, "target": 3950, "invalid": 3860},  # said twice
    ]
    import json

    levels = parse_levels(json.dumps({"inst": "xau-usdt-swap", "bias": "LONG", "scenarios": rows}))
    assert levels == Levels(XAU, "long", (LONG,))


def test_a_direction_alone_is_kept_and_nothing_checkable_is_not():
    assert parse_levels('{"inst":"BTC-USDT-SWAP","bias":"short","scenarios":[]}') == Levels("BTC-USDT-SWAP", "short", ())
    assert parse_levels('{"inst":"BTC-USDT-SWAP","bias":"range","scenarios":[]}') is None
    assert parse_levels('{"inst":"BTC-USDT-SWAP","bias":"up","scenarios":[]}') is None


# -------------------------------------------------------------------- scoring


def bar(index: int, open_: float, high: float, low: float, close: float, confirmed: bool = True) -> Candle:
    return Candle((T0 + index * BAR_SECONDS) * 1000, open_, high, low, close, 0.0, confirmed)


def flat(index: int, price: float = 3880.0) -> Candle:
    return bar(index, price, price + 2, price - 2, price)


def call(scenario: Scenario = LONG, **overrides) -> dict:
    row = {
        "id": 1, "ts": T0, "inst_id": XAU, "side": scenario.side, "trigger_px": scenario.trigger, "target_px": scenario.target,
        "invalid_px": scenario.invalid, "status": "open", "triggered_ts": None, "closed_ts": None, "checked_ts": None, "result_r": None,
    }  # fmt: skip
    row.update(overrides)
    return row


def later(bars: int) -> float:
    return T0 + bars * BAR_SECONDS + 1


def test_a_long_that_triggers_and_reaches_its_target_is_a_hit():
    candles = [flat(0), bar(1, 3880, 3906, 3878, 3904), bar(2, 3904, 3930, 3899, 3925), bar(3, 3925, 3952, 3920, 3948)]
    result = score_call(call(), candles, later(4), 72)
    assert result["status"] == "hit" and result["result_r"] == 1.25
    assert result["triggered_ts"] == T0 + 2 * BAR_SECONDS and result["closed_ts"] == T0 + 4 * BAR_SECONDS


def test_a_wick_through_the_trigger_is_not_a_trigger():
    """"站稳" means a close beyond the level, not a touch."""
    candles = [flat(0), bar(1, 3880, 3960, 3878, 3895), flat(2, 3890)]
    result = score_call(call(), candles, later(3), 72)
    assert "status" not in result and "triggered_ts" not in result and result["checked_ts"] == T0 + 3 * BAR_SECONDS


def test_the_invalidation_level_first_is_a_failure():
    candles = [bar(0, 3880, 3906, 3878, 3904), bar(1, 3904, 3910, 3858, 3870), bar(2, 3870, 3960, 3868, 3955)]
    result = score_call(call(), candles, later(3), 72)
    assert result["status"] == "failed" and result["result_r"] == -1.0 and result["closed_ts"] == T0 + 2 * BAR_SECONDS


def test_target_and_invalidation_in_one_candle_count_as_a_failure():
    candles = [bar(0, 3880, 3906, 3878, 3904), bar(1, 3904, 3955, 3855, 3900)]
    assert score_call(call(), candles, later(2), 72)["status"] == "failed"


def test_a_close_that_jumps_past_the_target_is_a_hit():
    assert score_call(call(), [bar(0, 3880, 3962, 3878, 3958)], later(1), 72)["status"] == "hit"


def test_a_short_is_scored_the_other_way_round():
    candles = [bar(0, 3870, 3872, 3855, 3858), bar(1, 3858, 3862, 3818, 3822)]
    result = score_call(call(SHORT), candles, later(2), 72)
    assert result["status"] == "hit" and result["result_r"] == 1.0
    candles = [bar(0, 3870, 3872, 3855, 3858), bar(1, 3858, 3901, 3850, 3895)]
    assert score_call(call(SHORT), candles, later(2), 72)["status"] == "failed"


def test_only_whole_closed_candles_after_the_analysis_count():
    row = call(ts=T0 + 60)  # the analysis was written a minute into candle 0
    before = bar(-1, 3880, 3960, 3850, 3955)
    partial = bar(0, 3880, 3960, 3878, 3955)  # began before the analysis
    unfinished = bar(1, 3880, 3960, 3878, 3955, confirmed=False)
    assert score_call(row, [before, partial, unfinished], later(1), 72) == {}


def test_progress_is_kept_and_picked_up_again():
    first = score_call(call(), [flat(0), bar(1, 3880, 3906, 3878, 3904)], later(2), 72)
    assert first == {"checked_ts": T0 + 2 * BAR_SECONDS, "triggered_ts": T0 + 2 * BAR_SECONDS}
    # Next run: the old candles are seen again and must not be counted twice.
    again = [flat(0), bar(1, 3880, 3906, 3878, 3850), bar(2, 3904, 3952, 3899, 3950)]
    assert score_call(call(**first), again, later(3), 72)["status"] == "hit"


def test_never_triggered_within_the_window():
    candles = [flat(index) for index in range(16)]  # four hours
    result = score_call(call(), candles, later(17), 4)
    assert result["status"] == "untriggered" and result["closed_ts"] == T0 + 4 * 3600 and "result_r" not in result


def test_triggered_but_undecided_is_valued_at_the_end_of_the_window():
    candles = [bar(0, 3880, 3906, 3878, 3904)] + [bar(index, 3910, 3922, 3905, 3920) for index in range(1, 16)]
    result = score_call(call(), candles, later(17), 4)
    assert result["status"] == "expired" and result["result_r"] == 0.5  # (3920 - 3900) / 40
    assert score_call(call(), candles, later(16) - 2, 4).get("status") is None  # the window is not over yet


def test_missing_candles_are_not_guessed_over():
    # The bot was off: OKX's candles begin long after what was last checked.
    result = score_call(call(checked_ts=T0 + 4 * BAR_SECONDS), [flat(40), flat(41)], later(42), 72)
    assert result["status"] == "unscored"
    # The whole window passed unseen.
    assert score_call(call(), [flat(400)], later(401), 72)["status"] == "unscored"


def test_price_at_reads_the_candle_that_contains_the_moment():
    candles = [flat(0, 3880), flat(1, 3890), bar(2, 3890, 3900, 3888, 3899, confirmed=False)]
    assert price_at(candles, T0 + BAR_SECONDS + 5) == 3890.0
    assert price_at(candles, T0 + 2 * BAR_SECONDS + 5) is None and price_at(candles, T0 - 5) is None


# ------------------------------------------------------- journal and the scorer


class Market:
    def __init__(self, candles: dict[str, list[Candle]]):
        self.data = candles
        self.requests: list[tuple[str, str, int]] = []
        self.error: Exception | None = None

    def candles(self, inst_id: str, bar: str = "1H", limit: int = 200) -> list[Candle]:
        self.requests.append((inst_id, bar, limit))
        if self.error:
            raise self.error
        return self.data.get(inst_id, [])


def scenarios(*items: Scenario) -> list[dict]:
    return [{"side": s.side, "trigger": s.trigger, "target": s.target, "invalid": s.invalid} for s in items]


def test_the_scorer_works_through_the_journal():
    journal = Journal(":memory:")
    journal.record_analysis("question", XAU, "long", 3880.0, scenarios(LONG, SHORT), "黄金怎么看", ts=T0)
    journal.record_analysis("question", "BTC-USDT-SWAP", "short", 65000.0, [], ts=T0)
    market = Market({XAU: [flat(0), bar(1, 3880, 3906, 3878, 3904), bar(2, 3904, 3952, 3899, 3950)]})
    clock = Clock(later(3))
    scorer = Scorer(journal, market, 72, clock)

    assert scorer.run_once() == 1
    assert market.requests == [(XAU, "15m", 300), ("BTC-USDT-SWAP", "15m", 300)]  # one read per instrument
    rows = journal.calls_between(T0, T0 + 1)
    assert [(row["side"], row["status"], row["result_r"]) for row in rows] == [("long", "hit", 1.25), ("short", "open", None)]
    assert [row["side"] for row in journal.open_calls()] == ["short"]
    assert scorer.run_once() == 0  # nothing new, nothing counted twice


def test_the_direction_is_compared_with_the_price_a_day_and_three_days_later():
    journal = Journal(":memory:")
    journal.record_analysis("question", XAU, "long", 3880.0, [], ts=T0 + 60)
    day = 24 * 3600 // BAR_SECONDS
    market = Market({XAU: [flat(index, 3880.0 + index) for index in range(day + 2)]})
    clock = Clock(later(day + 2))
    scorer = Scorer(journal, market, 72, clock)
    scorer.run_once()
    [row] = journal.analyses_between(T0, T0 + 3600)
    assert row["px_24h"] == 3880.0 + day and row["px_72h"] is None and row["scored"] == 0

    # Three days on, OKX's candles no longer reach back that far: the 72-hour figure stays empty, and the waiting ends.
    clock.now = later(10 * day)
    market.data[XAU] = [flat(index, 4000.0) for index in range(9 * day, 10 * day)]
    scorer.run_once()
    [row] = journal.analyses_between(T0, T0 + 3600)
    assert row["px_72h"] is None and row["scored"] == 1 and journal.unscored_analyses() == []


def test_a_market_that_cannot_be_read_changes_nothing():
    journal = Journal(":memory:")
    journal.record_analysis("question", XAU, "long", 3880.0, scenarios(LONG), ts=T0)
    market = Market({})
    market.error = OkxError("51001", "Instrument ID does not exist")
    assert Scorer(journal, market, 72, Clock(later(500))).run_once() == 0
    assert [row["status"] for row in journal.open_calls()] == ["open"]


# ---------------------------------------------------------------------- the card


def test_the_card_before_anything_is_recorded_says_how_it_works():
    text = scorecard_markdown(Journal(":memory:"), T0, UTC, 30, XAU)
    assert "最近 30 天 · XAU-USDT-SWAP" in text and "还没有记录到带点位的分析" in text and "72 小时" in text


def test_the_card_counts_what_happened_and_says_how_it_was_judged():
    journal = Journal(":memory:")
    first = journal.record_analysis("question", XAU, "long", 3880.0, scenarios(LONG, SHORT), ts=T0)
    second = journal.record_analysis("morning brief", XAU, "short", 3900.0, scenarios(SHORT, LONG), ts=T0 + 3600)
    journal.record_analysis("question", "BTC-USDT-SWAP", "long", 65000.0, [], ts=T0 + 7200)
    by_analysis = {(row["analysis_id"], row["side"]): row["id"] for row in journal.calls_between(T0, T0 + 86400)}
    journal.update_call(by_analysis[(first, "long")], status="hit", result_r=1.25, closed_ts=T0 + 9000, triggered_ts=T0 + 900)
    journal.update_call(by_analysis[(first, "short")], status="untriggered", closed_ts=T0 + 72 * 3600)
    journal.update_call(by_analysis[(second, "short")], status="failed", result_r=-1.0, closed_ts=T0 + 12000)
    journal.update_call(by_analysis[(second, "long")], status="expired", result_r=0.35, closed_ts=T0 + 76 * 3600)
    journal.update_analysis(first, px_24h=3920.0, scored=0)
    journal.update_analysis(second, px_24h=3930.0, px_72h=3890.0, scored=1)

    text = scorecard_markdown(journal, T0 + 80 * 3600, UTC, 30)
    for expected in (
        "记录了 3 次带点位的分析，共 4 个情景。",
        "有结果的 2 个：命中 1 个，失效 1 个，命中率 50%",
        "平均每个触发的情景 +0.20R（命中时平均 +1.25R，失效按 -1R，到期的按到期时的价格算）",
        "到期没到目标也没到失效位 1 个；没触发 1 个",
        "24 小时后：给过方向的 2 次里对了 1 次（50%）。同样这 2 次里价格上涨的有 2 次（100%）",
        "72 小时后：给过方向的 1 次里对了 1 次（100%）",
        "多头：触发 3,900，目标 3,950，失效 3,860，结果命中（+1.25R）",
        "结果到期（+0.35R）",
        "15 分钟 K 线收盘站上（空头是跌破）触发位才算触发",
        "有结果的情景还不到 20 个",
        "不是交易记录，也不代表收益",
    ):
        assert expected in text, expected
    only_btc = scorecard_markdown(journal, T0 + 80 * 3600, UTC, 30, "BTC-USDT-SWAP")
    assert "记录了 1 次带点位的分析，共 0 个情景" in only_btc and "还没有走出结果的情景" in only_btc
    assert "还没有记录" in scorecard_markdown(journal, T0 + 80 * 86400, UTC, 30)  # all of it is older than 30 days


# ------------------------------------------------------------------ the analyst


def kept_analyst(text: str, **cfg):
    kept: list[tuple[str, str, Levels]] = []
    client = FakeClient(beta=[message(text), message(text)])
    bot = Analyst(make_cfg(**cfg), client, on_levels=lambda purpose, question, levels: kept.append((purpose, question, levels)))
    return bot, client, kept


def test_an_analysis_is_asked_for_its_levels_and_shown_without_them():
    bot, client, kept = kept_analyst("**结论**：偏多。\n" + BLOCK)
    assert bot.answer("黄金怎么看", "最新价：3,880.00", None, in_group=False) == "**结论**：偏多。"
    assert client.beta.messages.calls[0]["system"] == ANALYST_SYSTEM + LEVELS_NOTE
    assert kept == [("question", "黄金怎么看", Levels(XAU, "long", (LONG, SHORT)))]


def test_only_the_owners_version_of_a_brief_is_kept():
    bot, client, kept = kept_analyst("今天偏多。\n" + BLOCK)
    assert bot.brief("morning", "行情", "账户", None, for_owner=True) == "今天偏多。"
    assert bot.brief("morning", "行情", None, None, for_owner=False) == "今天偏多。"  # cut off all the same
    assert [purpose for purpose, _, _ in kept] == ["morning brief"]
    assert client.beta.messages.calls[1]["system"] == ANALYST_SYSTEM


def test_with_the_track_record_switched_off_nothing_is_asked_or_kept():
    bot, client, kept = kept_analyst("偏多。", track_record=False)
    bot.answer("黄金怎么看", "行情", None, in_group=False)
    assert client.beta.messages.calls[0]["system"] == ANALYST_SYSTEM and kept == []


def test_a_failure_to_keep_the_levels_never_costs_the_answer():
    client = FakeClient(beta=[message("偏多。\n" + BLOCK)])

    def broken(purpose, question, levels):
        raise RuntimeError("disk full")

    assert Analyst(make_cfg(), client, on_levels=broken).answer("黄金怎么看", "行情", None, in_group=False) == "偏多。"


def test_the_weekly_review_carries_no_levels_note():
    client = FakeClient(beta=[message("本周复盘。")])
    Analyst(make_cfg(), client, on_levels=lambda *args: None).weekly_review("行情", "记录", "权益")
    assert LEVELS_NOTE not in client.beta.messages.calls[0]["system"]
