# Copyright 2026 anchored-vwam-backtest contributors
"""Causal oscillator votes from saved depth and recorded margin/OI history."""

from __future__ import annotations

import csv
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO
from typing import TYPE_CHECKING

from snapshot_tunnel.bitget import Growth, growth_momentum
from snapshot_tunnel.daily_flow import FlowPoint, recorded_points
from snapshot_tunnel.oi_divergence import (
    DivergenceSeries,
    DivergenceSettings,
    DivergenceSignal,
    InterestSample,
    MarketSample,
    divergence_series,
)
from snapshot_tunnel.tunnel import (
    OiConfirmationMode,
    TunnelLevels,
    TunnelQuote,
    TunnelRegime,
    TunnelSettings,
    oscillator_vote,
)
from snapshot_tunnel.tunnel_oscillators import (
    limit_order_oscillator_rate,
)

_HOURLY_OI = timedelta(hours=1)
BOTH_DIRECTIONS = 2

if TYPE_CHECKING:
    from snapshot_tunnel.options_model import Snapshot


@dataclass(frozen=True, kw_only=True)
class OscillatorObservation:
    """One directional vote at its actual availability time."""

    available: datetime
    direction: int
    calculatable: bool = True
    momentum: float | None = None
    valid_for: timedelta | None = None


def fresh_observation(
    stream: tuple[OscillatorObservation, ...],
    now: datetime,
    *,
    maximum_age: timedelta,
) -> OscillatorObservation | None:
    """Find the most recent calculatable observation available at the quote.

    Returns:
        Fresh evidence, without falling back past a newer invalid observation.

    """
    index = bisect_right(stream, now, key=lambda item: item.available) - 1
    if index < 0:
        return None
    point = stream[index]
    age = now - point.available
    if (
        not point.calculatable
        or age > maximum_age
        or (point.valid_for is not None and age >= point.valid_for)
    ):
        return None
    return point


@dataclass(frozen=True, kw_only=True)
class ConfirmationDecision:
    """An entry's direction and evidence before account mutation."""

    direction: int
    votes: int
    calculatable: bool
    gex_direction: int = 0


