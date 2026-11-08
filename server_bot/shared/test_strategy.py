# Copyright 2026 anchored-vwam-backtest contributors
"""Execution regressions: executable prices, causal bands, fees, and risk."""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Unpack

import numpy as np
import pandas as pd
import pytest

from shared.strategy import backtest
from shared.testing_scenarios import BacktestChanges, BacktestOptions

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shared.models import Trade


def candles(
    rows: Sequence[tuple[float, float, float, float]],
    vwaps: list[float] | None = None,
) -> pd.DataFrame:
    """Provide the candles test fixture.

    Returns:
        The constructed test value.

    """
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["time"] = pd.date_range(
        "2026-09-14", periods=len(df), freq="min", tz="UTC"
    )
    df["anchored_vwap"] = vwaps if vwaps is not None else 100.0
    df["anchored_std"] = 1.0
    return df


def run(
    df: pd.DataFrame, **kwargs: Unpack[BacktestChanges]
) -> tuple[pd.DataFrame, list[Trade]]:
    """Provide the run test fixture.

    Returns:
        The constructed test value.

    """
    args = BacktestOptions(
        strategy_start_at=pd.to_datetime(df["time"], utc=True).iloc[0],
        strategy_end_at=pd.to_datetime(df["time"], utc=True).iloc[-1],
        initial_capital=1000,
        fee_pct=0,
        stop_loss_pct=0,
        take_profit_pct=0,
        allow_reentry=True,
        strategy_mode="VWAP band mean reversion",
        band_sigma=1,
        exit_band_sigma=2,
        trend_mode=True,
    )
    args.update(kwargs)
    return backtest(df, **args)


@pytest.mark.parametrize(("side", "price"), [("Long", 110), ("Short", 90)])
def test_candles_wholly_beyond_entry_band_never_create_fictional_fills(
    side: str, price: int
) -> None:
    """Verify candles wholly beyond entry band never create fictional fills."""
    df = candles([(price, price + 0.1, price - 0.1, price)] * 20)
    eq, trades = run(df, open_position_side=side)
    assert not trades
    expected_value = 1000
    assert (eq["equity"].astype(float) == expected_value).all()


def test_entry_uses_previous_indicator_and_has_no_first_bar_lookahead() -> None:
    """Verify entry uses previous indicator and has no first bar lookahead."""
    df = candles(
        [
            (100, 101, 99, 100),
            (101, 101.1, 100.9, 101),
            (102, 102.1, 101.9, 102),
        ],
        [100, 500, 1],
    )
    _, trades = run(df, open_position_side="Long")
    assert len(trades) == 1
    assert trades[0].entry_time == pd.to_datetime(df["time"], utc=True).iloc[1]
    expected_value = 101
    assert trades[0].entry_price == expected_value
    expected_value = 102
    assert trades[0].exit_price == expected_value


@pytest.mark.parametrize(
    ("side", "entry", "exit_"), [("Long", 101, 101), ("Short", 99, 99)]
)
def test_both_sides_pay_entry_and_exit_fees_and_mark_equity(
    side: str, entry: int, exit_: int
) -> None:
    """Verify both sides pay entry and exit fees and mark equity."""
    df = candles([
        (100, 100, 100, 100),
        (entry, entry, entry, entry),
        (exit_, exit_, exit_, exit_),
    ])
    eq, trades = run(df, open_position_side=side, fee_pct=1)
    t = trades[0]
    assert t.quantity == pytest.approx(990 / entry)
    assert t.pnl == pytest.approx(-19.9)
    assert eq["equity"].astype(float).iloc[1] == pytest.approx(990)
    assert eq["equity"].astype(float).iloc[-1] == pytest.approx(980.1)


@pytest.mark.parametrize(
    ("side", "entry", "opening"), [("Long", 101, 90), ("Short", 99, 110)]
)
def test_gap_through_stop_executes_at_open_not_unavailable_stop(
    side: str, entry: int, opening: int
) -> None:
    """Verify gap through stop executes at open not unavailable stop."""
    df = candles([
        (100, 100, 100, 100),
        (entry, entry, entry, entry),
        (opening, opening, opening, opening),
    ])
    _, trades = run(df, open_position_side=side, stop_loss_pct=1)
    assert trades[0].exit_price == opening
    assert trades[0].exit_reason == "Stop loss"


