from __future__ import annotations

from datetime import date, datetime, time, timezone, tzinfo
from zoneinfo import ZoneInfo

import pandas as pd

def utc_datetime(day: date, hour: int = 0, at: time | None = None) -> datetime:
    selected_time = at if at is not None else time(hour=hour)
    return datetime.combine(day, selected_time, tzinfo=timezone.utc)


def timeline_datetime(day: date, selected_timezone: tzinfo, hour: int = 0, at: time | None = None) -> datetime:
    """Interpret a timeline input in the displayed timezone and normalize it to UTC."""
    selected_time = at if at is not None else time(hour=hour)
    return datetime.combine(day, selected_time, tzinfo=selected_timezone).astimezone(timezone.utc)


def chart_timezone(selection: str) -> tuple[tzinfo, str]:
    if selection == "Eastern Time (New York)":
        return ZoneInfo("America/New_York"), "Eastern Time (New York)"
    if selection == "Local time":
        local_now = datetime.now().astimezone()
        local_tz = local_now.tzinfo or timezone.utc
        local_name = local_now.tzname() or str(local_tz) or "UTC"
        return local_tz, f"Local time ({local_name})"
    return timezone.utc, "UTC"


def chart_times(values: pd.Series, selected_timezone: tzinfo) -> pd.Series:
    return pd.to_datetime(values, utc=True).dt.tz_convert(selected_timezone).dt.tz_localize(None)


def chart_timestamp(value: pd.Timestamp, selected_timezone: tzinfo) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert(selected_timezone).tz_localize(None)

