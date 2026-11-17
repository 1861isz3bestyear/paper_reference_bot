# Copyright 2026 anchored-vwam-backtest contributors
"""Verify signed wall events and optional negative-GEX long profit ceilings."""

from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from shared.candles import Candle
from shared.spot_execution import SpotAccount, SpotRisk, protective_exit
from shared.tunnel import (
    NegativeGex,
    OiConfirmationMode,
    TunnelLevels,
    TunnelQuote,
    TunnelSettings,
    TunnelState,
    long_target_ceiling,
    regime_state,
)
from shared.tunnel_config import parse_tunnel, tunnel_values
from shared.tunnel_execution import tunnel_entry_decision

NOW = pd.Timestamp("2026-11-17T08:16:00Z")
POSITIVE = TunnelLevels(gex=10, lower=100, upper=110, flip=90)
NEGATIVE = replace(POSITIVE, gex=-10, flip=112)
RISK = SpotRisk(
    fee_pct=0.04,
    slippage_bps=2,
    stop_loss_pct=5,
    take_profit_pct=10,
    minimum_order_size=5,
)


def wall_quote(
    price: float, *, previous: tuple[float, float, float, float]
) -> TunnelQuote:
    """Supply a completed minute of OHLC before the executable quote.

    Returns:
        A quote with causal wall evidence and no current candle range.

    """
    opening, high, low, close = previous
    return TunnelQuote(
        time=NOW.to_pydatetime(),
        price=price,
        previous=Candle(
            time=NOW - timedelta(minutes=1),
            open=opening,
            high=high,
            low=low,
            close=close,
        ),
    )


@pytest.mark.parametrize(
    ("price", "previous", "direction"),
    [
        (100.05, (100.1, 100.2, 99.9, 100.05), 1),
        (109.95, (109.9, 110.1, 109.8, 109.95), -1),
        (100.05, (100.05, 100.1, 100.01, 100.05), 1),
        (110.05, (109.9, 110.1, 109.8, 110.05), -1),
        (99.95, (100.1, 100.2, 99.9, 100.05), 1),
        (110.05, (109.9, 110.1, 109.8, 109.95), -1),
    ],
)
def test_positive_gex_selects_nearby_option_levels(
    price: float, previous: tuple[float, float, float, float], direction: int
) -> None:
    """Select level proximity independently of past or future candle ranges."""
    quote = wall_quote(price, previous=previous)
    assert POSITIVE.direction(quote.price, TunnelSettings()) == direction


@pytest.mark.parametrize("price", [1, 90, 99.95, 105, 110.05, 1000])
def test_negative_gex_has_no_option_level_direction(price: float) -> None:
    """Negative GEX never derives entry direction from option structure."""
    assert NEGATIVE.direction(price, TunnelSettings()) == 0


def test_signed_states_and_automatic_oi_selection() -> None:
    """GEX sign determines OI mode independently of the standalone selector."""
    settings = TunnelSettings(oi_mode=OiConfirmationMode.CONVERGENCE)
    assert POSITIVE.state(settings) == TunnelState.BOTH
    assert NEGATIVE.state(settings) == TunnelState.OSCILLATORS
    assert (
        replace(NEGATIVE, flip=110).state(settings) == TunnelState.OSCILLATORS
    )
    assert replace(NEGATIVE, gex=0).state(settings) == TunnelState.INACTIVE
    assert settings.for_gex(10).oi_mode == OiConfirmationMode.DIVERGENCE
    assert settings.for_gex(-10).oi_mode == OiConfirmationMode.CONVERGENCE
    standalone = replace(settings, use_gex=False)
    assert standalone.for_gex(10) == standalone


def test_wall_evidence_is_causal_and_contiguous() -> None:
    """Missing, future and noncontiguous candles cannot authorize entries."""
    quote = wall_quote(110.05, previous=(109.9, 110.1, 109.8, 110.05))
    assert quote.previous is not None
    for previous in (
        None,
        replace(quote.previous, time=NOW),
        replace(quote.previous, time=NOW - timedelta(minutes=2)),
    ):
        assert replace(quote, previous=previous).preceding() is None


