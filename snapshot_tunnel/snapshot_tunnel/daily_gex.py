# Copyright 2026 anchored-vwam-backtest contributors
"""Daily options plans and independently testable entry confirmations."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import time, timedelta
from enum import StrEnum
from statistics import fmean

MINIMUM_REGRESSION_SAMPLES = 3


class PlanStatus(StrEnum):
    """Describe the daily setup's current trading recommendation."""

    UNAVAILABLE = "No valid plan"
    NEUTRAL = "Neutral: do not trade"
    CANCELLED = "Cancelled until next daily plan"
    REVERSION = "Reversion"
    MOMENTUM = "Momentum"


class FlowMode(StrEnum):
    """Select independent directional evidence."""

    TRADES = "Executed trades"
    BOOK = "Order book"
    BOTH = "Trades and book"


@dataclass(frozen=True, kw_only=True)
class FlowSettings:
    """Configure causal OI regression and disjoint volume windows."""

    mode: FlowMode = FlowMode.TRADES
    threshold: float = 0.2
    oi_window: timedelta = timedelta(minutes=30)
    oi_age: timedelta = timedelta(minutes=10)
    oi_delay: timedelta = timedelta(minutes=1)
    oi_tolerance: float = 0.0001
    volume_recent: timedelta = timedelta(minutes=5)
    volume_baseline: timedelta = timedelta(minutes=30)
    volume_tolerance: float = 0.05
    book_age: timedelta = timedelta(seconds=10)
    use_oi: bool = True
    use_volume: bool = True

    def __post_init__(self) -> None:
        """Reject invalid windows and thresholds.

        Raises:
            ValueError: If a setting cannot describe a causal filter.

        """
        windows = (
            self.oi_window,
            self.oi_age,
            self.volume_recent,
            self.volume_baseline,
            self.book_age,
        )
        numbers = (self.threshold, self.oi_tolerance, self.volume_tolerance)
        if (
            any(value <= timedelta(0) for value in windows)
            or self.oi_delay < timedelta(0)
            or not all(math.isfinite(value) and value >= 0 for value in numbers)
            or self.threshold > 1
        ):
            msg = (
                "Flow windows must be positive and thresholds"
                " finite/nonnegative."
            )
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class WallSettings:
    """Configure wall proximity, confirmation drift and stop placement."""

    proximity: float = 0.001
    momentum_offset: float = 0.001
    stop_offset: float | None = 0.002
    neutral_gex: float = 0.0
    neutral_fraction: float = 0.0

    def __post_init__(self) -> None:
        """Reject invalid relative price distances.

        Raises:
            ValueError: If distances or the neutral exposure are invalid.

        """
        distances = (self.proximity, self.neutral_fraction)
        if self.stop_offset is not None:
            distances += (self.stop_offset,)
        if (
            not all(
                math.isfinite(value) and 0 <= value < 1 for value in distances
            )
            or not math.isfinite(self.momentum_offset)
            or not -1 < self.momentum_offset < 1
            or not math.isfinite(self.neutral_gex)
            or self.neutral_gex < 0
        ):
            msg = "Invalid wall distances or neutral GEX threshold."
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class FreshConfirmation:
    """Configure fresh-chain validation separately from fixed plans."""

    maximum_age: timedelta = timedelta(minutes=10)
    wall_drift: float = 0.002

    def __post_init__(self) -> None:
        """Reject invalid fresh-chain limits.

        Raises:
            ValueError: If age or drift is outside the supported domain.

        """
        if (
            self.maximum_age <= timedelta(0)
            or not math.isfinite(self.wall_drift)
            or not 0 <= self.wall_drift < 1
        ):
            msg = (
                "Fresh confirmation requires positive age and drift in [0, 1)."
            )
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class DailySettings:
    """Specify a daily publication schedule and execution semantics."""

    refresh: time = time(8, 15)
    fresh_confirmation: FreshConfirmation | None = None
    intrabar: bool = False
    gamma_flip_direction: bool = False
    snapshot_age: timedelta = timedelta(hours=24)
    expiry_horizon: timedelta = timedelta(days=90)
    flow: FlowSettings = field(default_factory=FlowSettings)
    walls: WallSettings = field(default_factory=WallSettings)

    def __post_init__(self) -> None:
        """Require positive history limits and a plain UTC wall clock.

        Raises:
            ValueError: If the schedule or expiry scope is invalid.

        """
        if (
            self.refresh.tzinfo is not None
            or self.snapshot_age <= timedelta(0)
            or self.expiry_horizon <= timedelta(0)
        ):
            msg = "Use a UTC clock time and positive snapshot/expiry limits."
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class FlowEvidence:
    """Keep independently observed confirmation values separate."""

    trades: float = math.nan
    book: float = math.nan
    oi_slope: float = math.nan
    volume_change: float = math.nan

    def confirms(
        self, direction: int, *, gex: float, settings: FlowSettings
    ) -> bool:
        """Evaluate directional pressure and regime-specific activity.

        Returns:
            Whether all selected filters have fresh confirming evidence.

        """
        oscillators = {
            FlowMode.TRADES: (self.trades,),
            FlowMode.BOOK: (self.book,),
            FlowMode.BOTH: (self.trades, self.book),
        }[settings.mode]
        if not all(
            math.isfinite(value) and direction * value >= settings.threshold
            for value in oscillators
        ):
            return False
        regime = 1 if gex > 0 else -1
        filters = (
            (settings.use_oi, self.oi_slope, settings.oi_tolerance),
            (
                settings.use_volume,
                self.volume_change,
                settings.volume_tolerance,
            ),
        )
        return all(
            not enabled
            or (math.isfinite(value) and regime * value <= tolerance)
            for enabled, value, tolerance in filters
        )


def normalized_slope(points: tuple[tuple[float, float], ...]) -> float:
    """Estimate relative OI change per minute using actual sample times.

    Returns:
        Regression slope divided by mean OI, or NaN without positive data.

    """
    if len(points) < MINIMUM_REGRESSION_SAMPLES:
        return math.nan
    x_mean = fmean(point[0] for point in points)
    y_mean = fmean(point[1] for point in points)
    denominator = math.fsum((point[0] - x_mean) ** 2 for point in points)
    if denominator <= 0 or y_mean <= 0:
        return math.nan
    return (
        math.fsum((x - x_mean) * (y - y_mean) for x, y in points)
        / denominator
        / y_mean
    )
