# Copyright 2026 anchored-vwam-backtest contributors
"""Bounded gamma-flip searches and numerical sensitivity diagnostics."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from snapshot_tunnel.options_model import gamma_flips

if TYPE_CHECKING:
    from snapshot_tunnel.options_model import Snapshot


MIN_INTERVALS = 2
MAX_INTERVALS = 10000


class FlipStatus(StrEnum):
    """Distinguish a completed search from unavailable option data."""

    FOUND = "Crossings found"
    NOT_FOUND = "No crossing found in search range"
    UNAVAILABLE = "Unavailable options data"


@dataclass(frozen=True, kw_only=True)
class FlipSearch:
    """Search all observed strikes or an explicitly selected spot window."""

    width: float | None = None
    intervals: int = 400

    def __post_init__(self) -> None:
        """Reject invalid ranges and impractical grid sizes.

        Raises:
            ValueError: If the range or grid size is unsupported.

        """
        if self.width is not None and (
            not math.isfinite(self.width) or not 0 < self.width < 1
        ):
            msg = "Flip search width must be finite and between zero and one."
            raise ValueError(msg)
        if (
            isinstance(self.intervals, bool)
            or not MIN_INTERVALS <= self.intervals <= MAX_INTERVALS
        ):
            msg = "Flip search intervals must be an integer from 2 to 10000."
            raise ValueError(msg)

    def prices(
        self, spot: float, *, strikes: tuple[float, ...] = ()
    ) -> tuple[float, ...]:
        """Build a grid covering spot and every observed strike.

        Returns:
            Prices including both search boundaries.

        Raises:
            ValueError: If spot or the generated prices are not usable.

        """
        if not math.isfinite(spot) or spot <= 0:
            msg = "Flip search spot must be finite and positive."
            raise ValueError(msg)
        if any(not math.isfinite(strike) or strike <= 0 for strike in strikes):
            msg = "Flip search strikes must be finite and positive."
            raise ValueError(msg)
        lower, upper = min((spot, *strikes)), max((spot, *strikes))
        if self.width is not None:
            lower, upper = spot * (1 - self.width), spot * (1 + self.width)
        prices = tuple(
            sorted(
                {
                    lower + (upper - lower) * index / self.intervals
                    for index in range(self.intervals + 1)
                }
                | {strike for strike in strikes if lower <= strike <= upper}
            )
        )
        if not math.isfinite(prices[-1]) or prices[0] <= 0:
            msg = "Flip search prices exceed the supported numeric range."
            raise ValueError(msg)
        return prices


@dataclass(frozen=True, kw_only=True)
class FlipResult:
    """Retain every detected crossing and its search provenance."""

    search: FlipSearch
    spot: float
    lower: float
    upper: float
    roots: tuple[float, ...]

    @property
    def nearest(self) -> float | None:
        """Choose the closest crossing, breaking exact ties downward."""
        return min(
            self.roots,
            key=lambda price: (abs(price - self.spot), price),
            default=None,
        )

    @property
    def status(self) -> FlipStatus:
        """Describe detection without claiming absence outside the grid."""
        return FlipStatus.FOUND if self.roots else FlipStatus.NOT_FOUND


@dataclass(frozen=True, kw_only=True)
class FlipSensitivity:
    """Compare a selected search with denser and wider searches."""

    baseline: FlipResult
    denser: FlipResult
    wider: FlipResult
    density_agrees: bool
    range_agrees: bool


DEFAULT_SEARCH = FlipSearch()


def search_flips(
    snapshot: Snapshot, *, search: FlipSearch = DEFAULT_SEARCH
) -> FlipResult:
    """Find crossings while retaining the range and grid resolution.

    Returns:
        All detected roots and the configuration used to find them.

    """
    prices = search.prices(
        snapshot.spot, strikes=tuple(o.strike for o in snapshot.options)
    )
    return FlipResult(
        search=search,
        spot=snapshot.spot,
        lower=prices[0],
        upper=prices[-1],
        roots=gamma_flips(snapshot, prices),
    )


def compare_flip_searches(
    snapshot: Snapshot, baseline: FlipResult
) -> FlipSensitivity:
    """Check root detection at finer spacing and over a wider price range.

    Agreement uses a price tolerance of one millionth of spot. Wider-search
    comparison concerns roots inside the original range and the nearest root;
    extra distant roots outside that range do not imply instability.
    For full-strike searches, both diagnostics retain the complete strike range
    and only increase grid density. Finite grids cannot certify that every
    mathematical root was found.

    Returns:
        Diagnostic searches and agreement flags, without changing signals.

    """
    denser = search_flips(
        snapshot,
        search=replace(
            baseline.search,
            intervals=min(MAX_INTERVALS, baseline.search.intervals * 2),
        ),
    )
    wider = search_flips(
        snapshot,
        search=FlipSearch(
            width=(
                None
                if baseline.search.width is None
                else max(
                    baseline.search.width, min(0.95, baseline.search.width * 2)
                )
            ),
            intervals=min(MAX_INTERVALS, baseline.search.intervals * 4),
        ),
    )
    tolerance = snapshot.spot * 1e-6
    within_range = tuple(
        root for root in wider.roots if baseline.lower <= root <= baseline.upper
    )
    original_nearest = () if denser.nearest is None else (denser.nearest,)
    wider_nearest = () if wider.nearest is None else (wider.nearest,)
    return FlipSensitivity(
        baseline=baseline,
        denser=denser,
        wider=wider,
        density_agrees=_matching_roots(
            baseline.roots, denser.roots, tolerance=tolerance
        ),
        range_agrees=(
            _matching_roots(denser.roots, within_range, tolerance=tolerance)
            and _matching_roots(
                original_nearest, wider_nearest, tolerance=tolerance
            )
        ),
    )


def _matching_roots(
    first: tuple[float, ...], second: tuple[float, ...], *, tolerance: float
) -> bool:
    """Compare ordered root sets without conflating different root counts.

    Returns:
        Whether roots correspond within the requested absolute tolerance.

    """
    return len(first) == len(second) and all(
        abs(left - right) <= tolerance
        for left, right in zip(first, second, strict=True)
    )
