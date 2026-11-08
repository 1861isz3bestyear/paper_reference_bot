# Copyright 2026 anchored-vwam-backtest contributors
"""Explicit, independently testable GEX entry hypotheses."""

import math
from dataclasses import dataclass
from enum import StrEnum


class GexHypothesis(StrEnum):
    """Separate regime confirmation from experimental directional rules."""

    REGIME = "Regime with price confirmation"
    TUNNEL = "Snapshot tunnel with oscillator confirmation"
    DAILY = "Daily wall plan with flow confirmation"
    FLIP = "Experimental: price versus flip"
    SIGN = "Experimental: GEX sign"


@dataclass(frozen=True, kw_only=True)
class GexConfirmation:
    """A completed candle and levels published before that candle."""

    open: float
    high: float
    low: float
    close: float
    previous_close: float
    call_wall: float
    put_wall: float
    gex: float


def confirmed_direction(
    bar: GexConfirmation, *, buffer: float = 0.001
) -> float:
    """Require a rejected level in positive gamma or breakout in negative gamma.

    Returns:
        Signed direction, zero without confirmation, NaN without usable data.

    Raises:
        ValueError: If the relative confirmation buffer is invalid.

    """
    if not math.isfinite(buffer) or not 0 <= buffer < 1:
        msg = "GEX confirmation buffer must be in [0, 1)."
        raise ValueError(msg)
    if not all(
        math.isfinite(value)
        for value in (
            bar.gex,
            bar.call_wall,
            bar.put_wall,
            bar.previous_close,
        )
    ):
        return math.nan
    upper = bar.call_wall * (1 + buffer)
    lower = bar.put_wall * (1 - buffer)
    if bar.gex > 0:
        long = (
            bar.low <= bar.put_wall
            and bar.close > bar.put_wall * (1 + buffer)
            and bar.close > bar.open
        )
        short = (
            bar.high >= bar.call_wall
            and bar.close < bar.call_wall * (1 - buffer)
            and bar.close < bar.open
        )
    elif bar.gex < 0:
        long = (
            bar.previous_close <= upper
            and bar.close > upper
            and bar.close > bar.open
        )
        short = (
            bar.previous_close >= lower
            and bar.close < lower
            and bar.close < bar.open
        )
    else:
        return 0.0
    if long == short:
        return 0.0
    return 1.0 if long else -1.0