@dataclass(frozen=True, kw_only=True)
class TunnelFlow:
    """Independent streams; missing and stale observations never vote."""

    book: tuple[OscillatorObservation, ...] = ()
    margin: tuple[OscillatorObservation, ...] = ()
    oi: tuple[OscillatorObservation, ...] = ()
    oi_convergence: tuple[OscillatorObservation, ...] = ()

    oi_momentum: tuple[OscillatorObservation, ...] = ()
    retest: tuple[OscillatorObservation, ...] = ()

    def oi_stream(
        self, mode: OiConfirmationMode
    ) -> tuple[OscillatorObservation, ...]:
        """Select the independently recorded OI evidence.

        Returns:
            The selected regime stream, without substituting missing data.

        """
        return {
            OiConfirmationMode.DIVERGENCE: self.oi,
            OiConfirmationMode.CONVERGENCE: self.oi_convergence,
            OiConfirmationMode.MOMENTUM: self.oi_momentum,
        }[mode]

    def available(
        self, now: datetime, *, settings: TunnelSettings
    ) -> tuple[OscillatorObservation, ...]:
        """Find usable enabled observations.

        Returns:
            Calculatable observations available within the age limit.

        """
        observations: list[OscillatorObservation] = []
        for enabled, stream in (
            (settings.use_book, self.book),
            (settings.use_margin, self.margin),
            (
                settings.use_oi,
                self.oi_stream(settings.oi_mode),
            ),
            (settings.retest is not None, self.retest),
        ):
            if not enabled:
                continue
            point = fresh_observation(
                stream, now, maximum_age=settings.maximum_age
            )
            if point is not None:
                observations.append(point)
        return tuple(observations)

    def entry(
        self,
        quote: TunnelQuote,
        *,
        settings: TunnelSettings,
        levels: TunnelRegime | None,
    ) -> ConfirmationDecision:
        """Use positive wall proximity or negative oscillator-only entries.

        Returns:
            An auditable decision without changing account state.

        """
        now = quote.time
        settings = settings.for_gex(levels.gex if levels else None)
        gex_direction = (
            levels.direction(quote.price, settings)
            if settings.use_gex and isinstance(levels, TunnelLevels)
            else 0
        )
        required = max(1, settings.votes)
        calculatable = len(
            self.available(now, settings=settings)
        ) >= required and (
            not settings.use_gex
            or (
                levels is not None
                and math.isfinite(levels.gex)
                and levels.gex != 0
            )
        )
        if not calculatable:
            return ConfirmationDecision(
                direction=0,
                votes=0,
                calculatable=False,
                gex_direction=gex_direction,
            )
        if settings.use_gex and levels is not None and levels.gex > 0:
            direction = gex_direction
        else:
            direction = self.direction(now, settings=settings)
        votes = (
            self.count(direction, now, settings=settings) if direction else 0
        )
        return ConfirmationDecision(
            direction=(
                direction
                if votes >= required
                and self._oi_confirms(direction, now, settings=settings)
                else 0
            ),
            votes=votes,
            calculatable=True,
            gex_direction=gex_direction,
        )

    def _oi_confirms(
        self, direction: int, now: datetime, *, settings: TunnelSettings
    ) -> bool:
        """Require enabled OI to match the order in the signed GEX regime.

        Returns:
            Whether the mandatory regime-specific OI condition is satisfied.

        """
        if not settings.use_gex or not settings.use_oi:
            return True
        point = fresh_observation(
            self.oi_stream(settings.oi_mode),
            now,
            maximum_age=settings.maximum_age,
        )
        return (
            point is not None
            and direction != 0
            and point.direction == direction
        )

    def confirmations(
        self, now: datetime, *, settings: TunnelSettings
    ) -> dict[str, int]:
        """Return each enabled oscillator's fresh vote for timeline markers.

        Returns:
            Signed confirmations; unavailable and neutral evidence is zero.

        """
        streams = (
            (
                "retest",
                settings.retest is not None,
                TunnelFlow(retest=self.retest),
            ),
            ("book", settings.use_book, TunnelFlow(book=self.book)),
            ("margin", settings.use_margin, TunnelFlow(margin=self.margin)),
            (
                (
                    "oi"
                    if settings.oi_mode == OiConfirmationMode.DIVERGENCE
                    else f"oi_{settings.oi_mode}"
                ),
                settings.use_oi,
                TunnelFlow(
                    oi=self.oi,
                    oi_convergence=self.oi_convergence,
                    oi_momentum=self.oi_momentum,
                ),
            ),
        )
        return {
            f"tunnel_confirmation_{name}": next(
                (
                    (
                        int(point.momentum >= settings.oi_momentum_minimum)
                        if point.momentum is not None
                        else point.direction
                    )
                    for point in flow.available(now, settings=settings)
                ),
                0,
            )
            for name, enabled, flow in streams
            if enabled
        }

    def direction(self, now: datetime, *, settings: TunnelSettings) -> int:
        """Choose an unambiguous oscillator quorum without a GEX prerequisite.

        Returns:
            A signed direction, or zero when neither or both sides qualify.

        """
        observations = self.available(now, settings=settings)
        long = sum(point.direction == 1 for point in observations)
        short = sum(point.direction == -1 for point in observations)
        either = sum(
            point.direction == BOTH_DIRECTIONS for point in observations
        )
        required = max(1, settings.votes) - either
        if (
            either
            and (long == 0) != (short == 0)
            and max(long, short) >= required
        ):
            return 1 if long else -1
        if (long >= required) == (short >= required):
            return 0
        return 1 if long >= required else -1

    def count(
        self, direction: int, now: datetime, *, settings: TunnelSettings
    ) -> int:
        """Count fresh, independent confirmations.

        Returns:
            Number of usable observations agreeing with the direction.

        """
        return sum(
            point.direction == direction
            or (direction != 0 and point.direction == BOTH_DIRECTIONS)
            or (
                direction != 0
                and point.momentum is not None
                and point.momentum >= settings.oi_momentum_minimum
            )
            for point in self.available(now, settings=settings)
        )


def saved_book_votes(
    snapshots: tuple[Snapshot, ...], settings: TunnelSettings
) -> tuple[OscillatorObservation, ...]:
    """Calculate bid-notional share using only saved, fresh BTC depth.

    Returns:
        Chronological votes at book receipt times, without synthetic backfill.

    """
    books = {
        snapshot.order_book.available: snapshot.order_book
        for snapshot in snapshots
        if snapshot.asset == "BTC"
        and snapshot.order_book is not None
        and snapshot.order_book.fresh(timedelta(seconds=10))
        and snapshot.order_book.available <= snapshot.timestamp
    }
    return tuple(
        OscillatorObservation(
            available=stamp,
            direction=oscillator_vote(
                limit_order_oscillator_rate(books[stamp]), settings
            ),
        )
        for stamp in sorted(books)
    )


