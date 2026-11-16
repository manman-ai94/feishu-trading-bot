"""US rates and the dollar from FRED: read as public CSV, shown as background with their dates."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

from bot.analyst import ANALYST_SYSTEM
from bot.app import build_ai, build_macro
from bot.config import load_config
from bot.instruments import InstrumentResolver
from bot.journal import Journal
from bot.macro import FRED_CSV, MACRO_NOTE, RETRY_SECONDS, SERIES, TTL_SECONDS, Macro, Series, parse_csv
from bot.service import Reporter
from bot.tools import Toolbox

from .helpers import Clock, FakeAnalyst, FakeFeishu, FakeOkx, make_cfg

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp()


def csv_text(series_id: str, rows: list[tuple[str, str]]) -> str:
    return "observation_date," + series_id + "\n" + "".join(f"{day},{value}\n" for day, value in rows)


# The shape FRED answers in: newest last, a "." or nothing on a day without a value.
REAL_YIELD = [("2026-10-13", "1.95"), ("2026-11-06", "1.90"), ("2026-11-09", "."), ("2026-11-12", "1.83"), ("2026-11-13", "1.80")]
DOLLAR = [("2026-10-13", "120.00"), ("2026-11-06", "121.20"), ("2026-11-13", "121.50")]
ANSWERS = {
    "DFII10": csv_text("DFII10", REAL_YIELD),
    "DGS10": csv_text("DGS10", [("2026-11-13", "4.12")]),
    "T10YIE": csv_text("T10YIE", [("2026-11-13", "2.32"), ("2026-11-14", "")]),
    "DTWEXBGS": csv_text("DTWEXBGS", DOLLAR),
}


class Response:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeFred:
    """Stands in for requests.get: answers each series from `answers`, and records what was asked."""

    def __init__(self, answers=None, fail: set[str] = frozenset()):
        self.answers = dict(ANSWERS if answers is None else answers)
        self.fail = set(fail)
        self.calls: list[tuple[str, dict, float]] = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        series_id = params["id"]
        if series_id in self.fail:
            raise requests.ConnectionError("refused")
        return Response(self.answers.get(series_id, ""))


def macro(fake: FakeFred | None = None, now: float = NOW) -> tuple[Macro, FakeFred, Clock]:
    fake = fake or FakeFred()
    clock = Clock(now)
    return Macro(get=fake, clock=clock), fake, clock


# -------------------------------------------------------------------- reading


def test_the_csv_skips_its_header_and_days_without_a_value_and_comes_out_oldest_first():
    text = "observation_date,DFII10\n2026-11-13,1.80\n2026-11-09,.\n2026-11-12,1.83\n2026-11-14,\nnot,a row\n\n"
    assert parse_csv(text) == ((date(2026, 11, 12), 1.83), (date(2026, 11, 13), 1.80))
    assert parse_csv("") == () and parse_csv("<html>error</html>") == ()


def test_a_line_carries_the_date_and_the_change_over_a_week_and_a_month():
    real = Series("DFII10", "实际利率", "%", 2, parse_csv(ANSWERS["DFII10"]))
    assert real.latest() == (date(2026, 11, 13), 1.80)
    assert real.back(7) == (date(2026, 11, 6), 1.90)  # the last value on or before a week earlier
    assert real.back(30) == (date(2026, 10, 13), 1.95)
    assert real.back(60) is None
    assert real.line() == "实际利率：1.80%（2026-11-13），比一周前-10 个基点，比一个月前-15 个基点"

    dollar = Series("DTWEXBGS", "美元", "", 2, parse_csv(ANSWERS["DTWEXBGS"]))
    assert dollar.line() == "美元：121.50（2026-11-13），比一周前+0.2%，比一个月前+1.2%"

    short = Series("DGS10", "国债", "%", 2, parse_csv(ANSWERS["DGS10"]))
    assert short.line() == "国债：4.12%（2026-11-13）"
    assert Series("X", "空", "%", 2, ()).line() == "空：没有取到"


def test_all_four_series_are_read_from_the_public_csv_without_a_key():
    source, fake, _ = macro()
    text = source.text()
    assert [params["id"] for _, params, _ in fake.calls] == [series_id for series_id, *_ in SERIES]
    assert all(url == FRED_CSV and timeout for url, _, timeout in fake.calls)
    assert all(set(params) == {"id", "cosd"} for _, params, _ in fake.calls)  # no api_key anywhere
    assert fake.calls[0][1]["cosd"] == "2026-09-07"  # 70 days back
    lines = text.splitlines()
    assert lines[0].startswith("美国 10 年期实际利率 (TIPS real yield)：1.80%（2026-11-13）")
    assert lines[1] == "美国 10 年期国债收益率：4.12%（2026-11-13）"
    assert lines[2] == "10 年期盈亏平衡通胀率 (breakeven inflation)：2.32%（2026-11-13）"
    assert lines[3].startswith("美元广义指数 (broad dollar index)：121.50（2026-11-13）")
    assert lines[-1] == MACRO_NOTE


def test_it_reads_once_an_hour():
    source, fake, clock = macro()
    first = source.text()
    clock.advance(TTL_SECONDS - 1)
    assert source.text() == first and len(fake.calls) == len(SERIES)
    clock.advance(2)
    source.text()
    assert len(fake.calls) == 2 * len(SERIES)


def test_a_series_that_cannot_be_read_is_left_out_and_the_rest_still_shown():
    source, _, _ = macro(FakeFred(fail={"DGS10"}, answers={**ANSWERS, "T10YIE": "<html>busy</html>"}))
    text = source.text()
    assert "实际利率" in text and "美元广义指数" in text
    assert "国债收益率" not in text and "盈亏平衡" not in text


def test_when_fred_is_down_it_says_nothing_and_waits_before_trying_again():
    fake = FakeFred(fail={series_id for series_id, *_ in SERIES})
    source, _, clock = macro(fake)
    assert source.text() == ""
    clock.advance(RETRY_SECONDS - 1)
    assert source.text() == "" and len(fake.calls) == len(SERIES)  # not asked again yet
    fake.fail.clear()
    clock.advance(2)
    assert "实际利率" in source.text()


def test_an_error_page_counts_as_not_read():
    source, _, _ = macro(FakeFred(answers={series_id: "" for series_id, *_ in SERIES}))
    assert source.text() == ""

    class Refusing(FakeFred):
        def __call__(self, url, params=None, timeout=None):
            self.calls.append((url, dict(params or {}), timeout))
            return Response("Forbidden", 403)

    source, _, _ = macro(Refusing())
    assert source.text() == ""


# ---------------------------------------------------------------- where it is used


def toolbox(source):
    okx = FakeOkx()
    return Toolbox(okx, InstrumentResolver(okx), None, LA, None, source)


def test_the_ai_gets_the_tool_only_when_macro_data_is_on():
    source, _, _ = macro()
    assert [tool["name"] for tool in toolbox(source).definitions(2)] == ["get_market", "get_candles", "get_macro"]
    assert "get_macro" not in [tool["name"] for tool in toolbox(None).definitions(2)]
    text, is_error = toolbox(None).run("get_macro", {})
    assert is_error and "没有打开" in text


def test_the_tool_gives_the_lines_or_says_it_could_not_read_them():
    source, _, _ = macro()
    text, is_error = toolbox(source).run("get_macro", {})
    assert not is_error and text == source.text()

    down, _, _ = macro(FakeFred(fail={series_id for series_id, *_ in SERIES}))
    text, is_error = toolbox(down).run("get_macro", {})
    assert is_error and "没有从 FRED 读到" in text


def test_the_instructions_call_it_background_with_a_date():
    assert "get_macro" in ANALYST_SYSTEM
    assert "不当点位的依据" in ANALYST_SYSTEM and "带上日期" in ANALYST_SYSTEM


def reporter(source):
    cfg, okx = make_cfg(), FakeOkx()
    return Reporter(cfg, FakeFeishu(), okx, FakeAnalyst(), Journal(":memory:"), Clock(1_800_000_000.0), macro=source)


def test_a_brief_is_written_with_rates_and_the_dollar_in_view():
    source, _, _ = macro()
    text = reporter(source).market_text()
    assert "品种：XAU-USDT-SWAP" in text and "美国利率和美元（FRED）：\n美国 10 年期实际利率" in text
    assert text.endswith(MACRO_NOTE)
    assert "FRED" not in reporter(None).market_text()


def test_a_brief_is_never_held_up_by_the_rates():
    class Broken:
        def text(self):
            raise RuntimeError("bug")

    text = reporter(Broken()).market_text()
    assert "品种：XAU-USDT-SWAP" in text and "FRED" not in text


def test_macro_data_is_on_by_default_and_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.delenv("MACRO_DATA", raising=False)
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    cfg, _ = load_config(env)
    assert cfg.macro_data is True and isinstance(build_macro(cfg), Macro)
    env.write_text("MACRO_DATA=false\n", encoding="utf-8")
    cfg, _ = load_config(env)
    monkeypatch.delenv("MACRO_DATA", raising=False)
    assert cfg.macro_data is False and build_macro(cfg) is None

    cfg = make_cfg(knowledge_dir=tmp_path)
    analyst, _, _ = build_ai(cfg, FakeOkx(), Journal(":memory:"), macro=build_macro(cfg))
    assert "get_macro" in [tool["name"] for tool in analyst._toolbox.definitions(2)]
