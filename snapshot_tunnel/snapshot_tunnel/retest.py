# Copyright 2026 anchored-vwam-backtest contributors
"""Evaluate spot opening prices against completed hourly extremes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from snapshot_tunnel.tunnel import RetestMode
from snapshot_tunnel.tunnel_flow import BOTH_DIRECTIONS, OscillatorObservation

if TYPE_CHECKING:
    from collections.abc import Iterator

    from snapshot_tunnel.tunnel import RetestSettings

HOUR = timedelta(hours=1)


@dataclass(frozen=True, kw_only=True)
class SpotBar:
    """A verified spot kline with its opening timestamp."""

    time: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, kw_only=True)
class HourlyLevels:
    """The most recent candles setting the active high and low."""

    peak: SpotBar
    trough: SpotBar


@dataclass(frozen=True, kw_only=True)
class RetestHistory:
    """Spot bars and opening samples available within one decision window."""

    hourly: tuple[SpotBar, ...] = ()
    samples: tuple[SpotBar, ...] = ()

    def assess(self, *, lookback: int) -> Iterator[RetestAssessment]:
        """Match each opening sample to its completed hourly window.

        Yields:
            Chronological assessments, including samples missing their warmup.

        """
        levels = dict(_hourly_windows(self.hourly, lookback=lookback))
        for sample in self.samples:
            hour = sample.time.replace(minute=0, second=0, microsecond=0)
            yield RetestAssessment(sample=sample, levels=levels.get(hour))


@dataclass(frozen=True, kw_only=True)
class RetestAssessment:
    """A sampled quote and the extremes available when it opened."""

    sample: SpotBar
    levels: HourlyLevels | None

    def touches(self) -> tuple[tuple[datetime, float], ...]:
        """Return the origin candles whose level the sample reaches.

        Returns:
            Origins in high then low order.

        """
        if self.levels is None:
            return ()
        return tuple(
            (origin.time + HOUR, price)
            for origin, price, touched in (
                (self.levels.peak, self.levels.peak.high, self.touches_high),
                (self.levels.trough, self.levels.trough.low, self.touches_low),
            )
            if touched
        )

    @property
    def touches_high(self) -> bool:
        """Whether the opening spot quote reaches the available high."""
        return (
            self.levels is not None
            and self.sample.open >= self.levels.peak.high
        )

    @property
    def touches_low(self) -> bool:
        """Whether the opening spot quote reaches the available low."""
        return (
            self.levels is not None
            and self.sample.open <= self.levels.trough.low
        )

    def direction(self, mode: RetestMode) -> int:
        """Map the level touch to a signed confirmation.

        Returns:
            A directional, bidirectional or neutral vote.

        """
        high, low = self.touches_high, self.touches_low
        if mode is RetestMode.EITHER and (high or low):
            return BOTH_DIRECTIONS
        reversal = int(low) - int(high)
        return reversal if mode is RetestMode.REVERSAL else -reversal


def _hourly_windows(
    bars: tuple[SpotBar, ...], *, lookback: int
) -> Iterator[tuple[datetime, HourlyLevels]]:
    window: deque[SpotBar] = deque(maxlen=lookback)
    for bar in bars:
        if window and bar.time - window[-1].time != HOUR:
            window.clear()
        window.append(bar)
        if len(window) == lookback:
            yield (
                bar.time + HOUR,
                HourlyLevels(
                    peak=max(reversed(window), key=lambda item: item.high),
                    trough=min(reversed(window), key=lambda item: item.low),
                ),
            )


def retest_votes(
    history: RetestHistory, *, settings: RetestSettings
) -> tuple[OscillatorObservation, ...]:
    """Evaluate chronological opening quotes without looking ahead.

    Returns:
        Votes expiring at the next expected sample, with gaps uncalculatable.

    """
    interval = timedelta(minutes=1 if settings.interval == "1m" else 5)
    return tuple(
        OscillatorObservation(
            available=assessment.sample.time,
            direction=assessment.direction(settings.mode),
            calculatable=assessment.levels is not None,
            valid_for=interval,
        )
        for assessment in history.assess(lookback=settings.lookback)
    )