def recorded_growth(content: str) -> tuple[FlowPoint, ...]:
    """Validate signed Bitget growth and explicit receipt timestamps.

    Returns:
        Chronological growth observations.

    Raises:
        ValueError: If schema, numbers, timestamps or duplicates are invalid.

    """
    rows = csv.reader(StringIO(content))
    if next(rows, None) != ["time", "available_at", "growth_rate"]:
        msg = "Bitget CSV requires time,available_at,growth_rate columns."
        raise ValueError(msg)
    points: list[FlowPoint] = []
    for row in rows:
        if len(row) != len(("time", "available_at", "growth_rate")):
            msg = "Bitget growth rows require three fields."
            raise ValueError(msg)
        observed = datetime.fromisoformat(row[0])
        available = datetime.fromisoformat(row[1])
        growth = float(row[2])
        if (
            observed.tzinfo is None
            or available.tzinfo is None
            or available < observed
            or not math.isfinite(growth)
        ):
            msg = "Bitget growth requires finite values and causal aware times."
            raise ValueError(msg)
        points.append(
            FlowPoint(time=observed, available=available, value=growth)
        )
    if len({point.time for point in points}) != len(points):
        msg = "Duplicate Bitget growth timestamps."
        raise ValueError(msg)
    return tuple(sorted(points, key=lambda point: point.time))


def margin_votes(
    content: str, settings: TunnelSettings
) -> tuple[OscillatorObservation, ...]:
    """Compute Bitget momentum after fourteen contiguous hourly samples.

    Returns:
        Receipt-stamped votes, with unknown votes during warmup and gaps.

    """
    points = recorded_growth(content)
    rates = growth_momentum(
        tuple(
            Growth(timestamp=point.time, growth=point.value) for point in points
        ),
        period="24h",
    )
    votes = tuple(
        OscillatorObservation(
            available=max(item.available for item in points[: index + 1]),
            calculatable=rate is not None,
            direction=oscillator_vote(
                rate if rate is not None else math.nan, settings
            ),
        )
        for index, rate in enumerate(rates)
    )
    return tuple(sorted(votes, key=lambda item: item.available))


def oi_votes(
    content: str, *, mode: OiConfirmationMode = OiConfirmationMode.DIVERGENCE
) -> tuple[OscillatorObservation, ...]:
    """Convert recorded OI into causal swing or momentum entry evidence.

    Returns:
        Divergence reverses price swings; raw OI convergence follows them.
        Momentum measures participation for either breakout direction.
        Signals from the other regime clear the vote. Gaps reset evidence.

    """
    points = recorded_points(content, ("open_interest", "price"))
    samples = tuple(
        MarketSample(
            interest=InterestSample(timestamp=point.time, value=point.value),
            price=point.auxiliary,
            received=(
                point.available if mode == OiConfirmationMode.MOMENTUM else None
            ),
        )
        for point in points
    )
    series = oi_confirmation_series(samples, mode=mode)
    if mode == OiConfirmationMode.MOMENTUM:
        return momentum_votes(series)
    signals = {signal.detected: signal for signal in series.signals}
    votes: list[OscillatorObservation] = []
    direction = 0
    for index, point in enumerate(series.points):
        if series.states[index] is None:
            direction = 0
        signal = signals.get(point.available)
        if signal is not None:
            direction = oi_signal_direction(signal, mode=mode)
        votes.append(
            OscillatorObservation(
                available=max(
                    point.available,
                    *(item.available for item in points[: index + 1]),
                ),
                direction=direction,
                calculatable=series.states[index] is not None,
            )
        )
    return tuple(sorted(votes, key=lambda item: item.available))


def oi_confirmation_series(
    samples: tuple[MarketSample, ...],
    *,
    mode: OiConfirmationMode,
    interval: timedelta = _HOURLY_OI,
) -> DivergenceSeries:
    """Calculate causal swings using momentum or raw OI comparisons.

    Returns:
        A series whose comparisons match the selected confirmation regime.

    """
    return divergence_series(
        samples,
        settings=DivergenceSettings(
            interval=interval,
            comparison=(
                "open_interest"
                if mode == OiConfirmationMode.CONVERGENCE
                else "momentum"
            ),
        ),
    )


def oi_signal_direction(
    signal: DivergenceSignal, *, mode: OiConfirmationMode
) -> int:
    """Accept only the chosen regime, clearing an opposing regime's vote.

    Returns:
        Divergence reverses the swing; convergence follows the price swing.

    """
    divergent = mode == OiConfirmationMode.DIVERGENCE
    if bool(signal.state) != divergent:
        return 0
    trend = 1 if signal.current.kind == "high" else -1
    return -trend if divergent else trend


def momentum_votes(
    series: DivergenceSeries,
) -> tuple[OscillatorObservation, ...]:
    """Expose causal smoothed 12-hour OI growth without waiting for pivots.

    Returns:
        Participation evidence usable by either breakout direction.

    """
    votes: list[OscillatorObservation] = []
    available: datetime | None = None
    for point in series.points:
        available = (
            max(available, point.available) if available else point.available
        )
        votes.append(
            OscillatorObservation(
                available=available,
                direction=0,
                calculatable=point.rate is not None,
                momentum=point.rate,
            )
        )
    return tuple(sorted(votes, key=lambda vote: vote.available))
