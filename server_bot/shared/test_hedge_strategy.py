# Copyright 2026 anchored-vwam-backtest contributors
"""Test hedge strategy."""

from dataclasses import dataclass

import pandas as pd
import pytest

from shared.candles import Candle
from shared.hedge_strategy import (
    HEDGE_MODE,
    HedgeEngine,
    StopOptions,
    stop_price,
)
from shared.strategy import backtest


def row(
    i: int,
    *,
    opening: float = 100,
    high: float = 100.2,
    low: float = 99.8,
    close: float = 100,
) -> Candle:
    """Provide the row test fixture.

    Returns:
        The constructed test value.

    """
    return Candle(
        time=pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=i),
        open=opening,
        high=high,
        low=low,
        close=close,
    )


@dataclass(frozen=True, kw_only=True)
class EngineOptions:
    """Configure a hedge-engine test instance."""

    fee_pct: float = 0.0
    target_sigma: float = 1.2
    stop_loss_pct: float = 5.0
    stop_loss_basis: str = "Position"
    entry_timeout_bars: int = 3
    minimum_order_size: float = 0.0
    take_profit_pct: float = 0.0


def engine(options: EngineOptions | None = None) -> HedgeEngine:
    """Provide the engine test fixture.

    Returns:
        The constructed test value.

    """
    config = options or EngineOptions()
    return HedgeEngine(
        1000,
        config.fee_pct,
        config.target_sigma,
        config.stop_loss_pct,
        config.stop_loss_basis,
        config.entry_timeout_bars,
        config.minimum_order_size,
        take_profit_pct=config.take_profit_pct,
    )


def test_touch_arms_two_limits_and_never_fills_retroactively() -> None:
    """Verify touch arms two limits and never fills retroactively."""
    e = engine()
    _ = e.process(row(0, high=103, low=97), 100, 1)
    assert set(e.pending) == {"Long", "Short"}
    assert not e.positions
    assert not e.history
    assert e.plan is not None
    expected_value = 100
    assert (
        e.plan["legs"]["Long"]["entry_price"]
        == e.plan["legs"]["Short"]["entry_price"]
        == expected_value
    )
    assert e.plan["legs"]["Long"]["take_profit"] == pytest.approx(101.2)
    assert e.plan["legs"]["Short"]["take_profit"] == pytest.approx(98.8)
    _ = e.process(row(1), 100, 1)
    assert set(e.positions) == {"Long", "Short"}
    expected_value = 1000
    assert e.equity(100) == expected_value


def test_both_legs_exit_independently_and_cash_is_not_double_counted() -> None:
    """Verify both legs exit independently and cash is not double counted."""
    e = engine()
    _ = e.process(row(0), 100, 1)
    _ = e.process(row(1), 100, 1)
    _ = e.process(row(2, high=101.3, low=100, close=101), 100, 1)
    assert set(e.positions) == {"Short"}
    assert e.history[0]["side"] == "Long"
    _ = e.process(row(3, high=100, low=98.7, close=99), 100, 1)
    assert not e.positions
    assert not e.pending
    assert e.cash == pytest.approx(1012)
    assert sum(t.pnl for t in e.trades()) == pytest.approx(12)


@pytest.mark.parametrize(
    ("side", "high", "low", "expected"),
    [("Long", 101.1, 100, 101), ("Short", 100, 98.9, 99)],
)
def test_percentage_take_profit_can_close_before_sigma_band(
    *, side: str, high: float, low: float, expected: int
) -> None:
    """Verify percentage take profit can close before sigma band."""
    e = engine(EngineOptions(target_sigma=3, take_profit_pct=1))
    _ = e.process(row(0), 100, 1)
    _ = e.process(row(1, high=100, low=100), 100, 1)

    _ = e.process(row(2, high=high, low=low), 100, 1)

    trade = next(t for t in e.trades() if t.side == side)
    assert trade.exit_price == expected
    assert trade.exit_reason == "Take profit"


