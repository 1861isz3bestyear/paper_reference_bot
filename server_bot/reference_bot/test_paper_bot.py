# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for reference bot test paper bot."""

import json
import os
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest
import requests

from reference_bot.cli import (
    BotState,
    PaperBot,
    StrategyDecision,
    calculate_target_side,
    process_is_running,
    read_pid,
    reset_command,
    stats_command,
    status_command,
    stop_command,
)
from reference_bot.config import PaperBotConfig
from reference_bot.trading import LocalPaperTrader
from shared.models import Trade
from shared.testing_support import ignoring_arguments
from tests.mock_support import call_args, call_kwargs, mock_child

TERMINAL_WIDTH = 60
INITIAL_CANDLE_COUNT = 2
HISTORY_FETCH_COUNT = 4


def config() -> PaperBotConfig:
    """Config.

    Returns:
        The config result.

    """
    return PaperBotConfig(
        strategy_mode="VWAP band mean reversion",
        timeframe="5m",
        trend=True,
        open_order_vwap_sigma=1.2,
        close_order_vwap_sigma=0.4,
        initial_capital=1_000,
        stop_loss_pct=2.5,
        allow_immediate_reentry=False,
        vwap_anchor_reset_weeks=2,
    )


def candles() -> pd.DataFrame:
    """Candles.

    Returns:
        The validated candle observations.

    """
    times = pd.to_datetime(["2026-01-01T00:05:00Z", "2026-01-01T00:10:00Z"])
    return pd.DataFrame({
        "time": times,
        "open": [100, 101],
        "high": [102, 103],
        "low": [99, 100],
        "close": [101, 102],
        "volume": [10, 10],
        "quote_asset_volume": [1_010, 1_020],
        "number_of_trades": [0, 0],
    })


