# Copyright 2026 anchored-vwam-backtest contributors
"""Provide time utils behavior."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo
from zoneinfo import ZoneInfo

import pandas as pd


def utc_datetime(day: date, hour: int = 0, at: time | None = None) -> datetime:
    """Create a UTC datetime from a date and optional clock time.

    Returns:
        The computed result.

    """
    selected_time = at if at is not None else time(hour=hour)
    return datetime.combine(day, selected_time, tzinfo=UTC)


def timeline_datetime(
    day: date,
    selected_timezone: tzinfo,
    *,
    hour: int = 0,
    at: time | None = None,
) -> datetime:
    """Interpret displayed time and normalize it to UTC.

    Returns:
        The corresponding UTC datetime.

    """
    selected_time = at if at is not None else time(hour=hour)
    return datetime.combine(
        day, selected_time, tzinfo=selected_timezone
    ).astimezone(UTC)


def chart_timezone(selection: str) -> tuple[tzinfo, str]:
    """Resolve a UI timezone selection to its timezone and display label.

    Returns:
        The computed result.

    """
    if selection == "Eastern Time (New York)":
        return ZoneInfo("America/New_York"), "Eastern Time (New York)"
    if selection == "Local time":
        local_now = datetime.now().astimezone()
        local_tz = local_now.tzinfo or UTC
        local_name = local_now.tzname() or str(local_tz) or "UTC"
        return local_tz, f"Local time ({local_name})"
    return UTC, "UTC"


def chart_times(
    values: pd.Series[pd.Timestamp], selected_timezone: tzinfo
) -> pd.Series[pd.Timestamp]:
    """Convert UTC timestamps to naive timestamps in the display timezone.

    Returns:
        The computed result.

    """
    return (
        pd.to_datetime(values, utc=True)
        .dt.tz_convert(selected_timezone)
        .dt.tz_localize(None)
    )


def chart_timestamp(
    value: pd.Timestamp, selected_timezone: tzinfo
) -> pd.Timestamp:
    """Convert one timestamp to a naive display-timezone timestamp.

    Returns:
        The computed result.

    """
    return pd.Timestamp(value).tz_convert(selected_timezone).tz_localize(None)
