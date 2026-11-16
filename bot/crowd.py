"""How the crowd is positioned: background for an analysis, never a signal.

Two public sources, neither needs a key:

  * OKX publishes, for every contract, the long/short ratio of its top
    traders: by the number of accounts on each side, and by the size of the
    positions on each side. The two can point in opposite directions (many
    small longs against a few large shorts), which is why both are shown.
  * The CFTC publishes every Friday what each kind of trader held in COMEX
    gold futures on the Tuesday before ("Commitments of Traders"). The line
    kept here is managed money, the funds that trade the trend.

Neither says where price goes next. Every line carries the time of its data,
and the AI is told to use it as context for risk, not as the basis of a level.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import requests

from .models import to_float
from .okx_client import OkxError, OkxNetworkError

log = logging.getLogger(__name__)

PERIOD = "1H"
DAY_MS = 24 * 3600 * 1000
RATIO_TTL_SECONDS = 5 * 60
COT_TTL_SECONDS = 6 * 3600
COT_RETRY_SECONDS = 30 * 60  # after a failed read, do not ask again for this long
COT_TIMEOUT = 10
COT_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"  # disaggregated report, futures only
# CFTC contract market codes, by the first part of the OKX instrument id.
COT_MARKETS = {"XAU": ("088691", "COMEX 黄金期货")}

CROWD_NOTE = "以上是群体持仓，只说明别人现在怎么站队，是背景，不是点位的依据。"


def long_share(ratio: float) -> float:
    """A long/short ratio as the share that is long, in per cent: 3.0 -> 75.0."""
    return ratio / (1.0 + ratio) * 100.0


@dataclass(frozen=True)
class TopTraders:
    inst_id: str
    ts: int  # ms, the start of the newest hour
    account_ratio: float | None
    account_ratio_before: float | None  # 24 hours earlier
    position_ratio: float | None
    position_ratio_before: float | None


@dataclass(frozen=True)
class CotReport:
    market: str
    report_date: str  # YYYY-MM-DD, the Tuesday the positions were counted
    mm_long: float
    mm_short: float
    change_long: float | None  # against the week before
    change_short: float | None
    open_interest: float | None

    @property
    def net(self) -> float:
        return self.mm_long - self.mm_short

    @property
    def net_change(self) -> float | None:
        if self.change_long is None or self.change_short is None:
            return None
        return self.change_long - self.change_short


def _get_json(url: str, params: dict) -> list:
    response = requests.get(url, params=params, timeout=COT_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def parse_cot(rows: list, market: str) -> CotReport | None:
    """The newest usable row of the CFTC's answer."""
    usable = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get("report_date_as_yyyy_mm_dd") or "")[:10]
        longs, shorts = to_float(row.get("m_money_positions_long_all")), to_float(row.get("m_money_positions_short_all"))
        if len(day) == 10 and longs is not None and shorts is not None:
            usable.append((day, longs, shorts, row))
    if not usable:
        return None
    day, longs, shorts, row = max(usable, key=lambda item: item[0])
    return CotReport(
        market=market,
        report_date=day,
        mm_long=longs,
        mm_short=shorts,
        change_long=to_float(row.get("change_in_m_money_long_all")),
        change_short=to_float(row.get("change_in_m_money_short_all")),
        open_interest=to_float(row.get("open_interest_all")),
    )


def _at_or_before(rows: list[tuple[int, float]], moment: int) -> float | None:
    """The value of the newest row that is not later than `moment`. Rows are newest first."""
    for ts, value in rows:
        if ts <= moment:
            return value
    return None


def _signed(value: float) -> str:
    return f"{value:+,.0f}"


def _ratio_line(name: str, ratio: float, before: float | None) -> str:
    text = f"- {name} {ratio:.2f}（多头占 {long_share(ratio):.1f}%）"
    if before is not None:
        text += f"，24 小时前 {before:.2f}（{long_share(before):.1f}%）"
    return text