class TestPaperBot:
    """Represent test paper bot."""

    @staticmethod
    def test_bot_state_create_and_reload(tmp_path: Path) -> None:
        """Verify bot state create and reload."""
        path = tmp_path / "state.json"
        created = BotState.load_or_create(path)
        loaded = BotState.load_or_create(path)
        assert loaded == created
        assert pd.Timestamp(created.launched_at).tzinfo is not None

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            ({"strategy_mode": "bad"}, "Unsupported"),
            ({"timeframe": "2m"}, "timeframe"),
            ({"open_order_vwap_sigma": 4}, "Open-order"),
            ({"close_order_vwap_sigma": -1}, "Close-order"),
            ({"initial_capital": 0}, "capital"),
            ({"minimum_order_size": 0}, "Minimum order size"),
            ({"stop_loss_pct": -1}, "Stop loss"),
            ({"take_profit_pct": -1}, "Take profit"),
            ({"take_profit_pct": float("nan")}, "Take profit"),
            ({"take_profit_pct": float("inf")}, "Take profit"),
            ({"vwap_anchor_reset_weeks": 0}, "reset"),
            ({"anchor_before_strategy_start": "yes"}, "true or false"),
            ({"anchor_before_days": 4000}, "between"),
            (
                {"anchor_before_strategy_start": True, "anchor_before_days": 0},
                "at least 1",
            ),
            ({"open_position_side": "invalid"}, "Long, Short, or Both"),
        ],
    )
    @staticmethod
    def test_config_validation(
        changes: dict[str, object], message: str
    ) -> None:
        """Verify config validation."""
        values = changes
        with pytest.raises(ValueError, match=message):
            replace(config(), **values).validate()

    @staticmethod
    def test_mexc_supports_vet_usdt() -> None:
        """Verify mexc supports vet usdt."""
        settings = replace(config(), data_source="MEXC REST", ticker="VET_USDT")

        settings.validate()

    @staticmethod
    def test_calculate_target_side_uses_offset_and_strategy_launch(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify calculate target side uses offset and strategy launch."""
        indicator = Mock(return_value=candles())
        trade = Trade(
            1_000,
            "Long",
            pd.Timestamp("2026-01-01T00:10:00Z"),
            pd.Timestamp("2026-01-01T00:10:00Z"),
            101,
            102,
            9.9,
            0,
            0,
            "End of strategy",
        )
        backtest = Mock(return_value=(pd.DataFrame(), [trade]))
        latest = Mock(return_value="Long")
        monkeypatch.setattr(
            "reference_bot.cli.add_launch_weekly_anchored_vwap", indicator
        )
        monkeypatch.setattr("reference_bot.cli.backtest", backtest)
        monkeypatch.setattr("reference_bot.cli.latest_strategy_side", latest)
        settings = replace(
            config(),
            anchor_before_strategy_start=True,
            anchor_before_days=14,
            take_profit_pct=1.5,
        )
        launch = pd.Timestamp("2026-01-15T00:00:00Z")
        assert calculate_target_side(candles(), settings, launch) == "Long"
        assert call_args(indicator)[1] == pd.Timestamp("2026-01-01T00:00:00Z")
        assert call_kwargs(backtest)["strategy_start_at"] == launch
        assert call_kwargs(backtest)["open_position_side"] == "Both"
        assert call_kwargs(backtest)["minimum_order_size"] == pytest.approx(
            0.01
        )
        assert call_kwargs(backtest)["take_profit_pct"] == pytest.approx(1.5)
        assert (
            calculate_target_side(candles().iloc[:1], settings, launch) is None
        )

    @staticmethod
    def test_stats_use_terminal_friendly_multiline_layout() -> None:
        """Verify stats use terminal friendly multiline layout."""
        trade = {
            "side": "Long",
            "entry_time": "2026-01-01T00:00:00+00:00",
            "exit_time": "2026-01-01T01:00:00+00:00",
            "entry_price": 100,
            "exit_price": 102,
            "quantity": "1.25",
            "deposit_size": "125.00",
            "entry_fee": 0.05,
            "exit_fee": 0.051,
            "pnl": 2.4,
            "return_pct": 1.92,
            "exit_reason": "Reached the configured close-order VWAP band",
        }
        output = StringIO()
        with (
            patch(
                "reference_bot.cli.LocalPaperTrader.trade_history",
                return_value=[trade],
            ),
            patch(
                "reference_bot.cli._configured_market",
                return_value=("BTCUSDT", "BTC", "USDT"),
            ),
            patch(
                "reference_bot.cli.shutil.get_terminal_size",
                return_value=os.terminal_size((60, 24)),
            ),
            redirect_stdout(output),
        ):
            stats_command()

        rendered = output.getvalue()
        assert "Completed trades: 1" in rendered
        assert "Trade 1: Long" in rendered
        assert "Deposit size: 125.00 USDT" in rendered
        assert "Exit reason:" in rendered
        assert all(
            len(line) <= TERMINAL_WIDTH for line in rendered.splitlines()
        )

    @pytest.mark.parametrize("take_profit_pct", [0.0, 1.5])
    @staticmethod
    def test_config_round_trip(take_profit_pct: float) -> None:
        """Verify config round trip."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            settings = replace(config(), take_profit_pct=take_profit_pct)
            settings.save(path)

            loaded = PaperBotConfig.load(path)

        assert loaded == settings
        assert set(loaded.__dict__) == {
            "strategy_mode",
            "timeframe",
            "trend",
            "open_order_vwap_sigma",
            "close_order_vwap_sigma",
            "initial_capital",
            "stop_loss_pct",
            "allow_immediate_reentry",
            "vwap_anchor_reset_weeks",
            "anchor_before_strategy_start",
            "anchor_before_days",
            "data_source",
            "ticker",
            "reverse_ticker",
            "open_position_side",
            "minimum_order_size",
            "stop_loss_basis",
            "hedge_entry_timeout_bars",
            "leverage",
            "slippage_bps",
            "take_profit_pct",
            "open_sigma_1",
            "close_sigma_1",
            "open_sigma_2",
            "close_sigma_2",
        }

    @staticmethod
    def test_loads_older_config_without_anchor_offset_fields() -> None:
        """Verify loads older config without anchor offset fields."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            values = config().__dict__.copy()
            values.pop("anchor_before_strategy_start")
            values.pop("anchor_before_days")
            _ = path.write_text(json.dumps(values), encoding="utf-8")

            loaded = PaperBotConfig.load(path)

        assert not loaded.anchor_before_strategy_start
        assert loaded.anchor_before_days == 0

    @staticmethod
    def test_loads_older_config_without_minimum_order_size(
        tmp_path: Path,
    ) -> None:
        """Verify loads older config without minimum order size."""
        path = tmp_path / "config.json"
        values = config().__dict__.copy()
        values.pop("minimum_order_size")
        _ = path.write_text(json.dumps(values), encoding="utf-8")

        loaded = PaperBotConfig.load(path)

        assert loaded.minimum_order_size == pytest.approx(0.01)

    @staticmethod
    def make_bot(directory: str) -> PaperBot:
        """Make bot.

        Returns:
            The make bot result.

        """
        root = Path(directory)
        config_path = root / "config.json"
        config().save(config_path)
        state_path = root / "state.json"
        BotState("2026-01-01T00:00:00+00:00").save(state_path)
        bot = PaperBot(config_path, state_path, root / "candles.sqlite")
        bot.trader = LocalPaperTrader(root / "account.json")
        return bot

    def test_retries_do_not_duplicate_processed_signal(self) -> None:
        """Verify retries do not duplicate processed signal."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            with (
                patch.object(
                    bot, "load_completed_candles", return_value=candles()
                ),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    side_effect=[
                        StrategyDecision(None, None),
                        StrategyDecision("Long", 102.0),
                    ],
                ),
                patch("reference_bot.cli.fetch_live_price", return_value=102.0),
                patch(
                    "reference_bot.cli.load_bybit_credentials",
                    return_value=("key", "secret"),
                ),
                patch(
                    "reference_bot.cli.fetch_bybit_taker_fee_rate",
                    return_value=Decimal("0.00055"),
                ),
            ):
                assert bot.process_available_candles() == INITIAL_CANDLE_COUNT

            first_entry = bot.trader.current_position("BTCUSDT")
            bot.state.last_processed_candle = "2026-01-01T00:05:00+00:00"
            bot.state.save(bot.state_path)
            with (
                patch.object(
                    bot, "load_completed_candles", return_value=candles()
                ),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    return_value=StrategyDecision("Long", None),
                ),
            ):
                assert bot.process_available_candles() == 1

            repeated = bot.trader.current_position("BTCUSDT")

        assert repeated.updated_at == first_entry.updated_at
        assert repeated.quantity == first_entry.quantity

    def test_network_failure_does_not_advance_candle_cursor(self) -> None:
        """Verify network failure does not advance candle cursor."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            one_candle = candles().iloc[:1]
            with (
                patch.object(
                    bot, "load_completed_candles", return_value=one_candle
                ),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    return_value=StrategyDecision("Long", 101.0),
                ),
                patch(
                    "reference_bot.cli.fetch_live_price",
                    side_effect=requests.ConnectionError("offline"),
                ),
                pytest.raises(requests.ConnectionError),
            ):
                _ = bot.process_available_candles()

            persisted = BotState.load_or_create(bot.state_path)

        assert persisted.last_processed_candle is None

    def test_catch_up_replays_historical_fills_and_times_without_live_price(
        self,
    ) -> None:
        """Replay historical fills using candle prices and timestamps."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            backlog = pd.concat(
                [
                    candles(),
                    candles().assign(
                        time=pd.to_datetime(
                            ["2026-01-01T00:15:00Z", "2026-01-01T00:20:00Z"]
                        )
                    ),
                ],
                ignore_index=True,
            )
            decisions = [
                StrategyDecision(None, None),
                StrategyDecision("Long", 95.0),
                StrategyDecision(None, 105.0, "Reached -0.4\u03c3 exit band"),
                StrategyDecision(None, None),
            ]
            with (
                patch.object(
                    bot, "load_completed_candles", return_value=backlog
                ),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    side_effect=decisions,
                ),
                patch("reference_bot.cli.fetch_live_price") as live_price,
                patch(
                    "reference_bot.cli.load_bybit_credentials",
                    return_value=("key", "secret"),
                ),
                patch(
                    "reference_bot.cli.fetch_bybit_taker_fee_rate",
                    return_value=Decimal("0.00055"),
                ),
            ):
                assert bot.process_available_candles() == HISTORY_FETCH_COUNT

            history = bot.trader.trade_history("BTCUSDT")
            live_price.assert_not_called()
            assert history[0]["entry_price"] == pytest.approx(95.0)
            assert history[0]["exit_price"] == pytest.approx(105.0)
            assert history[0]["entry_time"] == "2026-01-01T00:10:00+00:00"
            assert history[0]["exit_time"] == "2026-01-01T00:15:00+00:00"
            assert history[0]["exit_reason"] == "Reached -0.4\u03c3 exit band"
            with (
                patch.object(
                    bot, "load_completed_candles", return_value=backlog
                ),
                patch(
                    "reference_bot.cli.calculate_strategy_decision"
                ) as decision,
            ):
                assert bot.process_available_candles() == 0
                decision.assert_not_called()

            assert len(bot.trader.trade_history("BTCUSDT")) == 1

    def test_reversal_sizes_new_position_from_post_close_capital(self) -> None:
        """Verify reversal sizes new position from post close capital."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            fee_rate = Decimal("0.001")
            opening = bot.trader.build_target(
                "BTCUSDT", "Long", 100, notional=bot.config.initial_capital
            )
            _ = bot.trader.reconcile(
                "BTCUSDT", opening, taker_fee_rate=fee_rate
            )
            one_candle = candles().iloc[:1]
            with (
                patch.object(
                    bot, "load_completed_candles", return_value=one_candle
                ),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    return_value=StrategyDecision("Short", 101.0),
                ),
                patch("reference_bot.cli.fetch_live_price", return_value=110.0),
                patch(
                    "reference_bot.cli.load_bybit_credentials",
                    return_value=("key", "secret"),
                ),
                patch(
                    "reference_bot.cli.fetch_bybit_taker_fee_rate",
                    return_value=fee_rate,
                ),
            ):
                _ = bot.process_available_candles()

            position = bot.trader.current_position("BTCUSDT")

        assert position.side == "Short"
        assert position.quantity == Decimal("9.980")

    def test_anchor_offset_loads_historical_candles_but_does_not_process_them(
        self,
    ) -> None:
        """Load anchor history without processing it as strategy candles."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            bot.config = replace(
                bot.config,
                anchor_before_strategy_start=True,
                anchor_before_days=14,
            )
            times = pd.date_range(
                "2025-12-18T00:00:00Z", "2026-01-01T00:05:00Z", freq="5min"
            )
            history = pd.DataFrame({
                "time": times,
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 10,
                "quote_asset_volume": 1_000,
                "number_of_trades": 0,
            })
            with (
                patch(
                    "reference_bot.cli.fetch_completed_linear_klines",
                    return_value=history,
                ) as fetch,
            ):
                loaded = bot.load_completed_candles(
                    pd.Timestamp("2025-12-18T00:00:00Z"),
                    datetime(2026, 1, 1, 0, 11, tzinfo=UTC),
                )

        assert call_kwargs(fetch)["start_ms"] == int(
            pd.Timestamp("2025-12-18T00:00:00Z").timestamp() * 1000
        )
        assert len(loaded) == len(history)

    def test_stop_loss_uses_actual_local_fill_and_is_recorded(self) -> None:
        """Verify stop loss uses actual local fill and is recorded."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            opening = bot.trader.build_target(
                "BTCUSDT", "Long", 102, notional=bot.config.initial_capital
            )
            _ = bot.trader.reconcile(
                "BTCUSDT", opening, taker_fee_rate=Decimal("0.00055")
            )
            stopped_candle = candles().iloc[:1].copy()
            stopped_candle.loc[stopped_candle.index[0], "time"] = (
                pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=1)
            )
            stopped_candle.loc[stopped_candle.index[0], "low"] = 90
            with (
                patch.object(
                    bot, "load_completed_candles", return_value=stopped_candle
                ),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    return_value=StrategyDecision("Long", 101.0),
                ),
                patch("reference_bot.cli.fetch_live_price", return_value=90.0),
                patch(
                    "reference_bot.cli.load_bybit_credentials",
                    return_value=("key", "secret"),
                ),
                patch(
                    "reference_bot.cli.fetch_bybit_taker_fee_rate",
                    return_value=Decimal("0.00055"),
                ),
            ):
                _ = bot.process_available_candles()

            history = bot.trader.trade_history("BTCUSDT")

        assert history[0]["exit_reason"] == "Stop loss"

    def test_empty_and_prelaunch_candles_are_not_processed(self) -> None:
        """Verify empty and prelaunch candles are not processed."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            with patch.object(
                bot, "load_completed_candles", return_value=pd.DataFrame()
            ):
                assert bot.process_available_candles() == 0
            old = candles().copy()
            old["time"] = pd.to_datetime(
                ["2025-12-31T23:50:00Z", "2025-12-31T23:55:00Z"]
            )
            with patch.object(bot, "load_completed_candles", return_value=old):
                assert bot.process_available_candles() == 0

    def test_run_forever_starts_and_stops_websocket(self) -> None:
        """Verify run forever starts and stops websocket."""
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            bot.market_feed = Mock()
            bot.process_available_candles = Mock(side_effect=bot.stop)
            output = StringIO()
            with (
                patch("reference_bot.cli.signal.signal"),
                redirect_stdout(output),
            ):
                bot.run_forever()
            mock_child(bot.market_feed, "start").assert_called_once()
            mock_child(bot.market_feed, "stop").assert_called_once()
            assert "initial capital=1000.00 USDT" in output.getvalue()


