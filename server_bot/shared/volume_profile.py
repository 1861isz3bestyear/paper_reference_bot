# Copyright 2026 anchored-vwam-backtest contributors
"""Causal executed-volume profiles.

Input rows become available at minute close.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict

import numpy as np
import pandas as pd

from shared.gex import aggregate_gex

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

MILLISECOND_TIMESTAMP_THRESHOLD = 10_000_000_000
PERCENT_SCALE = 100


class ProfileOptions(TypedDict, total=False):
    """Optional settings for an executed-volume profile."""

    bucket: float
    value_area: float
    period: str
    rolling_hours: int
    anchor: pd.Timestamp | None
    tick_size: float
    bar_interval: str


@dataclass(frozen=True, kw_only=True)
class _ProfileConfig:
    bucket: float = 10.0
    value_area: float = 70.0
    period: str = "Daily UTC"
    rolling_hours: int = 24
    anchor: pd.Timestamp | None = None
    tick_size: float = 0.1
    bar_interval: str = "1min"


type _Event = tuple[pd.Timestamp, int, float, float]


@dataclass
class _ProfileState:
    events: Iterator[_Event]
    event: _Event | None
    volumes: dict[int, float] = field(default_factory=dict)
    daily: dict[int, float] = field(default_factory=dict)
    queue: deque[tuple[pd.Timestamp, int, float]] = field(default_factory=deque)
    current_period: pd.Timestamp | None = None
    previous: tuple[float, float, float] = (np.nan, np.nan, np.nan)
    last_close: pd.Timestamp | None = None


def price_minutes(trades: pd.DataFrame) -> pd.DataFrame:
    """Build reusable exact-price volume totals from validated Bybit trades.

    Returns:
        The computed result.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    _ = aggregate_gex(trades, "1min")
    if "price" not in trades:
        msg = "Volume profiles require trade price in the CSV."
        raise ValueError(msg)
    frame = trades.copy()
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame["size"] = pd.to_numeric(frame["size"])
    if (
        not frame["price"].astype(float).map(math.isfinite).all()
        or (frame["price"].astype(float) <= 0).any()
    ):
        msg = "Trade prices must be finite and positive."
        raise ValueError(msg)
    if "trdMatchID" in frame:
        prices = frame.groupby("trdMatchID")["price"]
        if prices.min().ne(prices.max()).any():
            msg = "Conflicting prices for duplicate trade IDs."
            raise ValueError(msg)
        frame = frame.drop_duplicates("trdMatchID")
    frame = frame.copy()
    timestamps = frame["timestamp"]
    if pd.api.types.is_numeric_dtype(timestamps):
        timestamps = pd.to_datetime(
            timestamps,
            unit=(
                "ms"
                if pd.to_numeric(timestamps).astype(float).median()
                > MILLISECOND_TIMESTAMP_THRESHOLD
                else "s"
            ),
            utc=True,
        )
    else:
        timestamps = pd.to_datetime(timestamps, utc=True, format="mixed")
    frame["time"] = timestamps.dt.floor("min") + pd.Timedelta(minutes=1)
    return frame.groupby(["time", "price"])[["size"]].sum().reset_index()


def levels(
    volumes: Mapping[int, float], bucket: float, fraction: float
) -> tuple[float, float, float]:
    """Return levels.

    Returns:
        The computed result.

    """
    populated = sorted(key for key, value in volumes.items() if value > 0)
    if not populated:
        return (np.nan, np.nan, np.nan)
    amounts = [volumes[key] for key in populated]
    poc = max(
        range(len(amounts)), key=amounts.__getitem__
    )  # Equal POCs resolve to the lower price.
    low = high = poc
    included = amounts[poc]
    target = sum(amounts) * fraction
    while included < target and (low > 0 or high < len(amounts) - 1):
        below = amounts[low - 1] if low > 0 else -1
        above = amounts[high + 1] if high < len(amounts) - 1 else -1
        if below >= above:
            low -= 1
            included += amounts[low]
        else:
            high += 1
            included += amounts[high]
    return (
        (populated[high] + 1) * bucket,
        (populated[poc] + 0.5) * bucket,
        populated[low] * bucket,
    )


def _config(options: ProfileOptions | None) -> _ProfileConfig:
    values = options or {}
    return _ProfileConfig(
        bucket=values.get("bucket", 10.0),
        value_area=values.get("value_area", 70.0),
        period=values.get("period", "Daily UTC"),
        rolling_hours=values.get("rolling_hours", 24),
        anchor=values.get("anchor"),
        tick_size=values.get("tick_size", 0.1),
        bar_interval=values.get("bar_interval", "1min"),
    )


def _validate_config(config: _ProfileConfig) -> None:
    if (
        not np.isfinite(config.bucket)
        or config.bucket <= 0
        or Decimal(str(config.bucket)) % Decimal(str(config.tick_size)) != 0
    ):
        msg = (
            "Price bucket must be a positive multiple of the BTCUSDT tick size"
            " (0.1 USDT)."
        )
        raise ValueError(msg)
    if not np.isfinite(config.value_area) or not (
        0 < config.value_area <= PERCENT_SCALE
    ):
        msg = "Value area must be greater than 0 and at most 100 percent."
        raise ValueError(msg)
    if config.period not in {
        "Daily UTC",
        "Weekly UTC",
        "Monthly UTC",
        "Rolling",
        "Anchored",
    }:
        msg = "Unknown volume profile period."
        raise ValueError(msg)
    if config.period == "Rolling" and (
        not np.isfinite(config.rolling_hours) or config.rolling_hours <= 0
    ):
        msg = "Rolling hours must be positive."
        raise ValueError(msg)
    if config.period == "Anchored" and config.anchor is None:
        msg = "An anchored profile requires an anchor."
        raise ValueError(msg)


