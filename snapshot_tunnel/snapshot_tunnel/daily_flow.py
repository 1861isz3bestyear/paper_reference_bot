# Copyright 2026 anchored-vwam-backtest contributors
"""Validate and join flow data using past observations."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO
from itertools import pairwise
from typing import cast

import pandas as pd

from snapshot_tunnel.daily_gex import (
    FlowEvidence,
    FlowSettings,
    normalized_slope,
)
from snapshot_tunnel.validation import numeric_string


@dataclass(frozen=True, kw_only=True)
class RecordedFlow:
    """Keep imported trade-minute and OI receipt histories together."""

    trades: str
    interest: str


def recorded_points(
    content: str, columns: tuple[str, ...]
) -> tuple[FlowPoint, ...]:
    """Require explicit receipts rather than assume historical publication.

    Returns:
        Validated observations with their actual availability times.

    Raises:
        ValueError: If receipt metadata is absent.

    """
    frame = pd.read_csv(StringIO(content))
    if "available_at" not in frame:
        msg = "Recorded flow requires an available_at column."
        raise ValueError(msg)
    return flow_points(frame, columns)


@dataclass(frozen=True, kw_only=True)
class FlowPoint:
    """Represent one validated observation and its availability."""

    time: datetime
    available: datetime
    value: float
    auxiliary: float = 0.0


def flow_points(
    frame: pd.DataFrame,
    columns: tuple[str, ...],
    *,
    delay: timedelta | None = None,
) -> tuple[FlowPoint, ...]:
    """Validate quantities, observation times and optional receipt times.

    Returns:
        Sorted observations; trade timestamps must be completed minute closes.

    Raises:
        ValueError: If fields, timestamps or quantities are invalid.

    """
    if not {"time", *columns}.issubset(frame):
        msg = "Flow data is missing required columns."
        raise ValueError(msg)
    result: list[FlowPoint] = []
    raw = cast("list[dict[str, object]]", frame.to_dict(orient="records"))
    for row in raw:
        observed = pd.Timestamp(str(row["time"]))
        available = pd.Timestamp(
            str(row.get("available_at", observed + (delay or timedelta(0))))
        )
        values = tuple(numeric_string(row[column]) for column in columns)
        if (
            pd.isna(observed)
            or pd.isna(available)
            or observed.tzinfo is None
            or available.tzinfo is None
            or available < observed
        ):
            msg = (
                "Flow requires aware timestamps, causal availability and finite"
                " nonnegative quantities."
            )
            raise ValueError(msg)
        if not all(math.isfinite(value) and value >= 0 for value in values):
            msg = "Flow quantities must be finite and nonnegative."
            raise ValueError(msg)
        result.append(
            FlowPoint(
                time=observed.to_pydatetime(),
                available=available.to_pydatetime(),
                value=values[0],
                auxiliary=values[1] if len(values) > 1 else 0.0,
            )
        )
    if len({point.time for point in result}) != len(result):
        msg = "Duplicate flow observation timestamps."
        raise ValueError(msg)
    return tuple(sorted(result, key=lambda point: point.time))


@dataclass(frozen=True, kw_only=True)
class FlowHistory:
    """Own validated minute trades, OI samples and book totals."""

    trades: tuple[FlowPoint, ...]
    interest: tuple[FlowPoint, ...]
    book: tuple[FlowPoint, ...] = ()
    volume: tuple[FlowPoint, ...] = ()

    def at(self, now: datetime, settings: FlowSettings) -> FlowEvidence:
        """Calculate filters exclusively from already available observations.

        Returns:
            Independent values, with NaN for missing or stale evidence.

        """
        recent = minute_window(
            self.trades,
            end=now,
            duration=settings.volume_recent,
            available_at=now,
        )
        volumes = minute_window(
            self.volume or self.trades,
            end=now,
            duration=settings.volume_recent,
            available_at=now,
        )
        baseline = minute_window(
            self.volume or self.trades,
            end=now - settings.volume_recent,
            duration=settings.volume_baseline,
            available_at=now,
        )
        total = math.fsum(point.value + point.auxiliary for point in recent)
        previous = math.fsum(
            point.value + point.auxiliary for point in baseline
        )
        volume = math.nan
        if volumes and baseline and previous > 0:
            current_volume = math.fsum(
                point.value + point.auxiliary for point in volumes
            )
            volume = (
                current_volume / settings.volume_recent.total_seconds()
            ) / (previous / settings.volume_baseline.total_seconds()) - 1
        books = [
            point
            for point in _observations(self.book, now - settings.book_age, now)
            if point.available <= now and now - point.time <= settings.book_age
        ]
        book = books[-1] if books else None
        return FlowEvidence(
            trades=(
                math.fsum(point.auxiliary - point.value for point in recent)
                / total
                if total > 0
                else math.nan
            ),
            book=(
                (book.value - book.auxiliary) / (book.value + book.auxiliary)
                if book is not None and book.value + book.auxiliary > 0
                else math.nan
            ),
            oi_slope=self._oi_slope(now, settings),
            volume_change=volume,
        )

    def _oi_slope(self, now: datetime, settings: FlowSettings) -> float:
        available = [
            point
            for point in _observations(
                self.interest, now - settings.oi_window - settings.oi_age, now
            )
            if point.available <= now
        ]
        if not available or now - available[-1].time > settings.oi_age:
            return math.nan
        end = available[-1].time
        points = [
            point
            for point in available
            if end - settings.oi_window <= point.time <= end
        ]
        if not points or end - points[0].time < settings.oi_window:
            return math.nan
        if any(
            right.time - left.time > timedelta(minutes=5)
            for left, right in pairwise(points)
        ):
            return math.nan
        return normalized_slope(
            tuple(
                ((point.time - end).total_seconds() / 60, point.value)
                for point in points
            )
        )


def _observations(
    points: tuple[FlowPoint, ...], start: datetime, end: datetime
) -> tuple[FlowPoint, ...]:
    """Select a bounded time window without rescanning the entire archive.

    Returns:
        Observations between the inclusive time limits.

    """
    lower = bisect_left(points, start, key=lambda point: point.time)
    upper = bisect_right(points, end, key=lambda point: point.time)
    return points[lower:upper]


def minute_window(
    points: tuple[FlowPoint, ...],
    *,
    end: datetime,
    duration: timedelta,
    available_at: datetime,
) -> tuple[FlowPoint, ...]:
    """Select complete consecutive minutes known at the decision time.

    Returns:
        A full window, or no usable observations when coverage is incomplete.

    """
    end = end.replace(second=0, microsecond=0)
    selected = tuple(
        point
        for point in _observations(points, end - duration, end)
        if end - duration < point.time <= end
        and point.available <= available_at
    )
    if len(selected) != duration.total_seconds() / 60:
        return ()
    if not selected or selected[-1].time != end:
        return ()
    if any(
        right.time - left.time != timedelta(minutes=1)
        for left, right in pairwise(selected)
    ):
        return ()
    return selected
