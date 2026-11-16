"""How the crowd is positioned: read from two public sources, shown as background only."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from bot.analyst import ANALYST_SYSTEM
from bot.app import build_ai, build_crowd
from bot.config import load_config
from bot.crowd import COT_RETRY_SECONDS, COT_TTL_SECONDS, COT_URL, CROWD_NOTE, RATIO_TTL_SECONDS, Crowd, long_share, parse_cot
from bot.instruments import InstrumentResolver
from bot.journal import Journal
from bot.okx_client import OkxError, OkxNetworkError
from bot.service import Reporter
from bot.tools import Toolbox

from .helpers import Clock, FakeAnalyst, FakeFeishu, FakeOkx, make_cfg
from .test_okx_client import client, ok

LA = ZoneInfo("America/Los_Angeles")
HOUR = 3600 * 1000
NEWEST = 1_794_798_000_000  # 2026-11-16 03:00 UTC, 19:00 the evening before in Los Angeles

# What the CFTC answers, newest first (the fields the bot reads, and one it does not).
COT_ROWS = [
    {
        "report_date_as_yyyy_mm_dd": "2026-11-03T00:00:00.000", "m_money_positions_long_all": "122283",
        "m_money_positions_short_all": "18669", "change_in_m_money_long_all": "-225", "change_in_m_money_short_all": "1723",
        "open_interest_all": "399169", "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
    },
    {
        "report_date_as_yyyy_mm_dd": "2026-10-27T00:00:00.000", "m_money_positions_long_all": "122508",
        "m_money_positions_short_all": "16946",
    },
]  # fmt: skip


def series(newest_ratio: float, day_ago_ratio: float, hours: int = 30) -> list[tuple[int, float]]:
    """Hourly rows, newest first: one value now, another from 24 hours back on."""
    return [(NEWEST - index * HOUR, newest_ratio if index < 24 else day_ago_ratio) for index in range(hours)]


class CrowdOkx(FakeOkx):
    def __init__(self):
        super().__init__()
        self.ratios = {"account": series(3.378, 2.5), "position": series(0.756, 0.9)}
        self.ratio_error: Exception | None = None
        self.ratio_reads: list[tuple[str, str, str]] = []

    def top_trader_ratio(self, inst_id, by="account", period="1H"):
        self.ratio_reads.append((inst_id, by, period))
        if self.ratio_error:
            raise self.ratio_error
        return list(self.ratios[by])


class Cftc:
    def __init__(self, rows=COT_ROWS):
        self.rows = rows
        self.error: Exception | None = None
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url, params):
        self.requests.append((url, params))
        if self.error:
            raise self.error
        return self.rows


def crowd(rows=COT_ROWS):
    okx, cftc, clock = CrowdOkx(), Cftc(rows), Clock(1_800_000_000.0)
    return Crowd(okx, LA, fetch_json=cftc, clock=clock), okx, cftc, clock


# ------------------------------------------------------------------ the numbers


def test_a_ratio_is_also_shown_as_the_share_that_is_long():
    assert long_share(3.0) == 75.0 and long_share(1.0) == 50.0 and round(long_share(0.756), 1) == 43.1


def test_top_traders_by_accounts_and_by_positions_now_and_a_day_ago():
    source, okx, _, _ = crowd()
    top = source.top_traders("XAU-USDT-SWAP")
    assert (top.account_ratio, top.account_ratio_before) == (3.378, 2.5)
    assert (top.position_ratio, top.position_ratio_before) == (0.756, 0.9)
    assert top.ts == NEWEST and okx.ratio_reads == [("XAU-USDT-SWAP", "account", "1H"), ("XAU-USDT-SWAP", "position", "1H")]


def test_a_gap_a_day_ago_uses_the_hour_before_it_and_a_short_history_gives_none():
    source, okx, _, _ = crowd()
    okx.ratios["account"] = [row for row in series(3.0, 2.0) if row[0] != NEWEST - 24 * HOUR]  # that hour is missing
    okx.ratios["position"] = series(0.8, 0.8, hours=10)  # OKX has only ten hours of it
    top = source.top_traders("XAU-USDT-SWAP")
    assert top.account_ratio_before == 2.0 and top.position_ratio_before is None


def test_only_contracts_have_top_trader_ratios():
    source, okx, _, _ = crowd()
    assert source.top_traders("BTC-USDT") is None and okx.ratio_reads == []
    assert source.lines("BTC-USDT") == [] and source.text("BTC-USDT") == ""


def test_one_ratio_failing_leaves_the_other_and_both_failing_leave_nothing():
    source, okx, _, _ = crowd()
    okx.ratios["position"] = []
    top = source.top_traders("BTC-USDT-SWAP")
    assert top.account_ratio == 3.378 and top.position_ratio is None
    assert any("按账户数" in line for line in source.lines("BTC-USDT-SWAP"))
    assert not any("按持仓量" in line for line in source.lines("BTC-USDT-SWAP"))

    for error in (OkxError("51000", "Parameter instId error"), OkxNetworkError("连不上 OKX")):
        source, okx, _, _ = crowd()
        okx.ratio_error = error
        assert source.top_traders("ETH-USDT-SWAP") is None and source.text("ETH-USDT-SWAP") == ""


def test_ratios_are_read_again_only_after_five_minutes():
    source, okx, _, clock = crowd()
    source.top_traders("XAU-USDT-SWAP")
    source.top_traders("XAU-USDT-SWAP")
    assert len(okx.ratio_reads) == 2
    clock.advance(RATIO_TTL_SECONDS + 1)
    source.top_traders("XAU-USDT-SWAP")
    assert len(okx.ratio_reads) == 4


# -------------------------------------------------------------------- the CFTC


def test_the_weekly_report_is_read_for_gold_only():
    source, _, cftc, _ = crowd()
    report = source.cot("XAU-USDT-SWAP")
    assert (report.report_date, report.mm_long, report.mm_short, report.net) == ("2026-11-03", 122283.0, 18669.0, 103614.0)
    assert report.net_change == -1948.0 and report.open_interest == 399169.0
    [(url, params)] = cftc.requests
    assert url == COT_URL and params["cftc_contract_market_code"] == "088691" and params["$limit"] == 2
    assert source.cot("BTC-USDT-SWAP") is None and len(cftc.requests) == 1  # no futures market is mapped to it


def test_the_report_is_kept_for_hours_and_a_failure_is_not_retried_at_once():
    source, _, cftc, clock = crowd()
    source.cot("XAU-USDT-SWAP")
    clock.advance(COT_TTL_SECONDS - 1)
    source.cot("XAU-USDT-SWAP")
    assert len(cftc.requests) == 1
    clock.advance(2)
    source.cot("XAU-USDT-SWAP")
    assert len(cftc.requests) == 2

    source, _, cftc, clock = crowd()
    cftc.error = TimeoutError("slow")  # a public site being down never becomes the bot's problem
    assert source.cot("XAU-USDT-SWAP") is None and source.cot("XAU-USDT-SWAP") is None
    assert len(cftc.requests) == 1
    cftc.error = None
    clock.advance(COT_RETRY_SECONDS + 1)
    assert source.cot("XAU-USDT-SWAP").report_date == "2026-11-03"


def test_parse_cot_takes_the_newest_usable_row():
    assert parse_cot(list(reversed(COT_ROWS)), "COMEX 黄金期货").report_date == "2026-11-03"
    older = parse_cot(COT_ROWS[1:], "COMEX 黄金期货")
    assert older.report_date == "2026-10-27" and older.net_change is None and older.open_interest is None
    for junk in ([], [{}], ["text"], [{"report_date_as_yyyy_mm_dd": "2026-11-03", "m_money_positions_long_all": "n/a"}]):
        assert parse_cot(junk, "COMEX 黄金期货") is None


# -------------------------------------------------------------------- the text


def test_the_text_carries_the_time_of_every_figure_and_what_it_is_for():
    source, _, _, _ = crowd()
    text = source.text("XAU-USDT-SWAP")
    assert text.splitlines() == [
        "OKX 大户多空比（XAU-USDT-SWAP，按小时，数据时间 11-15 19:00）",
        "- 按账户数：多空比 3.38（多头占 77.2%），24 小时前 2.50（71.4%）",
        "- 按持仓量：多空比 0.76（多头占 43.1%），24 小时前 0.90（47.4%）",
        "CFTC 持仓周报（COMEX 黄金期货，统计到 2026-11-03，每周五公布）",
        "- 基金经理 (managed money)：多头 122,283 张，空头 18,669 张，净多 103,614 张，比前一周 -1,948 张",
        CROWD_NOTE,
    ]
    assert "不是点位的依据" in CROWD_NOTE


def test_without_the_cftc_only_the_ratios_are_shown():
    source, _, cftc, _ = crowd()
    cftc.error = ConnectionError("down")
    lines = source.lines("XAU-USDT-SWAP")
    assert len(lines) == 3 and not any("CFTC" in line for line in lines)


# ------------------------------------------------------------ where it is used


def toolbox(source):
    okx = FakeOkx()
    return Toolbox(okx, InstrumentResolver(okx), None, LA, source)


def test_the_ai_gets_the_tool_only_when_crowd_data_is_on():
    source, _, _, _ = crowd()
    assert [tool["name"] for tool in toolbox(source).definitions(2)] == ["get_market", "get_candles", "get_positioning"]
    assert "get_positioning" not in [tool["name"] for tool in toolbox(None).definitions(2)]
    text, is_error = toolbox(None).run("get_positioning", {"symbol": "黄金"})
    assert is_error and "没有打开" in text


def test_the_tool_answers_by_ticker_or_name_and_says_when_there_is_nothing():
    source, okx, _, _ = crowd()
    text, is_error = toolbox(source).run("get_positioning", {"symbol": "黄金"})
    assert not is_error and text == source.text("XAU-USDT-SWAP") and okx.ratio_reads[0][0] == "XAU-USDT-SWAP"
    text, is_error = toolbox(source).run("get_positioning", {"symbol": "ETH-USDT"})  # spot
    assert is_error and "没有群体持仓数据" in text
    text, is_error = toolbox(source).run("get_positioning", {"symbol": "AAPL"})
    assert is_error and "没有找到" in text


def test_the_instructions_call_it_background_not_a_basis_for_levels():
    assert "get_positioning" in ANALYST_SYSTEM
    assert "不能当作点位的依据" in ANALYST_SYSTEM and "也不能单凭它判断方向" in ANALYST_SYSTEM


def reporter(source):
    cfg, okx = make_cfg(), FakeOkx()
    return Reporter(cfg, FakeFeishu(), okx, FakeAnalyst(), Journal(":memory:"), Clock(1_800_000_000.0), crowd=source)


def test_a_brief_is_written_with_the_crowd_in_view():
    source, _, _, _ = crowd()
    text = reporter(source).market_text()
    assert "品种：XAU-USDT-SWAP" in text and "OKX 大户多空比（XAU-USDT-SWAP" in text and text.endswith(CROWD_NOTE)
    assert "大户多空比" not in reporter(None).market_text()


def test_a_brief_is_never_held_up_by_the_crowd_data():
    class Broken:
        def text(self, inst_id):
            raise RuntimeError("bug")

    text = reporter(Broken()).market_text()
    assert "品种：XAU-USDT-SWAP" in text and "大户" not in text


def test_crowd_data_is_on_by_default_and_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.delenv("CROWD_DATA", raising=False)
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    cfg, _ = load_config(env)
    assert cfg.crowd_data is True and isinstance(build_crowd(cfg, FakeOkx()), Crowd)
    env.write_text("CROWD_DATA=false\n", encoding="utf-8")
    cfg, _ = load_config(env)
    monkeypatch.delenv("CROWD_DATA", raising=False)
    assert cfg.crowd_data is False and build_crowd(cfg, FakeOkx()) is None

    cfg = make_cfg(knowledge_dir=tmp_path)
    analyst, _, _ = build_ai(cfg, FakeOkx(), Journal(":memory:"), build_crowd(cfg, FakeOkx()))
    assert "get_positioning" in [tool["name"] for tool in analyst._toolbox.definitions(2)]


# ------------------------------------------------------------- the OKX requests


def test_top_trader_ratios_are_public_requests_sorted_newest_first():
    path = "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader"
    okx, session = client({path: [ok([["1794794400000", "3.2"], ["1794798000000", "3.3782383419689119"], ["bad"], ["x", "y"]])]})
    assert okx.top_trader_ratio("XAU-USDT-SWAP") == [(1794798000000, 3.3782383419689119), (1794794400000, 3.2)]
    url, headers = session.requests[0]
    assert url == f"https://okx.test{path}?instId=XAU-USDT-SWAP&period=1H" and "OK-ACCESS-KEY" not in headers

    path = "/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader"
    okx, session = client({path: [ok([["1794798000000", "0.7559333454624208"]])]})
    assert okx.top_trader_ratio("XAU-USDT-SWAP", "position", "4H") == [(1794798000000, 0.7559333454624208)]
    assert session.requests[0][0].endswith("?instId=XAU-USDT-SWAP&period=4H")
