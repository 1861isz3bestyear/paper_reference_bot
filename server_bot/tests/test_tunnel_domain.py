# Copyright 2026 anchored-vwam-backtest contributors
"""Verify authoritative backtest behavior in the deployed shared engine."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from shared.tunnel import TunnelLevels, TunnelSettings, TunnelState

LEVELS = TunnelLevels(gex=10, lower=100, upper=110, flip=90)


@pytest.mark.parametrize(
    ("levels", "state"),
    [
        (LEVELS, TunnelState.BOTH),
        (replace(LEVELS, upper=100.2), TunnelState.LONG),
        (replace(LEVELS, flip=105), TunnelState.SHORT),
        (replace(LEVELS, gex=-1), TunnelState.INACTIVE),
        (replace(LEVELS, gex=0), TunnelState.INACTIVE),
        (replace(LEVELS, flip=math.nan), TunnelState.INACTIVE),
        (replace(LEVELS, lower=0), TunnelState.INACTIVE),
    ],
)
def test_tunnel_state(levels: TunnelLevels, state: TunnelState) -> None:
    """Classify all three configurable scenarios and reject inactive chains."""
    if levels.state(TunnelSettings()) != state:
        pytest.fail("Expected levels.state(TunnelSettings()) == state")


@pytest.mark.parametrize(
    ("state", "price", "direction"),
    [
        (TunnelState.LONG, 100, 1),
        (TunnelState.LONG, 110, 0),
        (TunnelState.SHORT, 110, -1),
        (TunnelState.SHORT, 100, 0),
        (TunnelState.BOTH, 110, 1),
        (TunnelState.BOTH, 100, -1),
        (TunnelState.BOTH, 90, 0),
        (TunnelState.BOTH, 105, 0),
    ],
)
def test_reference_border_directions(
    state: TunnelState, price: float, direction: int
) -> None:
    """Match the image's exact border directions, gated by gamma flip."""
    settings = TunnelSettings(state_override=state)
    if LEVELS.direction(price, settings) != direction:
        pytest.fail("Expected LEVELS.direction(price, settings) == direction")
    if replace(LEVELS, gex=-1).direction(price, settings) != 0:
        pytest.fail(
            "Expected replace(LEVELS, gex=-1).direction(price, settings) == 0"
        )


def test_custom_tight_threshold_and_ambiguous_borders() -> None:
    """Configure width and proximity without selecting competing sides."""
    if LEVELS.state(TunnelSettings(tight_width=0.2)) != TunnelState.LONG:
        pytest.fail(
            "Expected LEVELS.state(TunnelSettings(tight_width=0.2)) =="
            " TunnelState.LONG"
        )
    if LEVELS.direction(105, TunnelSettings(proximity=0.1)) != 0:
        pytest.fail(
            "Expected LEVELS.direction(105, TunnelSettings(proximity=0.1)) == 0"
        )
