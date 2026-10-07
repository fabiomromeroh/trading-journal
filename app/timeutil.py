from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def local_to_utc_naive(d: date, t: time, tz: ZoneInfo | str = ET) -> datetime:
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    return to_utc_naive(datetime.combine(d, t, tzinfo=tz))


def utc_naive_to_tz(dt: datetime | None, tz: str | ZoneInfo) -> datetime | None:
    if dt is None:
        return None
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


def et_date(dt_utc_naive: datetime) -> date:
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(ET).date()
