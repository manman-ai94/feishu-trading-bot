"""US rates and the dollar, the macro drivers of gold, from FRED (Federal Reserve Bank of St. Louis).

Public daily series, read as CSV without a key:

  DFII10    10-year TIPS yield: the real interest rate. Gold pays nothing, so a higher real rate
            makes holding it dearer; the two have long moved against each other.
  DGS10     10-year Treasury yield (nominal).
  T10YIE    10-year breakeven inflation: the nominal yield minus the real one.
  DTWEXBGS  Broad trade-weighted US dollar index.

FRED posts each value a day or two after the market day it belongs to, so every line carries its
date. This is background for an analysis, not a timing signal.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import requests

log = logging.getLogger(__name__)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
TIMEOUT = 10
TTL_SECONDS = 3600
RETRY_SECONDS = 30 * 60
LOOKBACK_DAYS = 70
# (FRED id, name, unit, decimals)
SERIES = (
    ("DFII10", "美国 10 年期实际利率 (TIPS real yield)", "%", 2),
    ("DGS10", "美国 10 年期国债收益率", "%", 2),
    ("T10YIE", "10 年期盈亏平衡通胀率 (breakeven inflation)", "%", 2),
    ("DTWEXBGS", "美元广义指数 (broad dollar index)", "", 2),
)
MACRO_NOTE = "以上来自 FRED（圣路易斯联储）的日度数据，通常晚一两个交易日。实际利率和美元走强一般压制金价，但近几年央行购金让这层关系时强时弱；这是背景，不是点位的依据。"


@dataclass(frozen=True)
class Series:
    series_id: str
    name: str
    unit: str
    decimals: int
    points: tuple[tuple[date, float], ...]  # oldest first

    def latest(self) -> tuple[date, float] | None:
        return self.points[-1] if self.points else None

    def back(self, days: int) -> tuple[date, float] | None:
        """The last value on or before `days` before the latest one."""
        latest = self.latest()
        if latest is None:
            return None
        cutoff = latest[0] - timedelta(days=days)
        earlier = [point for point in self.points if point[0] <= cutoff]
        return earlier[-1] if earlier else None

    def line(self) -> str:
        latest = self.latest()
        if latest is None:
            return f"{self.name}：没有取到"
        day, value = latest
        text = f"{self.name}：{value:.{self.decimals}f}{self.unit}（{day.isoformat()}）"
        changes = []
        for days, label in ((7, "一周前"), (30, "一个月前")):
            before = self.back(days)
            if before is not None:
                diff = value - before[1]
                if self.unit == "%":
                    changes.append(f"比{label}{'+' if diff >= 0 else ''}{diff * 100:.0f} 个基点")
                else:
                    changes.append(f"比{label}{'+' if diff >= 0 else ''}{diff / before[1] * 100:.1f}%")
        return text + ("，" + "，".join(changes) if changes else "")


def parse_csv(text: str) -> tuple[tuple[date, float], ...]:
    """FRED's CSV: a header, then date,value rows; a missing value is "." or empty."""
    points = []
    reader = csv.reader(io.StringIO(text))
    next(reader, None)
    for row in reader:
        if len(row) < 2:
            continue
        try:
            points.append((date.fromisoformat(row[0].strip()), float(row[1])))
        except ValueError:
            continue
    return tuple(sorted(points))


class Macro:
    """Reads the series when asked, keeps them for an hour, and never raises."""

    def __init__(self, get: Callable[..., requests.Response] | None = None, clock: Callable[[], float] = time.time):
        self._get = get or requests.get
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, str] | None = None
        self._failed_at = 0.0

    def text(self) -> str:
        """The lines for the AI; "" when FRED cannot be read."""
        now = self._clock()
        with self._lock:
            if self._cached and now - self._cached[0] < TTL_SECONDS:
                return self._cached[1]
            if now - self._failed_at < RETRY_SECONDS:
                return ""
        start = (date.fromtimestamp(now) - timedelta(days=LOOKBACK_DAYS)).isoformat()
        lines = []
        for series_id, name, unit, decimals in SERIES:
            try:
                response = self._get(FRED_CSV, params={"id": series_id, "cosd": start}, timeout=TIMEOUT)
                response.raise_for_status()
                points = parse_csv(response.text)
            except Exception as exc:
                log.info("macro: %s could not be read (%s)", series_id, type(exc).__name__)
                points = ()
            if points:
                lines.append(Series(series_id, name, unit, decimals, points).line())
        with self._lock:
            if not lines:
                self._failed_at = now
                return ""
            text = "\n".join(lines + [MACRO_NOTE])
            self._cached = (now, text)
            return text