@pytest.mark.parametrize(("basis", "risk"), [("Position", 5), ("Deposit", 10)])
def test_sl_budget_includes_both_fees(basis: str, risk: int) -> None:
    """Verify sl budget includes both fees."""
    e = engine(
        EngineOptions(fee_pct=0.1, stop_loss_pct=1, stop_loss_basis=basis)
    )
    _ = e.process(row(0), 100, 1)
    _ = e.process(row(1, high=100, low=100), 100, 1)
    _ = e.process(row(2, high=103, low=97), 100, 1)
    expected_count = 2
    assert len(e.history) == expected_count
    assert all(t["exit_reason"] == "Stop loss" for t in e.history)
    assert [t["pnl"] for t in e.history] == pytest.approx([-risk, -risk])
    assert e.cash == pytest.approx(1000 - 2 * risk)


def test_snapshot_restart_does_not_duplicate_fills_or_trades() -> None:
    """Verify snapshot restart does not duplicate fills or trades."""
    e = engine()
    _ = e.process(row(0), 100, 1)
    _ = e.process(row(1), 100, 1)
    restored = HedgeEngine(**e.snapshot())
    assert not restored.process(row(1), 100, 1)
    assert restored.snapshot() == e.snapshot()
    for i in [2, 3]:
        candle = row(i, high=103, low=97)
        _ = e.process(candle, 100, 1)
        _ = restored.process(candle, 100, 1)
    assert restored.snapshot() == e.snapshot()


def test_pending_leg_expires_and_does_not_rearm_without_fresh_touch() -> None:
    """Verify pending leg expires and does not rearm without fresh touch."""
    e = engine(EngineOptions(entry_timeout_bars=1))
    _ = e.process(row(0), 100, 1)
    _ = e.process(row(1, opening=101, high=101, low=101, close=101), 100, 1)
    assert not e.pending
    assert set(e.positions) == {"Short"}
    e.close_all(101, row(1).time, "test")
    _ = e.process(row(2), 100, 1)
    assert set(e.pending) == {"Long", "Short"}


def test_entry_bar_losses_are_charged_without_same_bar_tp_credit() -> None:
    """Verify entry bar losses are charged without same bar tp credit."""
    e = engine(EngineOptions(stop_loss_pct=1))
    _ = e.process(row(0), 100, 1)
    _ = e.process(row(1, high=103, low=97), 100, 1)
    expected_count = 2
    assert len(e.history) == expected_count
    assert all(t["exit_reason"] == "Stop loss" for t in e.history)


def test_zero_variance_and_insufficient_capital_do_not_arm() -> None:
    """Verify zero variance and insufficient capital do not arm."""
    e = engine(EngineOptions(minimum_order_size=501))
    _ = e.process(row(0), 100, 1)
    assert not e.pending
    e = engine()
    _ = e.process(row(0), 100, 0)
    assert not e.pending


@pytest.mark.parametrize("basis", ["Position", "Deposit"])
def test_backtest_dispatch_opens_both_sides_and_liquidates_each_at_end(
    basis: str,
) -> None:
    """Verify backtest dispatch opens both sides and liquidates each at end."""
    data = pd.DataFrame([vars(row(i)) for i in range(4)])
    data["anchored_vwap"] = 100.0
    data["anchored_std"] = 1.0
    eq, trades = backtest(
        data,
        pd.to_datetime(data["time"], utc=True).iloc[0],
        strategy_end_at=pd.to_datetime(data["time"], utc=True).iloc[-1],
        initial_capital=1000,
        fee_pct=0.1,
        stop_loss_pct=5,
        take_profit_pct=0,
        allow_reentry=True,
        strategy_mode=HEDGE_MODE,
        band_sigma=0,
        exit_band_sigma=1.2,
        stop_loss_basis=basis,
    )
    assert {t.side for t in trades} == {"Long", "Short"}
    assert all(
        t.entry_time == pd.to_datetime(data["time"], utc=True).iloc[2]
        for t in trades
    )
    assert all(t.exit_reason == "End of strategy" for t in trades)
    assert eq["equity"].astype(float).iloc[-1] == pytest.approx(
        1000 + sum(t.pnl for t in trades)
    )
    expected_value = 1000
    assert eq["equity"].astype(float).iloc[-1] < expected_value


