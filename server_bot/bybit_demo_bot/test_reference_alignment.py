# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for bybit demo bot test reference alignment."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from bybit_demo_bot import cli
from bybit_demo_bot.test_bybit_demo import config


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Bot."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = None
    client.last_price.return_value = Decimal(100)
    client.available_usdt.return_value = Decimal(100)
    client.instruments.return_value = {
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}
    }
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        cli, "fetch_completed_linear_klines", lambda *_: candles
    )
    monkeypatch.setattr(cli, "reference_decision", lambda *_: Mock(side="Long"))
    return cli.ReferenceAlignedDemoBot(
        config(), client, cli.DemoState("2026-01-01T00:00:00+00:00")
    )


NOW = datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC)


def test_reenters_consumed_signal_without_vwap_entry_filter(bot) -> None:
    """Verify reenters consumed signal without vwap entry filter."""
    bot.state.consumed_signal_side = "Buy"
    bot._exit_band = Mock(side_effect=AssertionError("must not filter VWAP TP"))
    if "opened Buy" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'opened Buy' in bot.reconcile_once(NOW)")
    bot.client.market_order.assert_called_once()
    if bot.state.pending_take_profit != "0":
        pytest.fail("Expected bot.state.pending_take_profit == '0'")
    if "awaiting entry reconciliation" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'awaiting entry reconciliation' in"
            " bot.reconcile_once(NOW)"
        )
    bot.client.market_order.assert_called_once()


@pytest.mark.parametrize("side,stop", [("Buy", "99.600"), ("Sell", "100.400")])
def test_existing_position_removes_tp_and_retains_sl(bot, side, stop) -> None:
    """Verify existing position removes tp and retains sl."""
    bot._protect({"side": side, "avgPrice": "100", "size": "1"}, Decimal(110))
    bot.client.set_protection.assert_called_once_with(
        "BTCUSDT", Decimal(stop), Decimal(0)
    )


def test_zero_stop_disables_stop(bot) -> None:
    """Verify zero stop disables stop."""
    bot.config = config(stop_loss_pct=0)
    bot._protect({"side": "Buy", "avgPrice": "100"}, Decimal(110))
    bot.client.set_protection.assert_called_once_with(
        "BTCUSDT", Decimal(0), Decimal(0)
    )


def test_pending_legacy_tp_is_removed_after_fill(bot) -> None:
    """Verify pending legacy tp is removed after fill."""
    bot.state.pending_protection_side = "Buy"
    bot.state.pending_take_profit = "90"
    bot.client.position.return_value = {
        "side": "Buy",
        "avgPrice": "100",
        "size": "1",
    }
    if "protected" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'protected' in bot.reconcile_once(NOW)")
    if (bot.state.pending_protection_side) is not (None):
        pytest.fail("Expected bot.state.pending_protection_side is None")
    bot.client.market_order.assert_not_called()
    bot.client.set_protection.assert_called_once_with(
        "BTCUSDT", Decimal("99.600"), Decimal(0)
    )


def test_reversal_waits_for_confirmed_flat(bot) -> None:
    """Verify reversal waits for confirmed flat."""
    bot.client.position.return_value = {
        "side": "Sell",
        "avgPrice": "100",
        "size": "1",
    }
    if "submitted close Sell" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'submitted close Sell' in bot.reconcile_once(NOW)"
        )
    bot.client.market_order.assert_called_once_with(
        "BTCUSDT", "Buy", Decimal(1), reduce_only=True
    )
    if (bot.state.last_processed_candle) is not (None):
        pytest.fail("Expected bot.state.last_processed_candle is None")
    bot.client.position.return_value = None
    if "opened Buy" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'opened Buy' in bot.reconcile_once(NOW)")


def test_protection_failure_emergency_closes_and_halts(bot) -> None:
    """Verify protection failure emergency closes and halts."""
    bot.state.pending_protection_side = "Buy"
    bot.client.position.return_value = {
        "side": "Buy",
        "avgPrice": "100",
        "size": "1",
    }
    bot.client.set_protection.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError, match="emergency close submitted"):
        bot.reconcile_once(NOW)
    if not (bot.state.halted_reason):
        pytest.fail("Expected bot.state.halted_reason")
    bot.client.market_order.assert_called_once_with(
        "BTCUSDT", "Sell", Decimal(1), reduce_only=True
    )


