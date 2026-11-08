# Copyright 2026 anchored-vwam-backtest contributors
"""Fixed snapshot tunnel scenarios and independent directional votes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum

OSCILLATOR_MAXIMUM = 100


class TunnelState(IntEnum):
    """Reference-image scenarios; zero never authorizes an entry."""

    INACTIVE = 0
    LONG = 1
    SHORT = 2
    BOTH = 3


@dataclass(frozen=True, kw_only=True)
class TunnelSettings:
    """Expose experimental scenario cutoffs and insurance placement."""

    tight_width: float = 0.005
    proximity: float = 0.001
    insurance_offset: float = 0.001
    state_override: TunnelState | None = None
    use_book: bool = True
    use_margin: bool = False
    use_oi: bool = False
    votes: int = 1
    long_threshold: float = 60
    short_threshold: float = 40
    maximum_age: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        """Reject invalid price distances and impossible confirmation counts.

        Raises:
            ValueError: If configuration cannot describe a valid strategy.

        """
        distances = (self.tight_width, self.proximity, self.insurance_offset)
        if any(
            not math.isfinite(value) or not 0 < value < 1 for value in distances
        ):
            msg = "Tunnel distances must be finite and between zero and one."
            raise ValueError(msg)
        if (
            not 0
            <= self.votes
            <= sum((self.use_book, self.use_margin, self.use_oi))
        ):
            msg = "Required confirmations must fit the enabled oscillators."
            raise ValueError(msg)
        if (
            not 0
            <= self.short_threshold
            < self.long_threshold
            <= OSCILLATOR_MAXIMUM
        ):
            msg = "Oscillator thresholds require 0 <= short < long <= 100."
            raise ValueError(msg)
        if self.maximum_age <= timedelta(0):
            msg = "Oscillator maximum age must be positive."
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class TunnelLevels:
    """Validated frozen snapshot prices, independent of live account state."""

    gex: float
    lower: float
    upper: float
    flip: float

    def state(self, settings: TunnelSettings) -> TunnelState:
        """Classify tight tunnels first, then the lower border versus flip.

        Returns:
            An inactive state for invalid levels or nonpositive GEX.

        """
        if not all(
            math.isfinite(value)
            for value in (self.gex, self.lower, self.upper, self.flip)
        ):
            return TunnelState.INACTIVE
        if (
            self.gex <= 0
            or not 0 < self.flip < self.upper
            or not 0 < self.lower <= self.upper
        ):
            return TunnelState.INACTIVE
        if settings.state_override is not None:
            return settings.state_override
        if self.upper / self.lower - 1 <= settings.tight_width:
            return TunnelState.LONG
        if self.lower <= self.flip:
            return TunnelState.SHORT
        return TunnelState.BOTH

    def direction(self, price: float, settings: TunnelSettings) -> int:
        """Require an observed quote above flip near a permitted border.

        Returns:
            A signed market direction, or zero for absent/competing setups.

        """
        if price <= self.flip:
            return 0
        state = self.state(settings)
        if state == TunnelState.INACTIVE:
            return 0
        near_lower = abs(price / self.lower - 1) <= settings.proximity
        near_upper = abs(price / self.upper - 1) <= settings.proximity
        if state == TunnelState.LONG:
            return int(near_lower)
        if state == TunnelState.SHORT:
            return -int(near_upper)
        if state == TunnelState.BOTH and near_lower != near_upper:
            return 1 if near_upper else -1
        return 0


def oscillator_vote(rate: float, settings: TunnelSettings) -> int:
    """Translate an available 0-100 oscillator to a directional vote.

    Returns:
        Long at the upper threshold, short at the lower, otherwise neutral.

    """
    if not math.isfinite(rate):
        return 0
    if rate >= settings.long_threshold:
        return 1
    if rate <= settings.short_threshold:
        return -1
    return 0
