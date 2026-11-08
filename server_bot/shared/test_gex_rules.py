# Copyright 2026 anchored-vwam-backtest contributors
"""Test GEX hypotheses separately from execution and data collection."""

import math
from dataclasses import replace

import pytest

from shared.gex_rules import GexConfirmation, GexHypothesis, confirmed_direction
from shared.test_strategy import candles, run


@pytest.fixture
def rebound() -> GexConfirmation:
    """Provide a completed bullish rejection of the previously known put level.

    Returns:
        A positive-gamma rebound candidate.

    """
    return GexConfirmation(
        open=100,
        high=105,
        low=99,
        close=104,
        previous_close=101,
        call_wall=110,
        put_wall=100,
        gex=1,
    )


def test_regime_requires_price_confirmation(rebound: GexConfirmation) -> None:
    """Gamma sign alone cannot cause entry in the default hypothesis."""
    assert confirmed_direction(rebound) == 1
    assert confirmed_direction(replace(rebound, low=101)) == 0
    assert confirmed_direction(replace(rebound, close=100.01)) == 0
    assert confirmed_direction(replace(rebound, gex=0)) == 0
    assert math.isnan(confirmed_direction(replace(rebound, put_wall=math.nan)))
    short = replace(rebound, open=110, high=111, low=105, close=106)
    assert confirmed_direction(short) == -1
    assert confirmed_direction(replace(short, gex=-1)) == 0


def test_negative_gamma_requires_crossing(rebound: GexConfirmation) -> None:
    """A breakout requires crossing a buffered level."""
    breakout = replace(rebound, gex=-1, close=112, high=113)
    assert confirmed_direction(breakout) == 1
    assert confirmed_direction(replace(breakout, previous_close=112)) == 0
    breakdown = replace(rebound, gex=-1, close=98, low=97)
    assert confirmed_direction(breakdown) == -1
    assert confirmed_direction(replace(breakdown, previous_close=98)) == 0
    with pytest.raises(ValueError, match="buffer"):
        _ = confirmed_direction(rebound, buffer=-0.1)


def test_default_strategy_does_not_trade_flip_or_sign() -> None:
    """Positive GEX and a flip are insufficient without confirmation."""
    data = candles([(110, 111, 109, 110)] * 3)
    data["options_gex"] = 100
    data["options_gamma_flip"] = 100
    _, trades = run(data, strategy_mode="GEX")
    assert not trades
    data["options_direction"] = [1, -1, -1]
    _, trades = run(data, strategy_mode="GEX")
    assert [trade.side for trade in trades] == ["Long", "Short"]
    assert trades[0].exit_reason == "Options GEX reversal"


def test_missing_flip_never_falls_back_to_sign() -> None:
    """The flip hypothesis refuses entry even when signed GEX is available."""
    data = candles([(110, 111, 109, 110)] * 2)
    data["options_gex"] = 100
    _, trades = run(
        data, strategy_mode="GEX", gex_hypothesis=GexHypothesis.FLIP
    )
    assert not trades
    _, trades = run(
        data, strategy_mode="GEX", gex_hypothesis=GexHypothesis.SIGN
    )
    assert len(trades) == 1


def test_gex_slippage_is_adverse_on_both_sides() -> None:
    """Configured execution costs affect entry and exit for longs and shorts."""
    for direction in (1, -1):
        data = candles([(100, 105, 95, 100)] * 2)
        data["options_direction"] = direction
        _, trades = run(data, strategy_mode="GEX", slippage_bps=100)
        assert len(trades) == 1
        assert trades[0].entry_price == pytest.approx(100 + direction)
        assert trades[0].exit_price == pytest.approx(100 - direction)
        assert trades[0].pnl < 0