def test_stop_budget_validation() -> None:
    """Verify stop budget validation."""
    with pytest.raises(ValueError, match="fees"):
        _ = stop_price(
            100,
            5,
            500,
            options=StopOptions(
                deposit=1000,
                percent=0.1,
                basis="Position",
                fee_pct=0.1,
            ),
        )
    with pytest.raises(ValueError, match="smaller"):
        _ = stop_price(
            100,
            5,
            500,
            options=StopOptions(deposit=1000, percent=100, basis="Deposit"),
        )
    with pytest.raises(ValueError, match="basis"):
        _ = stop_price(
            100,
            5,
            500,
            options=StopOptions(deposit=1000, percent=1, basis="wrong"),
        )


def test_gap_stop_uses_open_and_disabled_stop_stays_disabled() -> None:
    """Verify hedge gap stop uses actual open and disabled stop remains.

    disabled.
    """
    e = engine(EngineOptions(stop_loss_pct=1))
    _ = e.process(row(0), 100, 1)
    _ = e.process(row(1), 100, 1)
    _ = e.process(row(2, opening=90, high=90, low=90, close=90), 100, 1)
    long = next(t for t in e.trades() if t.side == "Long")
    expected_value = 90
    assert long.exit_price == expected_value
    assert long.pnl == pytest.approx(-50)
    assert stop_price(
        100,
        5,
        500,
        options=StopOptions(deposit=1000, percent=0, basis="Position"),
    ) == {
        "Long": None,
        "Short": None,
    }


@pytest.mark.parametrize(("side", "fill"), [("Short", 120.0), ("Long", 80.0)])
def test_gap_fill_persists_affordable_quantity_and_consistent_fees(
    side: str, fill: float
) -> None:
    """Require a capped fill to replace the planned ledger quantity."""
    e = HedgeEngine(100.0, 0.3, 1.2, 5.0)
    _ = e.process(row(0), 100.0, 1.0)
    planned = e.pending[side].copy()
    _ = e.process(
        row(1, opening=fill, high=fill, low=fill, close=fill), 100.0, 1.0
    )
    position = e.positions[side]
    expected = min(planned["quantity"], planned["allocation"] / (fill * 1.003))
    assert position["quantity"] == pytest.approx(expected)
    assert position["allocation"] == pytest.approx(expected * fill * 1.003)
    assert position["allocation"] <= 50.0 + 1e-10
    assert position["entry_fee"] == pytest.approx(expected * fill * 0.003)
    assert e.equity(fill) == pytest.approx(100.0 - position["entry_fee"])
    restored = HedgeEngine(**e.snapshot())
    restored.close_all(fill, row(2).time, "Test close")
    trade = restored.history[0]
    assert trade["quantity"] == pytest.approx(expected)
    assert trade["pnl"] == pytest.approx(-2 * expected * fill * 0.003)
    assert restored.cash == pytest.approx(100.0 + trade["pnl"])


def test_short_gap_stop_loses_exactly_five_percent_of_fifty_usdt_leg() -> None:
    """Verify short gap stop loses exactly five percent of fifty usdt leg."""
    e = HedgeEngine(100.0, 0.3, 1.2, 5.0)
    _ = e.process(row(0), 100.0, 1.0)
    _ = e.process(
        row(1, opening=120.0, high=120.0, low=120.0, close=120.0), 100.0, 1.0
    )
    stop = e.positions["Short"]["stop_price"]
    assert stop is not None
    _ = e.process(
        row(2, opening=120.0, high=stop, low=120.0, close=stop), 100.0, 1.0
    )
    trade = e.history[0]
    assert trade["deposit_size"] == pytest.approx(50.0)
    assert trade["pnl"] == pytest.approx(-2.5)
    assert trade["return_pct"] == pytest.approx(-5.0)
    assert e.cash == pytest.approx(97.5)
