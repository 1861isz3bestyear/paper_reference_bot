# Copyright 2026 anchored-vwam-backtest contributors
"""Fixed snapshot tunnel scenarios and independent directional votes."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Literal

from snapshot_tunnel.option_levels import OptionLevelRange

if TYPE_CHECKING:
    from snapshot_tunnel.candles import Candle

_MINUTE = timedelta(minutes=1)

OSCILLATOR_MAXIMUM = 100
RETEST_LOOKBACK_MAXIMUM = 500


class TunnelState(IntEnum):
    """Reference-image scenarios; zero never authorizes an entry."""

    OSCILLATORS = -1
    INACTIVE = 0
    LONG = 1
    SHORT = 2
    BOTH = 3


class OiConfirmationMode(StrEnum):
    """Select opposing momentum or agreeing raw participation swings."""

    DIVERGENCE = "divergence"
    CONVERGENCE = "convergence"
    MOMENTUM = "momentum"


class RetestMode(StrEnum):
    """Map a spot level touch to an entry confirmation."""

    EITHER = "either"
    REVERSAL = "reversal"
    BREAKOUT = "breakout"


@dataclass(frozen=True, kw_only=True)
class RetestSettings:
    """Configure completed hourly levels and spot opening quote cadence."""

    lookback: int = 48
    interval: Literal["1m", "5m"] = "5m"
    mode: RetestMode = RetestMode.REVERSAL

    def __post_init__(self) -> None:
        """Require a supported hourly lookback.

        Raises:
            ValueError: If the lookback is outside the supported range.

        """
        if not 1 <= self.lookback <= RETEST_LOOKBACK_MAXIMUM:
            msg = "Retest lookback must be between 1 and 500 hours."
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class TunnelSettings:
    """Expose experimental scenario cutoffs and insurance placement."""

    level_range: OptionLevelRange = field(default_factory=OptionLevelRange)
    tight_width: float = 0.005
    proximity: float = 0.001
    insurance_offset: float = 0.001
    negative_long_tp_offset: float = 0.001
    cap_negative_long_tp: bool = True
    state_override: TunnelState | None = None
    use_gex: bool = True
    use_book: bool = True
    use_margin: bool = False
    margin_source: Literal["bitget_growth"] = "bitget_growth"
    use_oi: bool = False
    oi_mode: OiConfirmationMode = OiConfirmationMode.DIVERGENCE
    oi_momentum_minimum: float = 1.0
    votes: int = 1
    long_threshold: float = 60
    short_threshold: float = 40
    maximum_age: timedelta = timedelta(minutes=10)
    retest: RetestSettings | None = None

    def __post_init__(self) -> None:
        """Reject invalid price distances and impossible confirmation counts.

        Raises:
            ValueError: If configuration cannot describe a valid strategy.

        """
        if (
            not math.isfinite(self.oi_momentum_minimum)
            or self.oi_momentum_minimum <= 0
        ):
            msg = "OI momentum minimum must be finite and positive."
            raise ValueError(msg)
        self._validate_distances()
        enabled = sum((
            self.use_book,
            self.use_margin,
            self.use_oi,
            self.retest is not None,
        ))
        if self.votes < 1 and enabled:
            msg = "Trading requires at least one confirmation."
            raise ValueError(msg)
        if self.state_override is not None and self.state_override not in {
            TunnelState.LONG,
            TunnelState.SHORT,
            TunnelState.BOTH,
        }:
            msg = "Positive GEX state override must be state 1, 2 or 3."
            raise ValueError(msg)
        if not 0 <= self.votes <= enabled:
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

    def _validate_distances(self) -> None:
        distances = (
            self.tight_width,
            self.proximity,
            self.insurance_offset,
            self.negative_long_tp_offset,
        )
        if any(
            not math.isfinite(value) or not 0 < value < 1 for value in distances
        ):
            msg = "Tunnel distances must be finite and between zero and one."
            raise ValueError(msg)

    def for_gex(self, gex: float | None) -> TunnelSettings:
        """Select OI automatically for signed GEX or retain standalone mode.

        Returns:
            Effective settings for this observation's GEX regime.

        """
        if not self.use_gex:
            return self
        mode = OiConfirmationMode.DIVERGENCE
        if gex is not None and gex < 0:
            mode = OiConfirmationMode.CONVERGENCE
        return replace(self, oi_mode=mode)


@dataclass(frozen=True, kw_only=True)
class TunnelQuote:
    """An opening quote and the immediately preceding completed candle."""

    time: datetime
    price: float
    previous: Candle | None = None
    interval: timedelta = _MINUTE

    def preceding(self) -> Candle | None:
        """Reject missing, future or noncontiguous candle evidence.

        Returns:
            Only the completed candle ending at this opening quote.

        """
        if self.previous is None:
            return None
        if self.previous.time + self.interval != self.time:
            return None
        return self.previous


@dataclass(frozen=True, kw_only=True)
class TunnelLevels:
    """Validated frozen snapshot prices, independent of live account state."""

    gex: float
    lower: float
    upper: float
    flip: float

    def state(self, settings: TunnelSettings) -> TunnelState:
        """Classify positive wall structure or negative oscillator trading.

        Returns:
            Positive states 1-3 or negative oscillator mode; zero is inactive.

        """
        if math.isfinite(self.gex) and self.gex < 0:
            return TunnelState.OSCILLATORS
        if not all(
            math.isfinite(value)
            for value in (self.gex, self.lower, self.upper, self.flip)
        ):
            return TunnelState.INACTIVE
        if self.gex == 0 or not (
            0 < self.lower <= self.upper and self.flip > 0
        ):
            return TunnelState.INACTIVE
        return self._positive_state(settings)

    def _positive_state(self, settings: TunnelSettings) -> TunnelState:
        """Classify positive-GEX wall rejections above gamma flip.

        Returns:
            Tight lower-wall long, upper-wall short or both wall rejections.

        """
        if self.upper <= self.flip:
            return TunnelState.INACTIVE
        if settings.state_override is not None:
            return settings.state_override
        if self.upper / self.lower - 1 <= settings.tight_width:
            return TunnelState.LONG
        if self.lower <= self.flip:
            return TunnelState.SHORT
        return TunnelState.BOTH

    def direction(self, price: float, settings: TunnelSettings) -> int:
        """Identify a nearby eligible positive-GEX wall.

        Returns:
            A structural direction to be confirmed independently by oscillators.

        """
        state = self.state(settings)
        if state in {TunnelState.INACTIVE, TunnelState.OSCILLATORS}:
            return 0
        if price <= self.flip:
            return 0
        near_lower = abs(price / self.lower - 1) <= settings.proximity
        near_upper = abs(price / self.upper - 1) <= settings.proximity
        long = near_lower and state in {TunnelState.LONG, TunnelState.BOTH}
        short = near_upper and state in {TunnelState.SHORT, TunnelState.BOTH}
        if long == short:
            return 0
        return 1 if long else -1


@dataclass(frozen=True, kw_only=True)
class NegativeGex:
    """Keep a negative regime and optional TP reference without entry walls."""

    gex: float
    flip: float | None = None

    def __post_init__(self) -> None:
        """Require signed finite exposure and a valid optional exit reference.

        Raises:
            ValueError: If the regime or its exit reference is invalid.

        """
        if not math.isfinite(self.gex) or self.gex >= 0:
            msg = "Negative GEX must be finite and below zero."
            raise ValueError(msg)
        if self.flip is not None and (
            not math.isfinite(self.flip) or self.flip <= 0
        ):
            msg = "Gamma flip must be finite and positive when supplied."
            raise ValueError(msg)


type TunnelRegime = TunnelLevels | NegativeGex


def regime_state(regime: TunnelRegime, settings: TunnelSettings) -> TunnelState:
    """Classify a wall-free negative regime or positive option structure.

    Returns:
        The observable strategy state.

    """
    if isinstance(regime, NegativeGex):
        return TunnelState.OSCILLATORS
    return regime.state(settings)


def long_target_ceiling(
    regime: TunnelRegime, settings: TunnelSettings
) -> float:
    """Freeze the optional negative-long TP cap independently of entry walls.

    Returns:
        A ceiling below the entry-time flip, or unavailable at the boundary.

    """
    if (
        not settings.use_gex
        or regime.gex >= 0
        or not settings.cap_negative_long_tp
        or regime.flip is None
    ):
        return math.nan
    return regime.flip * (1 - settings.negative_long_tp_offset)


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
