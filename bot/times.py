"""Times as the owners read them: the trader's own zone and Beijing time side by side.

The owner lives in Los Angeles; the other owner and the friends read Beijing time.
A time shown in only one of them gets asked about again, so the bot writes both.
Each person may also say which one he reads first ("我用北京时间").
"""
from __future__ import annotations

import json
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
ZONE_SETTING = "time_zones"  # open_id -> the zone he reads first, e.g. "Asia/Shanghai"
ZONE_NAMES = {"Asia/Shanghai": "北京", "America/Los_Angeles": "洛杉矶"}


def zone_name(tz: tzinfo | None) -> str:
    key = getattr(tz, "key", "") or ""
    return ZONE_NAMES.get(key, key or "本地")


def _same(one: tzinfo | None, other: tzinfo | None) -> bool:
    return getattr(one, "key", None) is not None and getattr(one, "key", None) == getattr(other, "key", None)


def both(ts: float, tz: tzinfo | None, fmt: str = "%m-%d %H:%M", first: tzinfo | None = None) -> str:
    """ "11-21 13:30（北京 11-22 05:30）": the trader's zone `tz` and Beijing. `first`: the zone the reader
    reads first, which then leads. One zone only when the two are the same."""
    lead = first or tz
    other = BEIJING if not _same(lead, BEIJING) else tz
    shown = datetime.fromtimestamp(ts, tz=lead).strftime(fmt)
    if _same(lead, other) or other is None:
        return shown
    return f"{shown}（{zone_name(other)} {datetime.fromtimestamp(ts, tz=other).strftime(fmt)}）"


def now_text(ts: float, tz: tzinfo | None, first: tzinfo | None = None) -> str:
    """ "洛杉矶 11-22 13:45 周日｜北京 11-23 05:45 周一": the answer to "现在几点"."""
    zones = [first or tz]
    for zone in (tz, BEIJING):
        if zone is not None and not any(_same(zone, seen) for seen in zones):
            zones.append(zone)
    parts = []
    for zone in zones:
        moment = datetime.fromtimestamp(ts, tz=zone)
        parts.append(f"{zone_name(zone)} {moment.strftime('%m-%d %H:%M')} {WEEKDAYS[moment.weekday()]}")
    return "｜".join(parts)


def zones_of(journal) -> dict[str, str]:
    if journal is None:
        return {}
    try:
        return {str(k): str(v) for k, v in dict(json.loads(journal.get_setting(ZONE_SETTING, "") or "{}")).items()}
    except (ValueError, TypeError, AttributeError):
        return {}


def zone_of(journal, open_id: str) -> ZoneInfo | None:
    """The zone this person said he reads first; None when he never said."""
    key = zones_of(journal).get(open_id or "", "")
    try:
        return ZoneInfo(key) if key else None
    except Exception:
        return None


def set_zone(journal, open_id: str, key: str) -> None:
    zones = zones_of(journal)
    zones[open_id] = key
    journal.set_setting(ZONE_SETTING, json.dumps(zones, ensure_ascii=False))


def when_text(ts: float, tz: tzinfo | None, first: tzinfo | None = None) -> str:
    """ "北京 11-21 周五 13:30（洛杉矶 11-20 周四 21:30）": a moment set for later, with the weekday."""
    zones = [first or tz]
    for zone in (tz, BEIJING):
        if zone is not None and not any(_same(zone, seen) for seen in zones):
            zones.append(zone)
    parts = []
    for zone in zones[:2]:
        moment = datetime.fromtimestamp(ts, tz=zone)
        parts.append(f"{zone_name(zone)} {moment.strftime('%m-%d')} {WEEKDAYS[moment.weekday()]} {moment.strftime('%H:%M')}")
    return parts[0] + (f"（{parts[1]}）" if len(parts) > 1 else "")
