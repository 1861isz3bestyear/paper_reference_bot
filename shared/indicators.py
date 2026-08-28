from __future__ import annotations

from datetime import time, tzinfo

import numpy as np
import pandas as pd

def add_anchored_vwap(df: pd.DataFrame, anchor_at: pd.Timestamp) -> pd.DataFrame:
    result = df.copy()
    result["typical_price"] = (result["high"] + result["low"] + result["close"]) / 3
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
    anchored_variance = cumulative_volume_price_squared / cumulative_volume - anchored_vwap**2
    result.loc[anchored, "anchored_vwap"] = anchored_vwap
    result.loc[anchored, "anchored_std"] = np.sqrt(np.maximum(anchored_variance, 0))
    return result


def add_launch_weekly_anchored_vwap(
    df: pd.DataFrame,
    launched_at: pd.Timestamp,
    reset_weeks: int,
) -> pd.DataFrame:
    """Anchor at bot launch and reset at exact N-week intervals from that instant."""
    if reset_weeks < 1:
        raise ValueError("VWAP anchor reset weeks must be at least 1.")
    result = df.copy()
    result["typical_price"] = (result["high"] + result["low"] + result["close"]) / 3
    result["anchored_vwap"] = np.nan
    result["anchored_std"] = np.nan
    elapsed = pd.to_datetime(result["time"], utc=True) - pd.Timestamp(launched_at)
    valid = elapsed >= pd.Timedelta(0)
    period = (elapsed.dt.total_seconds() // (reset_weeks * 7 * 24 * 3600)).where(valid)
    working = result[valid].copy()
    if working.empty:
        return result
    working["period"] = period[valid].astype(int)
    working["volume_price"] = working["typical_price"] * working["volume"]
    working["volume_price_squared"] = (working["typical_price"] ** 2) * working["volume"]
    grouped = working.groupby("period", sort=False)
    cumulative_volume = grouped["volume"].cumsum()
    vwap = grouped["volume_price"].cumsum() / cumulative_volume
    variance = grouped["volume_price_squared"].cumsum() / cumulative_volume - vwap**2
    result.loc[working.index, "anchored_vwap"] = vwap
    result.loc[working.index, "anchored_std"] = np.sqrt(np.maximum(variance, 0))
    return result


def add_daily_anchored_vwap(
    df: pd.DataFrame,
    anchor_time: time,
    selected_timezone: tzinfo,
) -> pd.DataFrame:
    """Reset anchored VWAP at a local time and carry it across midnight."""
    result = df.copy()
    result["typical_price"] = (result["high"] + result["low"] + result["close"]) / 3
    result["anchored_vwap"] = np.nan
    result["anchored_std"] = np.nan

    local_times = pd.to_datetime(result["time"], utc=True).dt.tz_convert(selected_timezone)
    anchor_days = local_times.dt.normalize()
    before_anchor = local_times.dt.time < anchor_time
    anchor_days = anchor_days - pd.to_timedelta(before_anchor.astype(int), unit="D")
    daily = result.copy()
    daily["anchor_day"] = anchor_days

    # A partial history beginning before its first anchor must not invent an
    # anchor from the first available candle.
    has_anchor = (~before_anchor).groupby(anchor_days).transform("any")
    daily = daily[has_anchor].copy()
    if daily.empty:
        return result

    daily["volume_price"] = daily["typical_price"] * daily["volume"]
    daily["volume_price_squared"] = (daily["typical_price"] ** 2) * daily["volume"]
    grouped = daily.groupby("anchor_day", sort=False)
    cumulative_volume = grouped["volume"].cumsum()
    anchored_vwap = grouped["volume_price"].cumsum() / cumulative_volume
    anchored_variance = grouped["volume_price_squared"].cumsum() / cumulative_volume - anchored_vwap**2
    result.loc[daily.index, "anchored_vwap"] = anchored_vwap
    result.loc[daily.index, "anchored_std"] = np.sqrt(np.maximum(anchored_variance, 0))
    return result


def add_recurring_anchored_vwap(
    df: pd.DataFrame,
    frequency: str,
    anchor_time: time,
    selected_timezone: tzinfo,
    anchor_weekday: int = 0,
    anchor_month_day: int = 1,
    interval_count: int = 1,
) -> pd.DataFrame:
    """Reset VWAP on a daily, weekly, or monthly schedule in local time."""
    if interval_count < 1:
        raise ValueError("VWAP anchor interval must be at least 1.")
    if frequency == "Daily" and interval_count == 1:
        return add_daily_anchored_vwap(df, anchor_time, selected_timezone)
    if frequency not in {"Weekly", "Monthly"}:
        raise ValueError("VWAP anchor frequency must be Daily, Weekly, or Monthly.")
    if not 0 <= anchor_weekday <= 6:
        raise ValueError("Weekly VWAP anchor weekday must be between Monday and Sunday.")
    if not 1 <= anchor_month_day <= 28:
        raise ValueError("Monthly VWAP anchor day must be between 1 and 28.")

    result = df.copy()
    result["typical_price"] = (result["high"] + result["low"] + result["close"]) / 3
    result["anchored_vwap"] = np.nan
    result["anchored_std"] = np.nan
    local_times = pd.to_datetime(result["time"], utc=True).dt.tz_convert(selected_timezone)

    if frequency == "Daily":
        anchor_dates = local_times.dt.normalize()
        before_anchor = local_times.dt.time < anchor_time
        anchor_dates = anchor_dates - pd.to_timedelta(before_anchor.astype(int), unit="D")
        reference = pd.Timestamp("1970-01-01")
        period_number = (anchor_dates.dt.tz_localize(None) - reference).dt.days
        anchor_dates = (reference + pd.to_timedelta(
            (period_number // interval_count) * interval_count,
            unit="D",
        )).dt.tz_localize(selected_timezone)
    elif frequency == "Weekly":
        days_since_anchor = (local_times.dt.weekday - anchor_weekday) % 7
        anchor_dates = local_times.dt.normalize() - pd.to_timedelta(days_since_anchor, unit="D")
        before_anchor = (days_since_anchor == 0) & (local_times.dt.time < anchor_time)
        anchor_dates = anchor_dates - pd.to_timedelta(before_anchor.astype(int) * 7, unit="D")
        reference = pd.Timestamp("1970-01-01")
        reference += pd.Timedelta(days=(anchor_weekday - reference.weekday()) % 7)
        period_number = (
            (anchor_dates.dt.tz_localize(None) - reference).dt.days // 7
        )
        anchor_dates = (reference + pd.to_timedelta(
            (period_number // interval_count) * interval_count * 7,
            unit="D",
        )).dt.tz_localize(selected_timezone)
    else:
        naive_local_times = local_times.dt.tz_localize(None)
        month_starts = naive_local_times.dt.to_period("M").dt.start_time.dt.tz_localize(selected_timezone)
        this_month_anchor = month_starts + pd.to_timedelta(anchor_month_day - 1, unit="D")
        before_anchor = (local_times.dt.day < anchor_month_day) | (
            (local_times.dt.day == anchor_month_day) & (local_times.dt.time < anchor_time)
        )
        previous_month_starts = (
            (naive_local_times - pd.DateOffset(months=1))
            .dt.to_period("M")
            .dt.start_time
            .dt.tz_localize(selected_timezone)
        )
        previous_month_anchor = previous_month_starts + pd.to_timedelta(anchor_month_day - 1, unit="D")
        anchor_dates = this_month_anchor.where(~before_anchor, previous_month_anchor)
        month_number = anchor_dates.dt.year * 12 + anchor_dates.dt.month - 1
        anchor_month_number = (month_number // interval_count) * interval_count
        anchor_year = anchor_month_number // 12
        anchor_month = anchor_month_number % 12 + 1
        anchor_dates = pd.to_datetime(
            {"year": anchor_year, "month": anchor_month, "day": anchor_month_day}
        ).dt.tz_localize(selected_timezone)

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
    recurring["volume_price_squared"] = (recurring["typical_price"] ** 2) * recurring["volume"]
    grouped = recurring.groupby("anchor_at", sort=False)
    cumulative_volume = grouped["volume"].cumsum()
    anchored_vwap = grouped["volume_price"].cumsum() / cumulative_volume
    anchored_variance = grouped["volume_price_squared"].cumsum() / cumulative_volume - anchored_vwap**2
    result.loc[recurring.index, "anchored_vwap"] = anchored_vwap
    result.loc[recurring.index, "anchored_std"] = np.sqrt(np.maximum(anchored_variance, 0))
    return result
