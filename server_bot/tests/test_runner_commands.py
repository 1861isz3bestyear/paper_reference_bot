# Copyright 2026 anchored-vwam-backtest contributors
"""Exercise paper command dispatch and process supervision without live bots."""

from __future__ import annotations

import json
import signal
from contextlib import nullcontext
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest

from live_paper_bot import cli as live_module
from reference_bot import cli as reference_module
from reference_bot.config import PaperBotConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(params=[False, True])
def live(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> bool:
    """Select a deployed bot and isolate its runtime artifacts.

    Returns:
        Whether the live-paper implementation is selected.

    Raises:
        TypeError: If a fixture parameter is not a boolean.

    """
    value = cast("object", request.param)
    if not isinstance(value, bool):
        message = "Expected a boolean implementation selector."
        raise TypeError(message)
    module = live_module if value else reference_module
    for name in (
        "STATE_FILE",
        "PID_FILE",
        "LOG_FILE",
        "CANDLE_CACHE_FILE",
        "INSTANCE_LOCK_FILE",
        "PAPER_ACCOUNT_FILE",
    ):
        monkeypatch.setattr(module, name, tmp_path / name.lower())
    monkeypatch.setattr(module, "archive_while_running", nullcontext)
    return value


def config(path: Path) -> Path:
    """Write an ordinary configuration accepted by each deployed runner.

    Returns:
        The configured test value.

    """
    PaperBotConfig(
        strategy_mode="VWAP band mean reversion",
        timeframe="1m",
        trend=True,
        open_order_vwap_sigma=1,
        close_order_vwap_sigma=2,
        initial_capital=1000,
        stop_loss_pct=1,
        allow_immediate_reentry=True,
        vwap_anchor_reset_weeks=1,
    ).save(path)
    return path


@pytest.mark.parametrize(
    "command", ["run", "start", "stop", "reset", "status", "stats"]
)
def test_cli_dispatch(
    *, live: bool, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch each public command without spawning or signaling a process."""
    runner = live_module if live else reference_module
    monkeypatch.setattr("sys.argv", ["paper", command])
    action = Mock()
    monkeypatch.setattr(runner, command + "_command", action)
    runner.main()
    action.assert_called_once()


def test_cli_error_status(
    *, live: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return nonzero when a command fails validation."""
    runner = live_module if live else reference_module
    monkeypatch.setattr("sys.argv", ["paper", "stop"])
    monkeypatch.setattr(
        runner, "stop_command", Mock(side_effect=RuntimeError("failed"))
    )
    with pytest.raises(SystemExit) as error:
        runner.main()
    if error.value.code != 1:
        pytest.fail("Expected error.value.code == 1")


def test_start_success_and_failures(
    *, live: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detect startup and report duplicate starts or child failures."""
    runner = live_module if live else reference_module
    path = config(path=tmp_path / "config.json")
    child = Mock()
    monkeypatch.setattr("os.posix_spawn", child)
    monkeypatch.setattr(runner, "time", SimpleNamespace(sleep=Mock()))
    monkeypatch.setattr(runner, "read_pid", Mock(side_effect=[None, 123]))
    monkeypatch.setattr(runner, "process_is_running", Mock(return_value=True))
    runner.start_command(path)
    child.assert_called_once()
    monkeypatch.setattr(runner, "read_pid", Mock(return_value=123))
    with pytest.raises(RuntimeError, match=r"already running"):
        runner.start_command(path)
    monkeypatch.setattr(runner, "read_pid", Mock(return_value=None))
    with pytest.raises(RuntimeError, match=r"did not start"):
        runner.start_command(path, resume=True)


def test_run_cleanup_and_exclusive_lock(
    *, live: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove the PID file and refuse overlapping foreground instances."""
    runner = live_module if live else reference_module
    run_forever = Mock()
    bot = SimpleNamespace(run_forever=run_forever)
    monkeypatch.setattr(runner, "PaperBot", Mock(return_value=bot))
    _ = runner.STATE_FILE.write_text("old")
    runner.run_command(tmp_path / "config.json")
    run_forever.assert_called_once()
    if runner.PID_FILE.exists():
        pytest.fail("Expected not runner.PID_FILE.exists()")
    if runner.STATE_FILE.exists():
        pytest.fail("Expected not runner.STATE_FILE.exists()")
    monkeypatch.setattr("fcntl.flock", Mock(side_effect=BlockingIOError))
    with pytest.raises(RuntimeError, match=r"already running"):
        runner.run_command(tmp_path / "config.json", resume=True)
    with pytest.raises(RuntimeError, match=r"before resetting"):
        runner.reset_command()


def test_stop_and_reset_safety(
    *, live: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop only the recorded process and reject reset while it is alive."""
    runner = live_module if live else reference_module
    monkeypatch.setattr(runner, "read_pid", Mock(return_value=123))
    monkeypatch.setattr(runner, "process_is_running", Mock(return_value=True))
    kill = Mock()
    monkeypatch.setattr("os.kill", kill)
    runner.stop_command()
    kill.assert_called_once_with(123, signal.SIGTERM)
    with pytest.raises(RuntimeError, match=r"before resetting"):
        runner.reset_command()
    monkeypatch.setattr(runner, "read_pid", Mock(return_value=None))
    runner.stop_command()
    runner.reset_command()


def test_process_probe_permission_failure(
    *, live: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handle nonexistent or inaccessible process IDs without crashing."""
    runner = live_module if live else reference_module
    monkeypatch.setattr("os.kill", Mock(side_effect=PermissionError))
    if runner.process_is_running(123):
        pytest.fail("Expected not runner.process_is_running(123)")


@pytest.mark.parametrize("ledger", ["hedge_state", "margin_state"])
def test_ledger_stats(
    *,
    live: bool,
    tmp_path: Path,
    ledger: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read aggregate history from the selected accounting engine."""
    runner = live_module if live else reference_module
    path = config(path=tmp_path / "config.json")
    trade = {
        "pnl": 5,
        "side": "Long",
        "leverage": 2,
        "margin": 500,
        "entry_notional": 1000,
        "funding_paid": 1,
        "liquidation_adjustment": 0,
    }
    _ = runner.STATE_FILE.write_text(json.dumps({ledger: {"history": [trade]}}))
    runner.stats_command(path)
    output = capsys.readouterr().out
    if "Completed trades: 1" not in output:
        pytest.fail("Expected 'Completed trades: 1' in output")
    if "Leverage: 2x" not in output:
        pytest.fail("Expected 'Leverage: 2x' in output")


def test_hedge_status(
    *, live: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose hedge cash, positions and pending entries in status output."""
    runner = live_module if live else reference_module
    path = config(path=tmp_path / "config.json")
    _ = runner.STATE_FILE.write_text(
        json.dumps(
            {"hedge_state": {"cash": 1000, "positions": {}, "pending": {}}}
        )
    )
    runner.status_command(path)
    if "Hedge cash:" not in capsys.readouterr().out:
        pytest.fail("Expected 'Hedge cash:' in capsys.readouterr().out")


def test_run_loop_retry_and_feed_shutdown(
    *, live: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry a transient processing error and always stop the market feed."""
    runner = live_module if live else reference_module
    path = config(path=tmp_path / "config.json")
    monkeypatch.setattr(runner, "LocalPaperTrader", Mock())
    bot = runner.PaperBot(
        path, tmp_path / "state.json", tmp_path / "cache.sqlite"
    )
    start = Mock()
    stop = Mock()
    feed = SimpleNamespace(start=start, stop=stop)
    monkeypatch.setattr(bot, "market_feed", feed)
    monkeypatch.setattr(
        bot,
        "process_available_candles",
        Mock(side_effect=RuntimeError("temporary")),
    )
    monkeypatch.setattr(bot, "_wait", Mock(side_effect=bot.stop))
    bot.run_forever()
    start.assert_called_once()
    stop.assert_called_once()
