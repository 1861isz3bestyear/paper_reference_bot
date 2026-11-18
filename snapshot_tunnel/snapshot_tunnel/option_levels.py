# Copyright 2026 anchored-vwam-backtest contributors
"""Configure the eligible option strikes without changing exposure totals."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class OptionLevelRange:
    """Limit ranked strikes to a symmetric fractional distance from spot."""

    enabled: bool = True
    distance: float = 0.2

    def __post_init__(self) -> None:
        """Reject invalid distances even when the filter is disabled.

        Raises:
            ValueError: If the distance is not finite and between zero and one.

        """
        if not math.isfinite(self.distance) or not 0 < self.distance < 1:
            msg = "Option level range must be finite and between zero and one."
            raise ValueError(msg)

    def includes(self, strike: float, *, spot: float) -> bool:
        """Check whether a strike is eligible, including both boundaries.

        Returns:
            Whether filtering is disabled or the strike is in range.

        """
        return not self.enabled or (
            spot * (1 - self.distance) <= strike <= spot * (1 + self.distance)
        )


DEFAULT_LEVEL_RANGE = OptionLevelRange()
