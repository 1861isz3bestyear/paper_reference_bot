# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for bybit demo bot test reference alignment."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pandas as pd
import pytest

from bybit_demo_bot import cli, executor
from bybit_demo_bot import state as execution_state
from bybit_demo_bot.test_bybit_demo import config
from shared.testing_support import ignoring_arguments
from tests.mock_support import mock_child

if TYPE_CHECKING:
    from pathlib import Path

type AlignmentSetup = tuple[cli.ReferenceAlignedDemoBot, Mock]


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AlignmentSetup:
    """Bot.

    Returns:
        The bot result.

    """
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    mock_child(client, "position").return_value = None
    mock_child(client, "last_price").return_value = Decimal(100)
    mock_child(client, "available_usdt").return_value = Decimal(100)
    mock_child(client, "instruments").return_value = {
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}
    }
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        executor, "fetch_completed_linear_klines", Mock(return_value=candles)
    )
    monkeypatch.setattr(
        executor,
        "reference_decision",
        ignoring_arguments(lambda: Mock(side="Long")),
    )
    return (
        cli.ReferenceAlignedDemoBot(
            config(), client, cli.DemoState("2026-01-01T00:00:00+00:00")
        ),
        client,
    )


NOW = datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC)


def test_reenters_consumed_signal_without_vwap_entry_filter(
    setup: AlignmentSetup,
) -> None:
    """Verify reenters consumed signal without vwap entry filter."""
    bot, client = setup
    bot.state.consumed_signal_side = "Buy"
    bot.exit_band = Mock(side_effect=AssertionError("must not filter VWAP TP"))
    assert "opened Buy" in (bot.reconcile_once(NOW) or "")
    mock_child(client, "market_order").assert_called_once()
    assert bot.state.pending_take_profit == "0"
    assert "awaiting entry reconciliation" in (bot.reconcile_once(NOW) or "")
    mock_child(client, "market_order").assert_called_once()


@pytest.mark.parametrize(
    ("side", "stop"), [("Buy", "99.600"), ("Sell", "100.400")]
)
def test_existing_position_removes_tp_and_retains_sl(
    setup: AlignmentSetup, side: str, stop: str
) -> None:
    """Verify existing position removes tp and retains sl."""
    bot, client = setup
    _ = bot.protect_position(
        {"side": side, "avgPrice": "100", "size": "1"}, Decimal(110)
    )
    mock_child(client, "set_protection").assert_called_once_with(
        "BTCUSDT", Decimal(stop), Decimal(0)
    )


def test_zero_stop_disables_stop(setup: AlignmentSetup) -> None:
    """Verify zero stop disables stop."""
    bot, client = setup
    bot.config = config(stop_loss_pct=0)
    _ = bot.protect_position({"side": "Buy", "avgPrice": "100"}, Decimal(110))
    mock_child(client, "set_protection").assert_called_once_with(
        "BTCUSDT", Decimal(0), Decimal(0)
    )


def test_pending_legacy_tp_is_removed_after_fill(setup: AlignmentSetup) -> None:
    """Verify pending legacy tp is removed after fill."""
    bot, client = setup
    bot.state.pending_protection_side = "Buy"
    bot.state.pending_take_profit = "90"
    mock_child(client, "position").return_value = {
        "side": "Buy",
        "avgPrice": "100",
        "size": "1",
    }
    assert "protected" in (bot.reconcile_once(NOW) or "")
    assert bot.state.pending_protection_side is None
    mock_child(client, "market_order").assert_not_called()
    mock_child(client, "set_protection").assert_called_once_with(
        "BTCUSDT", Decimal("99.600"), Decimal(0)
    )


def test_reversal_waits_for_confirmed_flat(setup: AlignmentSetup) -> None:
    """Verify reversal waits for confirmed flat."""
    bot, client = setup
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "avgPrice": "100",
        "size": "1",
    }
    assert "submitted close Sell" in (bot.reconcile_once(NOW) or "")
    mock_child(client, "market_order").assert_called_once_with(
        "BTCUSDT", "Buy", Decimal(1), reduce_only=True
    )
    assert bot.state.last_processed_candle is None
    mock_child(client, "position").return_value = None
    assert "opened Buy" in (bot.reconcile_once(NOW) or "")


