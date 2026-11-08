# Copyright 2026 anchored-vwam-backtest contributors
"""Provide indicators behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

LAST_WEEKDAY_INDEX = 6
LAST_SUPPORTED_MONTH_DAY = 28

if TYPE_CHECKING:
    from datetime import time, tzinfo


@dataclass(frozen=True, kw_only=True)
class RecurringAnchor:
    """Configure the local schedule for a recurring VWAP anchor."""

    selected_timezone: tzinfo
    anchor_weekday: int = 0
    anchor_month_day: int = 1
    interval_count: int = 1


def add_anchored_vwap(
    df: pd.DataFrame, anchor_at: pd.Timestamp
) -> pd.DataFrame:
    """Handle add anchored vwap.

    Returns:
        The resulting value.

    """
    result = df.copy()
    result["typical_price"] = (
        result["high"] + result["low"] + result["close"]
    ) / 3
    result["anchored_vwap"] = np.nan
    result["anchored_std"] = np.nan

    anchored = result["time"] >= anchor_at
    typical_price = result.loc[anchored, "typical_price"]
    volume = result.loc[anchored, "volume"]
    volume_price = typical_price * volume
    volume_price_squared = (typical_price**2) * volume
    cumulative_volume_price = volume_price.cumsum()
    cumulative_volume_price_squared = volume_price_squared.cumsum()
    cumulative_volume = volume.cumsum()
    anchored_vwap = cumulative_volume_price / cumulative_volume
    anchored_variance = (
        cumulative_volume_price_squared / cumulative_volume - anchored_vwap**2
    )
    result.loc[anchored, "anchored_vwap"] = anchored_vwap
    result.loc[anchored, "anchored_std"] = np.sqrt(
        np.maximum(anchored_variance, 0)
    )
    return result


def add_launch_weekly_anchored_vwap(
    df: pd.DataFrame,
    launched_at: pd.Timestamp,
    reset_weeks: int,
) -> pd.DataFrame:
    """Anchor at bot launch and reset at exact N-week intervals from that.

    instant.

    Returns:
        The computed result.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    if reset_weeks < 1:
        msg = "VWAP anchor reset weeks must be at least 1."
        raise ValueError(msg)
    result = df.copy()
    result["typical_price"] = (
        result["high"] + result["low"] + result["close"]
    ) / 3
    result["anchored_vwap"] = np.nan
    result["anchored_std"] = np.nan
    elapsed = pd.to_datetime(result["time"], utc=True) - pd.Timestamp(
        launched_at
    )
    valid = elapsed >= pd.Timedelta(0)
    period = (
        elapsed.dt.total_seconds() // (reset_weeks * 7 * 24 * 3600)
    ).where(valid)
    working = result[valid].copy()
    if working.empty:
        return result
    working["period"] = period[valid].astype(int)
    working["volume_price"] = working["typical_price"] * working["volume"]
    working["volume_price_squared"] = (working["typical_price"] ** 2) * working[
        "volume"
    ]
    grouped = working.groupby("period", sort=False)
    cumulative_volume = grouped["volume"].cumsum()
    vwap = grouped["volume_price"].cumsum() / cumulative_volume
    variance = (
        grouped["volume_price_squared"].cumsum() / cumulative_volume - vwap**2
    )
    result.loc[working.index, "anchored_vwap"] = vwap
    result.loc[working.index, "anchored_std"] = np.sqrt(np.maximum(variance, 0))
    return result