@pytest.mark.parametrize(
    ("side", "entry", "opening"), [("Long", 101, 110), ("Short", 99, 90)]
)
def test_gap_through_profit_executes_at_open(
    side: str, entry: int, opening: int
) -> None:
    """Verify gap through profit executes at open."""
    df = candles([
        (100, 100, 100, 100),
        (entry, entry, entry, entry),
        (opening, opening, opening, opening),
    ])
    _, trades = run(df, open_position_side=side, take_profit_pct=1)
    assert trades[0].exit_price == opening
    assert trades[0].exit_reason == "Take profit"


@pytest.mark.parametrize(
    ("side", "row"),
    [("Long", (101, 105, 95, 100)), ("Short", (99, 105, 95, 100))],
)
def test_entry_candle_adverse_stop_wins_over_unknown_intrabar_profit(
    side: str, row: tuple[int, int, int, int]
) -> None:
    """Verify entry candle adverse stop wins over unknown intrabar profit."""
    df = candles([(100, 100, 100, 100), row, (100, 100, 100, 100)])
    _, trades = run(
        df, open_position_side=side, stop_loss_pct=1, take_profit_pct=1
    )
    t = trades[0]
    assert t.exit_time == t.entry_time
    assert t.exit_reason == "Stop loss"
    assert t.pnl < 0


@pytest.mark.parametrize(
    ("trend", "side", "entry", "opening"),
    [
        (True, "Long", 101, 103),
        (True, "Short", 99, 97),
        (False, "Long", 99, 101),
        (False, "Short", 101, 99),
    ],
)
def test_band_exit_gaps_use_real_open(
    *, trend: bool, side: str, entry: int, opening: int
) -> None:
    """Verify band exit gaps use real open."""
    df = candles([
        (100, 100, 100, 100),
        (entry, entry, entry, entry),
        (opening, opening, opening, opening),
    ])
    _, trades = run(
        df,
        trend_mode=trend,
        open_position_side=side,
        exit_band_sigma=2 if trend else 0,
    )
    assert trades[0].exit_price == opening
    assert "exit band" in trades[0].exit_reason


@pytest.mark.parametrize(
    "change",
    [
        BacktestChanges(initial_capital=0),
        BacktestChanges(initial_capital=np.nan),
        BacktestChanges(fee_pct=100),
        BacktestChanges(stop_loss_pct=-1),
        BacktestChanges(minimum_order_size=np.inf),
        BacktestChanges(open_position_side="bad"),
        BacktestChanges(strategy_mode="bad"),
        BacktestChanges(take_profit_pct=np.nan),
        BacktestChanges(exit_band_sigma=np.inf),
    ],
)
def test_invalid_parameters_rejected(change: BacktestChanges) -> None:
    """Verify that invalid backtest parameters are rejected."""
    with pytest.raises(ValueError, match=r".+"):
        _ = run(candles([(100, 100, 100, 100)] * 3), **change)


@pytest.mark.parametrize(
    "kind", ["nan", "range", "zero", "duplicate", "unsorted"]
)
def test_invalid_candles_rejected(kind: str) -> None:
    """Verify invalid candles rejected."""
    df = candles([(100, 100, 100, 100)] * 3)
    if kind == "nan":
        df.loc[1, "close"] = np.nan
    if kind == "range":
        df.loc[1, "close"] = 110
    if kind == "zero":
        df.loc[1, "low"] = 0
    if kind == "duplicate":
        df.loc[1, "time"] = pd.to_datetime(df["time"], utc=True).iloc[0]
    if kind == "unsorted":
        df = df.iloc[::-1]
    with pytest.raises(ValueError, match=r".+"):
        _ = run(df)


def test_input_not_mutated_and_warmup_can_supply_first_order_band() -> None:
    """Verify input not mutated and warmup can supply first order band."""
    df = candles(
        [(100, 100, 100, 100), (101, 101, 101, 101), (102, 102, 102, 102)]
    )
    original = df.copy(deep=True)
    _, trades = run(
        df, strategy_start_at=pd.to_datetime(df["time"], utc=True).iloc[1]
    )
    expected_value = 101
    assert trades[0].entry_price == expected_value
    pd.testing.assert_frame_equal(df, original)