def _period_start(timestamp: pd.Timestamp, period: str) -> pd.Timestamp:
    """Return the UTC start of the configured calendar period.

    Returns:
        The period start timestamp.

    """
    day = timestamp.normalize()
    if period == "Weekly UTC":
        return day - pd.Timedelta(days=day.weekday())
    if period == "Monthly UTC":
        return pd.Timestamp(day.year, day.month, 1, tz="UTC")
    return day


def _previous_period_start(
    period_start: pd.Timestamp, period: str
) -> pd.Timestamp:
    """Return the start of the calendar period immediately before a period.

    Returns:
        The previous period start timestamp.

    """
    return _period_start(period_start - pd.Timedelta(1, unit="ns"), period)


def _start_period(
    state: _ProfileState, period_start: pd.Timestamp, config: _ProfileConfig
) -> None:
    if state.current_period == _previous_period_start(
        period_start, config.period
    ):
        state.previous = levels(
            state.daily, config.bucket, config.value_area / PERCENT_SCALE
        )
    else:
        state.previous = (np.nan, np.nan, np.nan)
    state.daily = {}
    state.current_period = period_start
    if config.period in {"Daily UTC", "Weekly UTC", "Monthly UTC"}:
        state.volumes = {}


def _consume_events(
    state: _ProfileState, close: pd.Timestamp, config: _ProfileConfig
) -> tuple[float, float]:
    traded_value = traded_size = 0.0
    while state.event is not None and state.event[0] <= close:
        timestamp, key, size, price = state.event
        event_period = _period_start(
            timestamp - pd.Timedelta(1, unit="ns"), config.period
        )
        if event_period != state.current_period:
            _start_period(state, event_period, config)
        state.daily[key] = state.daily.get(key, 0.0) + size
        is_active = config.period != "Anchored" or (
            config.anchor is not None
            and timestamp - pd.Timedelta(minutes=1) >= config.anchor
        )
        if is_active:
            state.volumes[key] = state.volumes.get(key, 0.0) + size
            state.queue.append((timestamp, key, size))
        if state.last_close is not None and timestamp > state.last_close:
            traded_value += price * size
            traded_size += size
        state.event = next(state.events, None)
    return traded_value, traded_size


def _expire_rolling(
    state: _ProfileState, close: pd.Timestamp, config: _ProfileConfig
) -> None:
    if config.period != "Rolling":
        state.queue.clear()
        return
    cutoff = close - pd.Timedelta(hours=config.rolling_hours)
    while state.queue and state.queue[0][0] <= cutoff:
        _, key, size = state.queue.popleft()
        state.volumes[key] = state.volumes.get(key, 0.0) - size
        if state.volumes[key] <= 0:
            del state.volumes[key]


def _bar_levels(
    state: _ProfileState, close: pd.Timestamp, config: _ProfileConfig
) -> tuple[float, float, float, float, float, float, float]:
    period = _period_start(close - pd.Timedelta(1, unit="ns"), config.period)
    traded_value, traded_size = _consume_events(state, close, config)
    _expire_rolling(state, close, config)
    active = (
        state.volumes
        if config.period not in {"Daily UTC", "Weekly UTC", "Monthly UTC"}
        or state.current_period == period
        else {}
    )
    prior = state.previous
    if state.current_period != period:
        prior = (
            levels(
                state.daily, config.bucket, config.value_area / PERCENT_SCALE
            )
            if state.current_period
            == _previous_period_start(period, config.period)
            else (np.nan, np.nan, np.nan)
        )
    state.last_close = close
    return (
        *levels(active, config.bucket, config.value_area / PERCENT_SCALE),
        *prior,
        traded_value / traded_size if traded_size else np.nan,
    )


def add_volume_profile(
    bars: pd.DataFrame,
    prices: pd.DataFrame,
    options: ProfileOptions | None = None,
) -> pd.DataFrame:
    """Return add volume profile.

    Returns:
        The computed result.

    """
    config = _config(options)
    _validate_config(config)
    frame = prices.sort_values("time").copy()
    # Decimal avoids binary-float errors at bucket boundaries.
    frame["bucket"] = (
        frame["price"]
        .astype(float)
        .map(lambda p: int(Decimal(str(p)) // Decimal(str(config.bucket))))
    )
    events = iter(
        zip(
            pd.to_datetime(frame["time"], utc=True),
            frame["bucket"].astype(int),
            frame["size"].astype(float),
            frame["price"].astype(float),
            strict=False,
        )
    )
    times = pd.to_datetime(bars["time"], utc=True)
    state = _ProfileState(
        events=events,
        event=next(events, None),
        last_close=(
            times.iloc[0] - pd.Timedelta(config.bar_interval)
            if not bars.empty
            else None
        ),
    )
    output = [_bar_levels(state, close, config) for close in times]
    result = bars.copy()
    columns = [
        "vah",
        "poc",
        "val",
        "previous_vah",
        "previous_poc",
        "previous_val",
        "bar_trade_vwap",
    ]
    result[columns] = pd.DataFrame(output, index=result.index, columns=columns)
    return result