def add_daily_anchored_vwap(
    df: pd.DataFrame,
    anchor_time: time,
    selected_timezone: tzinfo,
) -> pd.DataFrame:
    """Reset anchored VWAP at a local time and carry it across midnight.

    Returns:
        The computed result.

    """
    result = df.copy()
    result["typical_price"] = (
        result["high"] + result["low"] + result["close"]
    ) / 3
    result["anchored_vwap"] = np.nan
    result["anchored_std"] = np.nan

    local_times = pd.to_datetime(result["time"], utc=True).dt.tz_convert(
        selected_timezone
    )
    anchor_days = local_times.dt.normalize()
    before_anchor = local_times.dt.time < anchor_time
    anchor_days -= pd.to_timedelta(before_anchor.astype(int), unit="D")
    daily = result.copy()
    daily["anchor_day"] = anchor_days

    # A partial history beginning before its first anchor must not invent an
    # anchor from the first available candle.
    has_anchor = (~before_anchor).groupby(anchor_days).transform("any")
    daily = daily[has_anchor].copy()
    if daily.empty:
        return result

    daily["volume_price"] = daily["typical_price"] * daily["volume"]
    daily["volume_price_squared"] = (daily["typical_price"] ** 2) * daily[
        "volume"
    ]
    grouped = daily.groupby("anchor_day", sort=False)
    cumulative_volume = grouped["volume"].cumsum()
    anchored_vwap = grouped["volume_price"].cumsum() / cumulative_volume
    anchored_variance = (
        grouped["volume_price_squared"].cumsum() / cumulative_volume
        - anchored_vwap**2
    )
    result.loc[daily.index, "anchored_vwap"] = anchored_vwap
    result.loc[daily.index, "anchored_std"] = np.sqrt(
        np.maximum(anchored_variance, 0)
    )
    return result