class Crowd:
    def __init__(
        self,
        okx,
        tz,
        fetch_json: Callable[[str, dict], list] = _get_json,
        clock: Callable[[], float] = time.time,
    ):
        self._okx = okx
        self._tz = tz
        self._fetch_json = fetch_json
        self._clock = clock
        self._lock = threading.Lock()
        self._ratios: dict[str, tuple[float, TopTraders | None]] = {}
        self._cot: dict[str, tuple[float, CotReport | None]] = {}

    # ------------------------------------------------------------- top traders

    def top_traders(self, inst_id: str) -> TopTraders | None:
        """None for anything that is not a contract, or when OKX has no figures for it."""
        if len(inst_id.split("-")) < 3:
            return None
        now = self._clock()
        with self._lock:
            cached = self._ratios.get(inst_id)
        if cached and now - cached[0] < RATIO_TTL_SECONDS:
            return cached[1]
        result = self._read_top_traders(inst_id)
        with self._lock:
            self._ratios[inst_id] = (now, result)
        return result

    def _read_top_traders(self, inst_id: str) -> TopTraders | None:
        series = {}
        for by in ("account", "position"):
            try:
                series[by] = self._okx.top_trader_ratio(inst_id, by, PERIOD)
            except (OkxError, OkxNetworkError) as exc:
                log.info("top-trader %s ratio of %s not read: %s", by, inst_id, exc)
                series[by] = []
        newest = max((rows[0][0] for rows in series.values() if rows), default=0)
        if not newest:
            return None

        def now_and_before(rows: list[tuple[int, float]]) -> tuple[float | None, float | None]:
            if not rows:
                return None, None
            return rows[0][1], _at_or_before(rows, rows[0][0] - DAY_MS)

        account, account_before = now_and_before(series["account"])
        position, position_before = now_and_before(series["position"])
        return TopTraders(inst_id, newest, account, account_before, position, position_before)

    # --------------------------------------------------------------------- COT

    def cot(self, inst_id: str) -> CotReport | None:
        """The latest weekly report for the futures market behind this instrument. None when there is none."""
        market = COT_MARKETS.get(inst_id.split("-")[0].upper())
        if market is None:
            return None
        code, name = market
        now = self._clock()
        with self._lock:
            cached = self._cot.get(code)
        if cached and now < cached[0]:
            return cached[1]
        report = None
        try:
            rows = self._fetch_json(
                COT_URL,
                {"cftc_contract_market_code": code, "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 2},
            )
            report = parse_cot(rows, name)
        except Exception as exc:  # a public site being slow or down must never hold up an answer
            log.info("CFTC report not read: %s", type(exc).__name__)
        with self._lock:
            self._cot[code] = (now + (COT_TTL_SECONDS if report else COT_RETRY_SECONDS), report)
        return report

    # -------------------------------------------------------------------- text

    def lines(self, inst_id: str) -> list[str]:
        """Everything there is for one instrument, as lines of a list. Empty when there is nothing."""
        lines: list[str] = []
        top = self.top_traders(inst_id)
        if top is not None:
            when = datetime.fromtimestamp(top.ts / 1000, tz=self._tz).strftime("%m-%d %H:%M")
            lines.append(f"OKX 大户多空比（{inst_id}，按小时，数据时间 {when}）")
            if top.account_ratio is not None:
                lines.append(_ratio_line("按账户数：多空比", top.account_ratio, top.account_ratio_before))
            if top.position_ratio is not None:
                lines.append(_ratio_line("按持仓量：多空比", top.position_ratio, top.position_ratio_before))
        report = self.cot(inst_id)
        if report is not None:
            lines.append(f"CFTC 持仓周报（{report.market}，统计到 {report.report_date}，每周五公布）")
            text = (
                f"- 基金经理 (managed money)：多头 {report.mm_long:,.0f} 张，空头 {report.mm_short:,.0f} 张，"
                f"净多 {report.net:,.0f} 张"
            )
            if report.net_change is not None:
                text += f"，比前一周 {_signed(report.net_change)} 张"
            lines.append(text)
        return lines

    def text(self, inst_id: str) -> str:
        """For the AI: the lines, and the reminder of what they are for. "" when there is nothing."""
        lines = self.lines(inst_id)
        return "\n".join(lines + [CROWD_NOTE]) if lines else ""
