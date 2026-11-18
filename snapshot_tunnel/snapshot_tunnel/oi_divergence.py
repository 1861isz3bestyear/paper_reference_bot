# Copyright 2026 anchored-vwam-backtest contributors
"""Causal OI momentum, price-swing signals and a held binary state."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

_HOUR = timedelta(hours=1)
_PIVOT_WINDOW = 4


@dataclass(frozen=True, kw_only=True)
class DivergenceSettings:
    """Minimum two-sided price excursion and oscillator change."""

    min_swing_percent: float = 0.1
    min_oi_change: float = 0.05
    interval: timedelta = _HOUR
    comparison: Literal["momentum", "open_interest"] = "momentum"

    def __post_init__(self) -> None:
        """Reject negative or nonfinite thresholds.

        Raises:
            ValueError: If either threshold is invalid.

        """
        if self.interval not in {_HOUR, timedelta(minutes=5)}:
            msg = "OI interval must be five minutes or one hour."
            raise ValueError(msg)
        if any(
            not math.isfinite(value) or value < 0
            for value in (self.min_swing_percent, self.min_oi_change)
        ):
            msg = "Divergence thresholds must be finite and nonnegative."
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class InterestSample:
    """One positive base-asset OI snapshot at its exchange timestamp."""

    timestamp: datetime
    value: float

    def __post_init__(self) -> None:
        """Validate timestamps and base-asset quantities.

        Raises:
            ValueError: If the timestamp or quantity is invalid.

        """
        if self.timestamp.utcoffset() is None or not (
            math.isfinite(self.value) and self.value > 0
        ):
            msg = "OI needs offset-aware timestamps and positive finite values."
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class MarketSample:
    """A source price paired with contemporaneous contract OI."""

    interest: InterestSample
    price: float
    received: datetime | None = None

    def __post_init__(self) -> None:
        """Validate the matched candle price.

        Raises:
            ValueError: If price is nonpositive or nonfinite.

        """
        if self.received is not None and (
            self.received.utcoffset() is None
            or self.received < self.interest.timestamp
        ):
            msg = "Market receipt must be causal and offset-aware."
            raise ValueError(msg)
        if not math.isfinite(self.price) or self.price <= 0:
            msg = "Matched price must be positive and finite."
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class MomentumPoint:
    """One smoothed signed rate with its first usable availability time."""

    sample: MarketSample
    rate: float | None

    @property
    def available(self) -> datetime:
        """Actual receipt time, or an assumed one-minute publication delay."""
        if self.sample.received is not None:
            return self.sample.received
        return self.sample.interest.timestamp + timedelta(minutes=1)


@dataclass(frozen=True, kw_only=True)
class SwingPivot:
    """A price swing with a nearby oscillator extreme."""

    index: int
    oi_index: int
    kind: Literal["high", "low"]
    point: MomentumPoint
    rate: float


@dataclass(frozen=True, kw_only=True)
class DivergenceSignal:
    """A confirmed comparison, timestamped when it first becomes knowable."""

    previous: SwingPivot
    current: SwingPivot
    detected: datetime
    state: Literal[0, 1]

    @property
    def label(self) -> str:
        """Describe participation agreement without a trading direction."""
        return "Divergence" if self.state else "No divergence (confirmation)"


@dataclass(frozen=True, kw_only=True)
class DivergenceSeries:
    """Momentum, held states and their originating comparisons."""

    points: tuple[MomentumPoint, ...]
    states: tuple[Literal[0, 1] | None, ...]
    signals: tuple[DivergenceSignal, ...]


def _momentum(
    samples: tuple[MarketSample, ...], settings: DivergenceSettings
) -> tuple[MomentumPoint, ...]:
    """Calculate 12-hour percentage change and its three-sample mean.

    Returns:
        One point per input, with unavailable rates during warmup.

    Raises:
        ValueError: If chronology is invalid or percentage change overflows.

    """
    lookback = int(timedelta(hours=12) / settings.interval)
    warmup = lookback + 2
    points: list[MomentumPoint] = []
    run_start = 0
    for index, sample in enumerate(samples):
        if index:
            delta = (
                sample.interest.timestamp
                - samples[index - 1].interest.timestamp
            )
            if delta <= timedelta(0):
                msg = "Market observations must be strictly chronological."
                raise ValueError(msg)
            if delta != settings.interval:
                run_start = index
        if index - run_start < warmup:
            points.append(MomentumPoint(sample=sample, rate=None))
            continue
        rate = math.fsum(
            100
            * (
                samples[item].interest.value
                / samples[item - lookback].interest.value
                - 1
            )
            / 3
            for item in range(index - 2, index + 1)
        )
        if not math.isfinite(rate):
            msg = "OI percentage change overflow."
            raise ValueError(msg)
        points.append(MomentumPoint(sample=sample, rate=rate))
    return tuple(points)


def _price_pivot(
    points: tuple[MomentumPoint, ...], index: int, settings: DivergenceSettings
) -> SwingPivot | None:
    """Locate strict, prominent price swings and OI extrema within one bar.

    Returns:
        A qualifying pivot, or no swing.

    """
    rates: dict[int, float] = {}
    for item in range(index - 2, index + 3):
        rate = points[item].rate
        if rate is None:
            return None
        rates[item] = (
            points[item].sample.interest.value
            if settings.comparison == "open_interest"
            else rate
        )
    point = points[index]
    left = [item.sample.price for item in points[index - 2 : index]]
    right = [item.sample.price for item in points[index + 1 : index + 3]]
    price = point.sample.price
    if all(price > value for value in (*left, *right)):
        kind = "high"
        excursion = min(price - min(left), price - min(right))
        oi_index = max((index, index - 1, index + 1), key=rates.__getitem__)
    elif all(price < value for value in (*left, *right)):
        kind = "low"
        excursion = min(max(left) - price, max(right) - price)
        oi_index = min((index, index - 1, index + 1), key=rates.__getitem__)
    else:
        return None
    if excursion / price * 100 < settings.min_swing_percent:
        return None
    if settings.comparison == "open_interest" and not _matched_oi_extreme(
        rates, index=oi_index, kind=kind
    ):
        return None
    return SwingPivot(
        index=index,
        oi_index=oi_index,
        kind=kind,
        point=point,
        rate=rates[oi_index],
    )


def _matched_oi_extreme(
    values: dict[int, float], *, index: int, kind: Literal["high", "low"]
) -> bool:
    """Require a strict raw OI turning point within one price-pivot sample.

    Returns:
        Whether nearby raw OI has formed the matching peak or trough.

    """
    value = values[index]
    neighbors = (values[index - 1], values[index + 1])
    if kind == "high":
        return all(value > neighbor for neighbor in neighbors)
    return all(value < neighbor for neighbor in neighbors)


def _compare_swings(
    previous: SwingPivot,
    current: SwingPivot,
    settings: DivergenceSettings,
    *,
    detected: datetime,
) -> DivergenceSignal | None:
    """Classify new price extremes using signed oscillator differences.

    Returns:
        Divergence or confirmation, excluding insignificant or equal changes.

    """
    price_change = (
        current.point.sample.price / previous.point.sample.price - 1
    ) * 100
    oi_change = (
        (current.rate / previous.rate - 1) * 100
        if settings.comparison == "open_interest"
        else current.rate - previous.rate
    )
    outward = price_change > 0 if current.kind == "high" else price_change < 0
    if not outward or abs(price_change) < settings.min_swing_percent:
        return None
    if oi_change == 0 or abs(oi_change) < settings.min_oi_change:
        return None
    agrees = oi_change > 0 if current.kind == "high" else oi_change < 0
    return DivergenceSignal(
        previous=previous,
        current=current,
        detected=detected,
        state=0 if agrees else 1,
    )


def divergence_series(
    samples: tuple[MarketSample, ...],
    *,
    settings: DivergenceSettings | None = None,
) -> DivergenceSeries:
    """Hold binary state from detection until the next qualifying comparison.

    Returns:
        Unknown state before the first signal and after missing intervals.

    """
    settings = settings or DivergenceSettings()
    points = _momentum(samples, settings)
    states: list[Literal[0, 1] | None] = []
    signals: list[DivergenceSignal] = []
    previous: dict[Literal["high", "low"], SwingPivot] = {}
    state: Literal[0, 1] | None = None
    run_start = 0
    for detected_index, point in enumerate(points):
        if (
            detected_index
            and point.sample.interest.timestamp
            - points[detected_index - 1].sample.interest.timestamp
            != settings.interval
        ):
            previous.clear()
            state = None
            run_start = detected_index
        if detected_index - run_start < _PIVOT_WINDOW:
            states.append(state)
            continue
        pivot = _price_pivot(points, detected_index - 2, settings)
        if pivot is None:
            states.append(state)
            continue
        prior = previous.get(pivot.kind)
        previous[pivot.kind] = pivot
        signal = (
            _compare_swings(prior, pivot, settings, detected=point.available)
            if prior is not None
            else None
        )
        if signal is not None:
            signals.append(signal)
            state = signal.state
        states.append(state)
    return DivergenceSeries(
        points=points, states=tuple(states), signals=tuple(signals)
    )