def _daily_anchor_dates(
    local_times: pd.Series[pd.Timestamp], anchor_time: time, interval: int
) -> pd.Series[pd.Timestamp]:
    anchor_dates = local_times.dt.normalize()
    before_anchor = local_times.dt.time < anchor_time
    anchor_dates -= pd.to_timedelta(before_anchor.astype(int), unit="D")
    reference = pd.Timestamp("1970-01-01")
    period_number = (anchor_dates.dt.tz_localize(None) - reference).dt.days
    return (
        reference
        + pd.to_timedelta(
            (period_number // interval) * interval,
            unit="D",
        )
    ).dt.tz_localize(local_times.dt.tz)


def _weekly_anchor_dates(
    local_times: pd.Series[pd.Timestamp],
    anchor_time: time,
    options: RecurringAnchor,
) -> pd.Series[pd.Timestamp]:
    days_since_anchor = (local_times.dt.weekday - options.anchor_weekday) % 7
    anchor_dates = local_times.dt.normalize() - pd.to_timedelta(
        days_since_anchor, unit="D"
    )
    before_anchor = (days_since_anchor == 0) & (
        local_times.dt.time < anchor_time
    )
    anchor_dates -= pd.to_timedelta(before_anchor.astype(int) * 7, unit="D")
    reference = pd.Timestamp("1970-01-01")
    reference += pd.Timedelta(
        days=(options.anchor_weekday - reference.weekday()) % 7
    )
    period_number = (anchor_dates.dt.tz_localize(None) - reference).dt.days // 7
    return (
        reference
        + pd.to_timedelta(
            (period_number // options.interval_count)
            * options.interval_count
            * 7,
            unit="D",
        )
    ).dt.tz_localize(options.selected_timezone)


def _monthly_anchor_dates(
    local_times: pd.Series[pd.Timestamp],
    anchor_time: time,
    options: RecurringAnchor,
) -> pd.Series[pd.Timestamp]:
    naive = local_times.dt.tz_localize(None)
    month_starts = naive.dt.to_period("M").dt.start_time.dt.tz_localize(
        options.selected_timezone
    )
    this_anchor = month_starts + pd.to_timedelta(
        options.anchor_month_day - 1, unit="D"
    )
    before_anchor = (local_times.dt.day < options.anchor_month_day) | (
        (local_times.dt.day == options.anchor_month_day)
        & (local_times.dt.time < anchor_time)
    )
    previous_starts = (
        (naive - pd.DateOffset(months=1))
        .dt.to_period("M")
        .dt.start_time.dt.tz_localize(options.selected_timezone)
    )
    previous_anchor = previous_starts + pd.to_timedelta(
        options.anchor_month_day - 1, unit="D"
    )
    anchor_dates = this_anchor.where(~before_anchor, previous_anchor)
    month_number = anchor_dates.dt.year * 12 + anchor_dates.dt.month - 1
    anchor_number = (
        month_number // options.interval_count
    ) * options.interval_count
    return pd.to_datetime({
        "year": anchor_number // 12,
        "month": anchor_number % 12 + 1,
        "day": options.anchor_month_day,
    }).dt.tz_localize(options.selected_timezone)


def _recurring_anchor_dates(
    local_times: pd.Series[pd.Timestamp],
    frequency: str,
    anchor_time: time,
    *,
    options: RecurringAnchor,
) -> pd.Series[pd.Timestamp]:
    if frequency == "Daily":
        return _daily_anchor_dates(
            local_times, anchor_time, options.interval_count
        )
    if frequency == "Weekly":
        return _weekly_anchor_dates(local_times, anchor_time, options)
    return _monthly_anchor_dates(local_times, anchor_time, options)


def add_recurring_anchored_vwap(
    df: pd.DataFrame,
    frequency: str,
    anchor_time: time,
    *,
    options: RecurringAnchor,
) -> pd.DataFrame:
    """Reset VWAP on a daily, weekly, or monthly schedule in local time.

    Returns:
        The computed result.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    if options.interval_count < 1:
        msg = "VWAP anchor interval must be at least 1."
        raise ValueError(msg)
    if frequency == "Daily" and options.interval_count == 1:
        return add_daily_anchored_vwap(
            df, anchor_time, options.selected_timezone
        )
    if frequency not in {"Weekly", "Monthly"}:
        msg = "VWAP anchor frequency must be Daily, Weekly, or Monthly."
        raise ValueError(msg)
    if not 0 <= options.anchor_weekday <= LAST_WEEKDAY_INDEX:
        msg = "Weekly VWAP anchor weekday must be between Monday and Sunday."
        raise ValueError(msg)
    if not 1 <= options.anchor_month_day <= LAST_SUPPORTED_MONTH_DAY:
        msg = "Monthly VWAP anchor day must be between 1 and 28."
        raise ValueError(msg)

    result = df.copy()
    result["typical_price"] = (
        result["high"] + result["low"] + result["close"]
    ) / 3
    result["anchored_vwap"] = np.nan
    result["anchored_std"] = np.nan
    local_times = pd.to_datetime(result["time"], utc=True).dt.tz_convert(
        options.selected_timezone
    )
    anchor_dates = _recurring_anchor_dates(
        local_times, frequency, anchor_time, options=options
    )

    anchor_timestamps = anchor_dates + pd.to_timedelta(
        anchor_time.hour * 3600 + anchor_time.minute * 60 + anchor_time.second,
        unit="s",
    )
    recurring = result.copy()
    recurring["anchor_at"] = anchor_timestamps

    # Do not calculate a partial period when its actual anchor candle predates
    # the loaded history.
    first_local_time = local_times.iloc[0]
    recurring = recurring[anchor_timestamps >= first_local_time].copy()
    if recurring.empty:
        return result

    recurring["volume_price"] = recurring["typical_price"] * recurring["volume"]
    recurring["volume_price_squared"] = (
        recurring["typical_price"] ** 2
    ) * recurring["volume"]
    grouped = recurring.groupby("anchor_at", sort=False)
    cumulative_volume = grouped["volume"].cumsum()
    anchored_vwap = grouped["volume_price"].cumsum() / cumulative_volume
    anchored_variance = (
        grouped["volume_price_squared"].cumsum() / cumulative_volume
        - anchored_vwap**2
    )
    result.loc[recurring.index, "anchored_vwap"] = anchored_vwap
    result.loc[recurring.index, "anchored_std"] = np.sqrt(
        np.maximum(anchored_variance, 0)
    )
    return result