def test_long_target_ceiling_toggle_and_serialization() -> None:
    """Keep the cap independent of signed entry rules and JSON persistence."""
    settings = TunnelSettings(negative_long_tp_offset=0.002)
    assert long_target_ceiling(NEGATIVE, settings) == pytest.approx(111.776)
    disabled = replace(settings, cap_negative_long_tp=False)
    assert parse_tunnel(tunnel_values(disabled)) == disabled
    row = Candle(
        time=NOW,
        open=110.05,
        high=111,
        low=110,
        close=110.5,
        options_direction=1,
        options_target_ceiling=long_target_ceiling(NEGATIVE, settings),
    )
    entry = tunnel_entry_decision(row, RISK)
    assert entry is not None
    assert entry.target == pytest.approx(111.776)
    normal = tunnel_entry_decision(
        replace(
            row, options_target_ceiling=long_target_ceiling(NEGATIVE, disabled)
        ),
        RISK,
    )
    assert normal is not None
    assert pd.isna(normal.target)
    assert pd.isna(long_target_ceiling(POSITIVE, settings))


def test_capped_target_is_fixed_under_flip_and_keeps_stop_priority() -> None:
    """Even opening gaps cannot fill a capped profit above the original flip."""
    row = Candle(
        time=NOW,
        open=110.05,
        high=111,
        low=110,
        close=110.5,
        options_direction=1,
        options_target_ceiling=long_target_ceiling(NEGATIVE, TunnelSettings()),
    )
    entry = tunnel_entry_decision(row, RISK)
    assert entry is not None
    account = SpotAccount(cash=1000, fee_pct=RISK.fee_pct, slippage_bps=2)
    account.open(entry, NOW)
    assert account.position is not None
    gap = replace(
        row,
        time=NOW + timedelta(minutes=1),
        open=115,
        high=116,
        low=114,
        close=115,
    )
    decision = protective_exit(gap, account.position, RISK)
    assert decision is not None
    assert decision.reason == "Take profit"
    assert decision.price < NEGATIVE.flip
    account.close(decision, gap.time)
    assert account.trades[0].exit_price < NEGATIVE.flip
    account.open(entry, NOW)
    assert account.position is not None
    stopped = protective_exit(replace(gap, low=100), account.position, RISK)
    assert stopped is not None
    assert stopped.reason == "Stop loss"
    unsafe = replace(row, open=111.88, options_target_ceiling=111.888)
    assert tunnel_entry_decision(unsafe, RISK) is None


@pytest.mark.parametrize("value", [0, 1, float("nan"), float("inf")])
def test_negative_regime_rejects_invalid_sign(value: float) -> None:
    """Require a finite negative regime at the domain boundary."""
    with pytest.raises(ValueError, match="below zero"):
        _ = NegativeGex(gex=value)


@pytest.mark.parametrize("flip", [0, -1, float("nan"), float("inf")])
def test_negative_regime_rejects_invalid_flip(flip: float) -> None:
    """An optional supplied TP reference must be an actual positive price."""
    with pytest.raises(ValueError, match="positive"):
        _ = NegativeGex(gex=-1, flip=flip)


def test_wall_free_regime_and_optional_cap() -> None:
    """Negative regime needs no walls while its optional TP cap is preserved."""
    settings = TunnelSettings()
    regime = NegativeGex(gex=-10, flip=112)
    assert regime_state(regime, settings) == TunnelState.OSCILLATORS
    assert long_target_ceiling(regime, settings) == pytest.approx(111.888)
    assert pd.isna(long_target_ceiling(NegativeGex(gex=-10), settings))
    assert pd.isna(
        long_target_ceiling(regime, replace(settings, use_gex=False))
    )
    assert regime_state(POSITIVE, settings) == TunnelState.BOTH