def test_protection_failure_emergency_closes_and_halts(
    setup: AlignmentSetup,
) -> None:
    """Verify protection failure emergency closes and halts."""
    bot, client = setup
    bot.state.pending_protection_side = "Buy"
    mock_child(client, "position").return_value = {
        "side": "Buy",
        "avgPrice": "100",
        "size": "1",
    }
    mock_child(client, "set_protection").side_effect = RuntimeError(
        "unavailable"
    )
    with pytest.raises(RuntimeError, match="emergency close submitted"):
        _ = bot.reconcile_once(NOW)
    assert bot.state.halted_reason
    mock_child(client, "market_order").assert_called_once_with(
        "BTCUSDT", "Sell", Decimal(1), reduce_only=True
    )


@pytest.mark.parametrize(
    ("side", "signal"), [("Buy", "Long"), ("Sell", "Short")]
)
def test_exchange_exit_waits_one_candle_and_survives_restart(
    *,
    setup: AlignmentSetup,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    signal: str,
) -> None:
    """Verify exchange exit waits one candle and survives restart."""
    bot, client = setup
    monkeypatch.setattr(
        executor,
        "reference_decision",
        ignoring_arguments(lambda: Mock(side=signal)),
    )
    bot.state.observed_position_side = side
    assert "waiting for next completed candle" in (
        bot.reconcile_once(NOW) or ""
    )
    mock_child(client, "market_order").assert_not_called()
    assert bot.state.last_processed_candle == "2026-01-01T00:01:00+00:00"

    resumed = cli.ReferenceAlignedDemoBot(
        bot.config, client, cli.DemoState.load_or_create(resume=True)
    )
    assert resumed.reconcile_once(NOW) is None
    mock_child(client, "market_order").assert_not_called()
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:02:00Z")}])
    monkeypatch.setattr(
        executor, "fetch_completed_linear_klines", Mock(return_value=candles)
    )
    assert f"opened {side}" in (
        resumed.reconcile_once(datetime(2026, 1, 1, 0, 3, 5, tzinfo=UTC)) or ""
    )
    mock_child(client, "market_order").assert_called_once()


def test_after_exchange_exit_rechecks_signal_instead_of_forcing_reentry(
    setup: AlignmentSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify after exchange exit rechecks signal instead of forcing reentry."""
    bot, client = setup
    bot.state.observed_position_side = "Buy"
    _ = bot.reconcile_once(NOW)
    monkeypatch.setattr(
        executor,
        "reference_decision",
        ignoring_arguments(lambda: Mock(side=None)),
    )
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:02:00Z")}])
    monkeypatch.setattr(
        executor, "fetch_completed_linear_klines", Mock(return_value=candles)
    )
    assert bot.reconcile_once(datetime(2026, 1, 1, 0, 3, 5, tzinfo=UTC)) is None
    mock_child(client, "market_order").assert_not_called()


def test_strategy_reversal_survives_restart_without_extra_candle_delay(
    setup: AlignmentSetup,
) -> None:
    """Verify strategy reversal survives restart without extra candle delay."""
    bot, client = setup
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "avgPrice": "100",
        "size": "1",
    }
    _ = bot.reconcile_once(NOW)
    resumed = cli.ReferenceAlignedDemoBot(
        bot.config, client, cli.DemoState.load_or_create(resume=True)
    )
    assert resumed.state.pending_strategy_close
    mock_child(client, "position").return_value = None
    assert "opened Buy" in (resumed.reconcile_once(NOW) or "")
    assert not resumed.state.pending_strategy_close


@pytest.mark.parametrize(
    "start", ["2026-01-01T00:01:00+00:00", "2026-01-01T00:01:55+00:00"]
)
def test_fresh_start_waits_until_completed_candle_after_launch(
    setup: AlignmentSetup, monkeypatch: pytest.MonkeyPatch, start: str
) -> None:
    """Verify fresh start waits until completed candle after launch."""
    bot, client = setup
    bot.state.launched_at = start
    calculate = Mock(return_value=Mock(side="Long"))
    monkeypatch.setattr(executor, "reference_decision", calculate)
    assert bot.reconcile_once(NOW) is None
    calculate.assert_not_called()
    mock_child(client, "market_order").assert_not_called()
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:02:00Z")}])
    monkeypatch.setattr(
        executor, "fetch_completed_linear_klines", Mock(return_value=candles)
    )
    assert "opened Buy" in (
        bot.reconcile_once(datetime(2026, 1, 1, 0, 3, 5, tzinfo=UTC)) or ""
    )