@pytest.mark.parametrize("side,signal", [("Buy", "Long"), ("Sell", "Short")])
def test_exchange_exit_waits_one_candle_and_survives_restart(
    bot, monkeypatch: pytest.MonkeyPatch, side, signal
) -> None:
    """Verify exchange exit waits one candle and survives restart."""
    monkeypatch.setattr(cli, "reference_decision", lambda *_: Mock(side=signal))
    bot.state.observed_position_side = side
    if "waiting for next completed candle" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'waiting for next completed candle' in"
            " bot.reconcile_once(NOW)"
        )
    bot.client.market_order.assert_not_called()
    if bot.state.last_processed_candle != "2026-01-01T00:01:00+00:00":
        pytest.fail(
            "Expected bot.state.last_processed_candle =="
            " '2026-01-01T00:01:00+00:00'"
        )

    resumed = cli.ReferenceAlignedDemoBot(
        bot.config, bot.client, cli.DemoState.load_or_create(True)
    )
    if (resumed.reconcile_once(NOW)) is not (None):
        pytest.fail("Expected resumed.reconcile_once(NOW) is None")
    bot.client.market_order.assert_not_called()
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:02:00Z")}])
    monkeypatch.setattr(
        cli, "fetch_completed_linear_klines", lambda *_: candles
    )
    if f"opened {side}" not in (
        resumed.reconcile_once(datetime(2026, 1, 1, 0, 3, 5, tzinfo=UTC))
    ):
        pytest.fail(
            "Expected f'opened {side}' in resumed.reconcile_once(datetime(2026,"
            " 1, 1, 0, 3, 5, tzinfo=UTC))"
        )
    bot.client.market_order.assert_called_once()


def test_after_exchange_exit_rechecks_signal_instead_of_forcing_reentry(
    bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify after exchange exit rechecks signal instead of forcing reentry."""
    bot.state.observed_position_side = "Buy"
    bot.reconcile_once(NOW)
    monkeypatch.setattr(cli, "reference_decision", lambda *_: Mock(side=None))
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:02:00Z")}])
    monkeypatch.setattr(
        cli, "fetch_completed_linear_klines", lambda *_: candles
    )
    if (bot.reconcile_once(datetime(2026, 1, 1, 0, 3, 5, tzinfo=UTC))) is not (
        None
    ):
        pytest.fail(
            "Expected bot.reconcile_once(datetime(2026, 1, 1, 0, 3, 5,"
            " tzinfo=UTC)) is None"
        )
    bot.client.market_order.assert_not_called()


def test_strategy_reversal_survives_restart_without_extra_candle_delay(
    bot,
) -> None:
    """Verify strategy reversal survives restart without extra candle delay."""
    bot.client.position.return_value = {
        "side": "Sell",
        "avgPrice": "100",
        "size": "1",
    }
    bot.reconcile_once(NOW)
    resumed = cli.ReferenceAlignedDemoBot(
        bot.config, bot.client, cli.DemoState.load_or_create(True)
    )
    if not (resumed.state.pending_strategy_close):
        pytest.fail("Expected resumed.state.pending_strategy_close")
    bot.client.position.return_value = None
    if "opened Buy" not in (resumed.reconcile_once(NOW)):
        pytest.fail("Expected 'opened Buy' in resumed.reconcile_once(NOW)")
    if resumed.state.pending_strategy_close:
        pytest.fail("Expected not resumed.state.pending_strategy_close")


@pytest.mark.parametrize(
    "start", ["2026-01-01T00:01:00+00:00", "2026-01-01T00:01:55+00:00"]
)
def test_fresh_start_waits_until_completed_candle_after_launch(
    bot, monkeypatch: pytest.MonkeyPatch, start
) -> None:
    """Verify fresh start waits until completed candle after launch."""
    bot.state.launched_at = start
    calculate = Mock(return_value=Mock(side="Long"))
    monkeypatch.setattr(cli, "reference_decision", calculate)
    if (bot.reconcile_once(NOW)) is not (None):
        pytest.fail("Expected bot.reconcile_once(NOW) is None")
    calculate.assert_not_called()
    bot.client.market_order.assert_not_called()
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:02:00Z")}])
    monkeypatch.setattr(
        cli, "fetch_completed_linear_klines", lambda *_: candles
    )
    if "opened Buy" not in (
        bot.reconcile_once(datetime(2026, 1, 1, 0, 3, 5, tzinfo=UTC))
    ):
        pytest.fail(
            "Expected 'opened Buy' in bot.reconcile_once(datetime(2026, 1, 1,"
            " 0, 3, 5, tzinfo=UTC))"
        )