def test_status_stats_and_pid_helpers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify status stats and pid helpers."""
    state = tmp_path / "state.json"
    BotState("2026-01-01T00:00:00+00:00").save(state)
    monkeypatch.setattr("reference_bot.cli.STATE_FILE", state)
    monkeypatch.setattr("reference_bot.cli.read_pid", lambda: None)
    monkeypatch.setattr(
        "reference_bot.cli.LocalPaperTrader.current_position",
        Mock(return_value=Mock(side=None, quantity=0, entry_price=None)),
    )
    monkeypatch.setattr(
        "reference_bot.cli._configured_market",
        ignoring_arguments(lambda: ("BTCUSDT", "BTC", "USDT")),
    )
    status_command()
    status_output = capsys.readouterr().out
    assert "Paper bot: stopped" in status_output
    assert "Deposit size: 0.00 USDT" in status_output

    monkeypatch.setattr(
        "reference_bot.cli.LocalPaperTrader.trade_history",
        Mock(return_value=[]),
    )
    stats_command()
    assert "No completed" in capsys.readouterr().out

    pid_file = tmp_path / "pid"
    monkeypatch.setattr("reference_bot.cli.PID_FILE", pid_file)
    assert read_pid() is None
    _ = pid_file.write_text("invalid")
    assert read_pid() is None
    assert process_is_running(999999999) is False
    stop_command()
    assert "not running" in capsys.readouterr().out


def test_reset_command_deletes_previous_data_but_keeps_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify reset command deletes previous data but keeps config."""
    account = tmp_path / "account.json"
    state = tmp_path / "state.json"
    candles = tmp_path / "candles.sqlite"
    pid = tmp_path / "bot.pid"
    instance_lock = tmp_path / "bot.instance.lock"
    config_path = tmp_path / "config.json"
    for path in (
        account,
        account.with_suffix(".json.lock"),
        state,
        candles,
        Path(f"{candles}-wal"),
        pid,
        config_path,
    ):
        _ = path.write_text("data", encoding="utf-8")
    monkeypatch.setattr("reference_bot.cli.PAPER_ACCOUNT_FILE", account)
    monkeypatch.setattr("reference_bot.cli.STATE_FILE", state)
    monkeypatch.setattr("reference_bot.cli.CANDLE_CACHE_FILE", candles)
    monkeypatch.setattr("reference_bot.cli.PID_FILE", pid)
    monkeypatch.setattr("reference_bot.cli.INSTANCE_LOCK_FILE", instance_lock)
    monkeypatch.setattr("reference_bot.cli.read_pid", lambda: None)

    reset_command()

    assert config_path.is_file()
    assert not account.exists()
    assert not account.with_suffix(".json.lock").exists()
    assert not state.exists()
    assert not candles.exists()
    assert not Path(f"{candles}-wal").exists()
    assert not pid.exists()
    assert "Deleted previous paper bot data" in capsys.readouterr().out


def test_reset_command_refuses_while_bot_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify reset command refuses while bot is running."""
    monkeypatch.setattr("reference_bot.cli.read_pid", lambda: 123)
    monkeypatch.setattr(
        "reference_bot.cli.process_is_running", ignoring_arguments(lambda: True)
    )

    with pytest.raises(RuntimeError, match="Stop the paper bot"):
        reset_command()
