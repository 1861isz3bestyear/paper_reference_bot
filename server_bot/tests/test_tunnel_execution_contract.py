# Copyright 2026 anchored-vwam-backtest contributors
"""Exercise funded tunnel legs, protective precedence and replay settlement."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from shared.candles import Candle
from shared.spot_execution import SpotAccount, SpotRisk
from shared.strategy_data import BacktestWindow, PreparedBacktest
from shared.testing_support import ignoring_arguments
from shared.tunnel_execution import (
    TunnelEngine,
    TunnelExecutionSettings,
    run_tunnel_backtest,
)

NOW = pd.Timestamp("2026-01-01", tz="UTC")


def engine(*, reentry: bool = True) -> TunnelEngine:
    """Create independently funded main and insurance accounts.

    Returns:
        The configured test value.

    """
    return TunnelEngine(
        main=SpotAccount(cash=500, fee_pct=0),
        insurance=SpotAccount(cash=500, fee_pct=0),
        settings=TunnelExecutionSettings(
            risk=SpotRisk(
                fee_pct=0,
                stop_loss_pct=5,
                take_profit_pct=10,
                minimum_order_size=1,
            ),
            allow_reentry=reentry,
        ),
    )


def candle(index: int = 0) -> Candle:
    """Offer an executable long with a fixed short insurance trigger.

    Returns:
        The configured test value.

    """
    return Candle(
        time=NOW + pd.Timedelta(minutes=index),
        open=100,
        high=101,
        low=99,
        close=100,
        options_direction=1,
        options_insurance=97,
    )


def test_insurance_activation_and_settlement() -> None:
    """Fund both positions once and cancel insurance after the main closes."""
    bot = engine()
    bot.process(candle())
    if not (bot.main.position is not None):
        pytest.fail("Expected bot.main.position is not None")
    if bot.insurance.position is not None:
        pytest.fail("Expected bot.insurance.position is None")
    bot.process(replace(candle(1), low=96, close=97))
    if not (bot.insurance.position is not None):
        pytest.fail("Expected bot.insurance.position is not None")
    if not (math.isnan(bot.trigger)):
        pytest.fail("Expected math.isnan(bot.trigger)")
    bot.process(replace(candle(2), high=111, low=96, close=110))
    bot.close(candle(3), session=False)
    if bot.main.mark(100).quantity != 0:
        pytest.fail("Expected bot.main.position is None")
    if bot.insurance.mark(100).quantity != 0:
        pytest.fail("Expected bot.insurance.position is None")
    if len(bot.main.trades) != 1:
        pytest.fail("Expected len(bot.main.trades) == 1")
    if len(bot.insurance.trades) != 1:
        pytest.fail("Expected len(bot.insurance.trades) == 1")


def test_stop_precedes_insurance() -> None:
    """A higher main stop executes before a lower insurance trigger."""
    bot = engine()
    bot.process(replace(candle(), options_insurance=94))
    bot.process(replace(candle(1), low=93, close=94))
    if bot.main.position is not None:
        pytest.fail("Expected bot.main.position is None")
    if bot.insurance.position is not None:
        pytest.fail("Expected bot.insurance.position is None")
    if bot.main.trades[0].exit_reason != "Stop loss":
        pytest.fail("Expected bot.main.trades[0].exit_reason == 'Stop loss'")
    if not (math.isnan(bot.trigger)):
        pytest.fail("Expected math.isnan(bot.trigger)")


def test_tunnel_entry_guards() -> None:
    """Reject absent signals, disallowed sides and unfunded entries."""
    bot = engine()
    bot.process(replace(candle(), options_direction=0))
    bot.process(replace(candle(), options_insurance=math.nan))
    bot.settings = replace(bot.settings, side="Short")
    bot.process(candle())
    if bot.main.position is not None:
        pytest.fail("Expected bot.main.position is None")
    bot.settings = replace(bot.settings, side="Both")
    bot.main.cash = bot.insurance.cash = 0
    bot.process(candle())
    if bot.main.position is not None:
        pytest.fail("Expected bot.main.position is None")


def test_tunnel_single_entry_and_insufficient_insurance() -> None:
    """Disable repeat entries and leave unaffordable insurance unfilled."""
    bot = engine(reentry=False)
    bot.process(candle())
    bot.insurance.cash = 0
    bot.process(replace(candle(1), low=96))
    if bot.insurance.position is not None:
        pytest.fail("Expected bot.insurance.position is None")
    bot.close(candle(2), session=True)
    bot.process(candle(3))
    if bot.main.position is not None:
        pytest.fail("Expected bot.main.position is None")
    if bot.main.trades[0].exit_reason != "End of scheduled session":
        pytest.fail(
            "Expected bot.main.trades[0].exit_reason == 'End of scheduled"
            " session'"
        )


@pytest.mark.parametrize("session", [False, True])
def test_tunnel_replay_combines_both_ledgers(*, session: bool) -> None:
    """Return settled equity and labeled insurance trades within the window."""
    data = pd.DataFrame({
        "time": pd.date_range(NOW, periods=3, freq="min"),
        "open": [100, 100, 97],
        "high": [101, 101, 98],
        "low": [99, 96, 96],
        "close": [100, 97, 97],
        "options_direction": 1,
        "options_insurance": 97,
    })
    prepared = PreparedBacktest(
        data=data,
        scheduled_days=(
            pd.Series([0] * 3).map(ignoring_arguments(lambda: date(2026, 1, 1)))
            if session
            else None
        ),
        window=BacktestWindow(start=NOW, end=NOW + pd.Timedelta(minutes=2)),
    )
    equity, trades = run_tunnel_backtest(
        prepared, 1000, settings=engine().settings
    )
    expected_value = 3
    if len(equity) != expected_value:
        pytest.fail("Expected len(equity) == 3")
    expected_value = 2
    if len(trades) != expected_value:
        pytest.fail("Expected len(trades) == 2")
    if equity.position.iloc[-1] != 0:
        pytest.fail("Expected equity.position.iloc[-1] == 0")
    if not (
        any(trade.exit_reason.startswith("Insurance:") for trade in trades)
    ):
        pytest.fail(
            "Expected any((trade.exit_reason.startswith('Insurance:') for trade"
            " in trades))"
        )
    if equity.equity.iloc[-1] != pytest.approx(
        1000 + sum(trade.pnl for trade in trades)
    ):
        pytest.fail(
            "Expected equity.equity.iloc[-1] == pytest.approx(1000 +"
            " sum((trade.pnl for trade in trades)))"
        )
