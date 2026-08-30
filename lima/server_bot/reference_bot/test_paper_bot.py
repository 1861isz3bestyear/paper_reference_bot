import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests
import pytest

from reference_bot.cli import (
    BotState,
    PaperBot,
    StrategyDecision,
    calculate_strategy_decision,
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


def config() -> PaperBotConfig:
    return PaperBotConfig("VWAP band mean reversion", "5m", True, 1.2, 0.4, 1_000, 2.5, False, 2)


def candles() -> pd.DataFrame:
    times = pd.to_datetime(["2026-01-01T00:05:00Z", "2026-01-01T00:10:00Z"])
    return pd.DataFrame(
        {
            "time": times,
            "open": [100, 101],
            "high": [102, 103],
            "low": [99, 100],
            "close": [101, 102],
            "volume": [10, 10],
            "quote_asset_volume": [1_010, 1_020],
            "number_of_trades": [0, 0],
        }
    )


class TestPaperBot:
    def test_bot_state_create_and_reload(self, tmp_path):
        path = tmp_path / "state.json"
        created = BotState.load_or_create(path)
        loaded = BotState.load_or_create(path)
        assert loaded == created
        assert pd.Timestamp(created.launched_at).tzinfo is not None

    @pytest.mark.parametrize("changes, message", [
        ({"strategy_mode": "bad"}, "Unsupported"),
        ({"timeframe": "2m"}, "timeframe"),
        ({"open_order_vwap_sigma": 4}, "Open-order"),
        ({"close_order_vwap_sigma": -1}, "Close-order"),
        ({"initial_capital": 0}, "capital"),
        ({"minimum_order_size": 0}, "Minimum order size"),
        ({"stop_loss_pct": -1}, "Stop loss"),
        ({"vwap_anchor_reset_weeks": 0}, "reset"),
        ({"anchor_before_strategy_start": "yes"}, "true or false"),
        ({"anchor_before_days": 4000}, "between"),
        ({"anchor_before_strategy_start": True, "anchor_before_days": 0}, "at least 1"),
        ({"open_position_side": "invalid"}, "Long, Short, or Both"),
    ])
    def test_config_validation(self, changes, message):
        values = {**config().__dict__, **changes}
        with pytest.raises(ValueError, match=message):
            PaperBotConfig(**values).validate()

    def test_mexc_supports_vet_usdt(self):
        settings = PaperBotConfig(**{**config().__dict__, "data_source": "MEXC REST", "ticker": "VET_USDT"})

        settings.validate()

    def test_calculate_target_side_uses_offset_and_strategy_launch(self, monkeypatch):
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
        monkeypatch.setattr("reference_bot.cli.add_launch_weekly_anchored_vwap", indicator)
        monkeypatch.setattr("reference_bot.cli.backtest", backtest)
        monkeypatch.setattr("reference_bot.cli.latest_strategy_side", latest)
        settings = PaperBotConfig(**{
            **config().__dict__, "anchor_before_strategy_start": True, "anchor_before_days": 14,
        })
        launch = pd.Timestamp("2026-01-15T00:00:00Z")
        assert calculate_target_side(candles(), settings, launch) == "Long"
        assert indicator.call_args.args[1] == pd.Timestamp("2026-01-01T00:00:00Z")
        assert backtest.call_args.kwargs["strategy_start_at"] == launch
        assert backtest.call_args.kwargs["open_position_side"] == "Both"
        assert backtest.call_args.kwargs["minimum_order_size"] == 0.01
        assert calculate_target_side(candles().iloc[:1], settings, launch) is None

    def test_stats_use_terminal_friendly_multiline_layout(self) -> None:
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
            patch("reference_bot.cli.LocalPaperTrader.trade_history", return_value=[trade]),
            patch("reference_bot.cli._configured_market", return_value=("BTCUSDT", "BTC", "USDT")),
            patch("reference_bot.cli.shutil.get_terminal_size", return_value=__import__("os").terminal_size((60, 24))),
            redirect_stdout(output),
        ):
            stats_command()

        rendered = output.getvalue()
        assert "Completed trades: 1" in rendered
        assert "Trade 1: Long" in rendered
        assert "Deposit size: 125.00 USDT" in rendered
        assert "Exit reason:" in rendered
        assert all(len(line) <= 60 for line in rendered.splitlines())

    def test_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config().save(path)

            loaded = PaperBotConfig.load(path)

        assert loaded == config()
        assert set(loaded.__dict__) == {
            "strategy_mode", "timeframe", "trend", "open_order_vwap_sigma", "close_order_vwap_sigma",
            "initial_capital", "stop_loss_pct", "allow_immediate_reentry", "vwap_anchor_reset_weeks",
            "anchor_before_strategy_start", "anchor_before_days",
            "data_source", "ticker", "reverse_ticker",
            "open_position_side",
            "minimum_order_size",
            "open_sigma_1", "close_sigma_1", "open_sigma_2", "close_sigma_2",
        }

    def test_loads_older_config_without_anchor_offset_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            values = config().__dict__.copy()
            values.pop("anchor_before_strategy_start")
            values.pop("anchor_before_days")
            path.write_text(__import__("json").dumps(values), encoding="utf-8")

            loaded = PaperBotConfig.load(path)

        assert not loaded.anchor_before_strategy_start
        assert loaded.anchor_before_days == 0

    def test_loads_older_config_without_minimum_order_size(self, tmp_path) -> None:
        path = tmp_path / "config.json"
        values = config().__dict__.copy()
        values.pop("minimum_order_size")
        path.write_text(__import__("json").dumps(values), encoding="utf-8")

        loaded = PaperBotConfig.load(path)

        assert loaded.minimum_order_size == 0.01

    def make_bot(self, directory: str) -> PaperBot:
        root = Path(directory)
        config_path = root / "config.json"
        config().save(config_path)
        state_path = root / "state.json"
        BotState("2026-01-01T00:00:00+00:00").save(state_path)
        bot = PaperBot(config_path, state_path, root / "candles.sqlite")
        bot.trader = LocalPaperTrader(root / "account.json")
        return bot

    def test_retries_do_not_duplicate_processed_signal(self) -> None:
        from decimal import Decimal

        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            with (
                patch.object(bot, "_load_completed_candles", return_value=candles()),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    side_effect=[StrategyDecision(None, None), StrategyDecision("Long", 102.0)],
                ),
                patch("reference_bot.cli.fetch_live_price", return_value=102.0),
                patch("reference_bot.cli.load_bybit_credentials", return_value=("key", "secret")),
                patch("reference_bot.cli.fetch_bybit_taker_fee_rate", return_value=Decimal("0.00055")),
            ):
                assert bot.process_available_candles() == 2

            first_entry = bot.trader.current_position("BTCUSDT")
            bot.state.last_processed_candle = "2026-01-01T00:05:00+00:00"
            bot.state.save(bot.state_path)
            with (
                patch.object(bot, "_load_completed_candles", return_value=candles()),
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
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            one_candle = candles().iloc[:1]
            with (
                patch.object(bot, "_load_completed_candles", return_value=one_candle),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    return_value=StrategyDecision("Long", 101.0),
                ),
                patch("reference_bot.cli.fetch_live_price", side_effect=requests.ConnectionError("offline")),
            ):
                with pytest.raises(requests.ConnectionError):
                    bot.process_available_candles()

            persisted = BotState.load_or_create(bot.state_path)

        assert persisted.last_processed_candle is None

    def test_catch_up_replays_historical_fills_and_times_without_live_price(self) -> None:
        from decimal import Decimal

        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            backlog = pd.concat(
                [
                    candles(),
                    candles().assign(
                        time=pd.to_datetime(["2026-01-01T00:15:00Z", "2026-01-01T00:20:00Z"])
                    ),
                ],
                ignore_index=True,
            )
            decisions = [
                StrategyDecision(None, None),
                StrategyDecision("Long", 95.0),
                StrategyDecision(None, 105.0, "Reached -0.4σ exit band"),
                StrategyDecision(None, None),
            ]
            with (
                patch.object(bot, "_load_completed_candles", return_value=backlog),
                patch("reference_bot.cli.calculate_strategy_decision", side_effect=decisions),
                patch("reference_bot.cli.fetch_live_price") as live_price,
                patch("reference_bot.cli.load_bybit_credentials", return_value=("key", "secret")),
                patch(
                    "reference_bot.cli.fetch_bybit_taker_fee_rate",
                    return_value=Decimal("0.00055"),
                ),
            ):
                assert bot.process_available_candles() == 4

            history = bot.trader.trade_history("BTCUSDT")
            live_price.assert_not_called()
            assert history[0]["entry_price"] == 95.0
            assert history[0]["exit_price"] == 105.0
            assert history[0]["entry_time"] == "2026-01-01T00:10:00+00:00"
            assert history[0]["exit_time"] == "2026-01-01T00:15:00+00:00"
            assert history[0]["exit_reason"] == "Reached -0.4σ exit band"

            with (
                patch.object(bot, "_load_completed_candles", return_value=backlog),
                patch("reference_bot.cli.calculate_strategy_decision") as decision,
            ):
                assert bot.process_available_candles() == 0
                decision.assert_not_called()

            assert len(bot.trader.trade_history("BTCUSDT")) == 1

    def test_reversal_sizes_new_position_from_post_close_capital(self) -> None:
        from decimal import Decimal

        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            fee_rate = Decimal("0.001")
            opening = bot.trader.build_target("BTCUSDT", "Long", 100, bot.config.initial_capital)
            bot.trader.reconcile("BTCUSDT", opening, fee_rate)
            one_candle = candles().iloc[:1]
            with (
                patch.object(bot, "_load_completed_candles", return_value=one_candle),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    return_value=StrategyDecision("Short", 101.0),
                ),
                patch("reference_bot.cli.fetch_live_price", return_value=110.0),
                patch("reference_bot.cli.load_bybit_credentials", return_value=("key", "secret")),
                patch("reference_bot.cli.fetch_bybit_taker_fee_rate", return_value=fee_rate),
            ):
                bot.process_available_candles()

            position = bot.trader.current_position("BTCUSDT")

        assert position.side == "Short"
        assert position.quantity == Decimal("9.980")

    def test_anchor_offset_loads_historical_candles_but_does_not_process_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            bot.config = PaperBotConfig(
                **{
                    **bot.config.__dict__,
                    "anchor_before_strategy_start": True,
                    "anchor_before_days": 14,
                }
            )
            times = pd.date_range("2025-12-18T00:00:00Z", "2026-01-01T00:05:00Z", freq="5min")
            history = pd.DataFrame(
                {
                    "time": times,
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 10,
                    "quote_asset_volume": 1_000,
                    "number_of_trades": 0,
                }
            )
            with (
                patch("reference_bot.cli.fetch_completed_linear_klines", return_value=history) as fetch,
            ):
                loaded = bot._load_completed_candles(
                    pd.Timestamp("2025-12-18T00:00:00Z"),
                    datetime(2026, 1, 1, 0, 11, tzinfo=timezone.utc),
                )

        assert fetch.call_args.args[2] == int(pd.Timestamp("2025-12-18T00:00:00Z").timestamp() * 1000)
        assert len(loaded) == len(history)

    def test_stop_loss_uses_actual_local_fill_and_is_recorded(self) -> None:
        from decimal import Decimal

        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            opening = bot.trader.build_target("BTCUSDT", "Long", 102, bot.config.initial_capital)
            bot.trader.reconcile("BTCUSDT", opening, Decimal("0.00055"))
            stopped_candle = candles().iloc[:1].copy()
            stopped_candle.loc[stopped_candle.index[0], "time"] = pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=1)
            stopped_candle.loc[stopped_candle.index[0], "low"] = 90
            with (
                patch.object(bot, "_load_completed_candles", return_value=stopped_candle),
                patch(
                    "reference_bot.cli.calculate_strategy_decision",
                    return_value=StrategyDecision("Long", 101.0),
                ),
                patch("reference_bot.cli.fetch_live_price", return_value=90.0),
                patch("reference_bot.cli.load_bybit_credentials", return_value=("key", "secret")),
                patch("reference_bot.cli.fetch_bybit_taker_fee_rate", return_value=Decimal("0.00055")),
            ):
                bot.process_available_candles()

            history = bot.trader.trade_history("BTCUSDT")

        assert history[0]["exit_reason"] == "Stop loss"

    def test_empty_and_prelaunch_candles_are_not_processed(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            with patch.object(bot, "_load_completed_candles", return_value=pd.DataFrame()):
                assert bot.process_available_candles() == 0
            old = candles().copy()
            old["time"] = pd.to_datetime(["2025-12-31T23:50:00Z", "2025-12-31T23:55:00Z"])
            with patch.object(bot, "_load_completed_candles", return_value=old):
                assert bot.process_available_candles() == 0

    def test_run_forever_starts_and_stops_websocket(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            bot.market_feed = Mock()
            bot.process_available_candles = Mock(side_effect=lambda: bot.stop())
            output = StringIO()
            with patch("reference_bot.cli.signal.signal"), redirect_stdout(output):
                bot.run_forever()
            bot.market_feed.start.assert_called_once()
            bot.market_feed.stop.assert_called_once()
            assert "initial capital=1000.00 USDT" in output.getvalue()


def test_status_stats_and_pid_helpers(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state.json"
    BotState("2026-01-01T00:00:00+00:00").save(state)
    monkeypatch.setattr("reference_bot.cli.STATE_FILE", state)
    monkeypatch.setattr("reference_bot.cli.read_pid", lambda: None)
    monkeypatch.setattr("reference_bot.cli.LocalPaperTrader.current_position", Mock(return_value=Mock(side=None, quantity=0, entry_price=None)))
    monkeypatch.setattr("reference_bot.cli._configured_market", lambda *_: ("BTCUSDT", "BTC", "USDT"))
    status_command()
    status_output = capsys.readouterr().out
    assert "Paper bot: stopped" in status_output
    assert "Deposit size: 0.00 USDT" in status_output

    monkeypatch.setattr("reference_bot.cli.LocalPaperTrader.trade_history", Mock(return_value=[]))
    stats_command()
    assert "No completed" in capsys.readouterr().out

    pid_file = tmp_path / "pid"
    monkeypatch.setattr("reference_bot.cli.PID_FILE", pid_file)
    assert read_pid() is None
    pid_file.write_text("invalid")
    assert read_pid() is None
    assert process_is_running(999_999_999) is False
    stop_command()
    assert "not running" in capsys.readouterr().out


def test_reset_command_deletes_previous_data_but_keeps_config(monkeypatch, tmp_path, capsys):
    account = tmp_path / "account.json"
    state = tmp_path / "state.json"
    candles = tmp_path / "candles.sqlite"
    pid = tmp_path / "bot.pid"
    instance_lock = tmp_path / "bot.instance.lock"
    config_path = tmp_path / "config.json"
    for path in (account, account.with_suffix(".json.lock"), state, candles, Path(f"{candles}-wal"), pid, config_path):
        path.write_text("data", encoding="utf-8")
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


def test_reset_command_refuses_while_bot_is_running(monkeypatch):
    monkeypatch.setattr("reference_bot.cli.read_pid", lambda: 123)
    monkeypatch.setattr("reference_bot.cli.process_is_running", lambda _: True)

    with pytest.raises(RuntimeError, match="Stop the paper bot"):
        reset_command()
