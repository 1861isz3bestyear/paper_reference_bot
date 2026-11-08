# Copyright 2026 anchored-vwam-backtest contributors
"""Verify opening-price execution and risk precedence for options GEX."""

import pytest

from shared.gex_rules import GexHypothesis
from shared.test_strategy import candles, run


def test_gex_enters_at_open_and_respects_direction_restriction() -> None:
    """A later close must neither change direction nor become the fill."""
    data = candles([(110, 120, 80, 90), (110, 120, 100, 115)])
    data["options_gamma_flip"] = 100.0
    _, trades = run(
        data, strategy_mode="GEX", gex_hypothesis=GexHypothesis.FLIP
    )
    assert len(trades) == 1
    assert trades[0].side == "Long"
    assert trades[0].entry_price == data.loc[0, "open"]
    _, restricted = run(
        data,
        strategy_mode="GEX",
        gex_hypothesis=GexHypothesis.FLIP,
        open_position_side="Short",
    )
    assert not restricted


def test_gex_opening_reversal_precedes_later_intrabar_stop() -> None:
    """An opening reversal must execute before a later adverse excursion."""
    data = candles([(110, 111, 109, 110), (99, 105, 50, 80)])
    data["options_gamma_flip"] = 100.0
    _, trades = run(
        data,
        strategy_mode="GEX",
        gex_hypothesis=GexHypothesis.FLIP,
        stop_loss_pct=20,
    )
    assert len(trades) == 1
    assert trades[0].exit_reason == "Options GEX reversal"
    assert trades[0].exit_price == data.loc[1, "open"]


def test_gex_opening_gap_stop_precedes_reversal() -> None:
    """An already breached protective order wins at the opening price."""
    data = candles([(110, 111, 109, 110), (80, 105, 50, 90)])
    data["options_gamma_flip"] = 100.0
    _, trades = run(
        data,
        strategy_mode="GEX",
        gex_hypothesis=GexHypothesis.FLIP,
        stop_loss_pct=20,
    )
    assert trades[0].exit_reason == "Stop loss"
    assert trades[0].exit_price == data.loc[1, "open"]


def test_gex_entry_is_protected_during_its_first_bar() -> None:
    """An opening entry has a stop throughout the subsequent candle."""
    data = candles([(110, 111, 50, 110), (110, 111, 109, 110)])
    data["options_gamma_flip"] = 100.0
    _, trades = run(
        data,
        strategy_mode="GEX",
        gex_hypothesis=GexHypothesis.FLIP,
        stop_loss_pct=20,
    )
    assert trades[0].exit_reason == "Stop loss"
    assert trades[0].exit_price == pytest.approx(88)


def test_gex_rejects_unsupported_leverage() -> None:
    """Do not silently dispatch GEX to an engine without its signals."""
    with pytest.raises(ValueError, match="unleveraged"):
        _ = run(
            candles([(100, 110, 90, 100)]),
            strategy_mode="GEX",
            gex_hypothesis=GexHypothesis.FLIP,
            leverage=2,
        )


def test_gex_sign_experiment_waits_until_following_bar_to_reenter() -> None:
    """Explicit sign-only mode has no same-bar reversal entry."""
    data = candles([(100, 101, 99, 100)] * 4)
    data["options_gex"] = [1.0, -1.0, -1.0, -1.0]
    _, trades = run(
        data, strategy_mode="GEX", gex_hypothesis=GexHypothesis.SIGN
    )
    assert [trade.side for trade in trades] == ["Long", "Short"]
    assert trades[0].exit_time == data.loc[1, "time"]
    assert trades[1].entry_time == data.loc[2, "time"]


def test_unavailable_gex_keeps_existing_position_protected() -> None:
    """Missing exposure blocks new entries but never disables a live stop."""
    data = candles(
        [(100, 101, 99, 100), (100, 101, 80, 95), (100, 101, 99, 100)]
    )
    data["options_gex"] = [1.0, float("nan"), float("nan")]
    _, trades = run(
        data,
        strategy_mode="GEX",
        gex_hypothesis=GexHypothesis.SIGN,
        stop_loss_pct=10,
    )
    assert len(trades) == 1
    assert trades[0].exit_reason == "Stop loss"
    assert trades[0].exit_price == pytest.approx(90)