@pytest.mark.parametrize(
    "case",
    [
        (
            "Short",
            (100, 101, 100, 101),
            (100.5, 100.5, 99.3, 99.3),
            101,
            99.3,
            "Reached -0.7\N{GREEK SMALL LETTER SIGMA} exit band",
        ),
        (
            "Long",
            (100, 100, 99, 99),
            (99.5, 100.7, 99.5, 100.7),
            99,
            100.7,
            "Reached +0.7\N{GREEK SMALL LETTER SIGMA} exit band",
        ),
    ],
)
def test_negative_close_sigma_crosses_vwap_to_mirrored_profit_band(
    *,
    case: tuple[
        str,
        tuple[int, int, int, int],
        tuple[float, float, float, float],
        int,
        float,
        str,
    ],
) -> None:
    """Verify negative close sigma crosses vwap to mirrored profit band."""
    side, entry_row, exit_row, entry_price, exit_price, reason = case
    df = candles([(100, 100, 100, 100), entry_row, exit_row])

    _, trades = run(
        df,
        trend_mode=False,
        open_position_side=side,
        exit_band_sigma=-0.7,
    )

    assert len(trades) == 1
    assert trades[0].side == side
    assert trades[0].entry_price == entry_price
    assert trades[0].exit_price == pytest.approx(exit_price)
    assert trades[0].exit_reason == reason


def test_negative_close_sigma_is_rejected_outside_mean_reversion() -> None:
    """Verify negative close sigma is rejected outside mean reversion."""
    df = candles([(100, 100, 100, 100)] * 3)
    with pytest.raises(ValueError, match="only valid for mean reversion"):
        _ = run(df, strategy_mode="AVWAP crossover", exit_band_sigma=-0.7)


@pytest.mark.parametrize(
    ("stop_pct", "exit_price", "reason"),
    [(1, 99.99, "Stop loss"), (0, 102.01, "Take profit")],
)
def test_protective_exits_precede_band_exits_and_prevent_same_bar_reentry(
    stop_pct: int, exit_price: float, reason: str
) -> None:
    """Keep stop, profit, and band priority when a candle reaches all orders."""
    df = candles([
        (100, 100, 100, 100),
        (101, 101, 101, 101),
        (101, 110, 95, 101),
    ])
    equity, trades = run(
        df,
        open_position_side="Long",
        stop_loss_pct=stop_pct,
        take_profit_pct=1,
    )

    assert len(trades) == 1
    assert trades[0].exit_reason == reason
    assert trades[0].exit_price == pytest.approx(exit_price)
    assert equity["position"].iloc[-1] == 0
    assert equity["equity"].iloc[-1] == pytest.approx(1000 + trades[0].pnl)


@pytest.mark.parametrize(("trend", "price"), [(True, 99), (False, 101)])
def test_short_entry_has_priority_when_both_bands_are_touched(
    *, trend: bool, price: int
) -> None:
    """Preserve short-first ordering in both band strategies."""
    df = candles([
        (100, 100, 100, 100),
        (100, 102, 98, 100),
        (100, 100, 100, 100),
    ])
    _, trades = run(df, trend_mode=trend)

    assert len(trades) == 1
    assert trades[0].side == "Short"
    assert trades[0].entry_price == price


def test_overnight_sessions_reset_crossings_and_close_positions() -> None:
    """A new session can enter above VWAP even without an intervening cross."""
    df = candles([(101, 101, 101, 101)] * 4)
    df["time"] = pd.to_datetime(
        [
            "2026-09-14 20:00",
            "2026-09-15 06:00",
            "2026-09-15 20:00",
            "2026-09-16 06:00",
        ],
        utc=True,
    )
    equity, trades = run(
        df,
        strategy_mode="AVWAP crossover",
        allow_reentry=False,
        active_weekdays={0, 1},
        active_session_start=time(20),
        active_session_end=time(6),
    )

    expected_trades = 2
    assert len(trades) == expected_trades
    assert [trade.entry_time for trade in trades] == df["time"].iloc[
        ::2
    ].tolist()
    assert [trade.exit_time for trade in trades] == df["time"].iloc[
        1::2
    ].tolist()
    assert all(
        trade.exit_reason == "End of scheduled session" for trade in trades
    )
    assert equity["position"].iloc[1::2].tolist() == [0, 0]
