# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for bybit bot test bybit bot."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from bybit_bot import cli
from bybit_bot.cli import BybitBot, BybitState
from bybit_bot.client import BybitClient
from bybit_demo_bot.cli import BybitDemoBot


def test_mainnet_client_and_bot_identity() -> None:
    """Verify mainnet client and bot identity."""
    if BybitClient("key", "secret").base_url != "https://api.bybit.com":
        pytest.fail(
            "Expected BybitClient('key', 'secret').base_url =="
            " 'https://api.bybit.com'"
        )
    if not (issubclass(BybitBot, BybitDemoBot)):
        pytest.fail("Expected issubclass(BybitBot, BybitDemoBot)")
    if BybitBot.EXECUTOR_NAME != "Bybit MAINNET":
        pytest.fail("Expected BybitBot.EXECUTOR_NAME == 'Bybit MAINNET'")


def test_state_create_resume_and_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify state create resume and invalid."""
    path = tmp_path / "bybit_state.json"
    monkeypatch.setattr(cli, "STATE_FILE", path)
    state = BybitState.load_or_create(False)
    if not (path.is_file()):
        pytest.fail("Expected path.is_file()")
    if not (state.launched_at):
        pytest.fail("Expected state.launched_at")
    state.pending_protection_side = "Buy"
    state.save()
    if BybitState.load_or_create(True).pending_protection_side != "Buy":
        pytest.fail(
            "Expected BybitState.load_or_create(True).pending_protection_side"
            " == 'Buy'"
        )
    _ = path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cannot read"):
        _ = BybitState.load_or_create(True)


def test_run_requires_confirmation_and_positive_poll(tmp_path: Path) -> None:
    """Verify run requires confirmation and positive poll."""
    with pytest.raises(ValueError, match="--confirm-live"):
        cli.run_bybit_command(tmp_path / "config", tmp_path / "env")
    with pytest.raises(ValueError, match="positive"):
        cli.run_bybit_command(
            tmp_path / "config",
            tmp_path / "env",
            confirm_live=True,
            poll_seconds=0,
        )


def test_run_builds_mainnet_bot_with_isolated_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify run builds mainnet bot with isolated state."""
    config = Mock()
    monkeypatch.setattr(cli.PaperBotConfig, "load", Mock(return_value=config))
    monkeypatch.setattr(
        cli,
        "load_env",
        Mock(return_value={"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}),
    )
    client = Mock()
    monkeypatch.setattr(cli, "BybitClient", Mock(return_value=client))
    state = Mock()
    monkeypatch.setattr(
        cli.BybitState, "load_or_create", Mock(return_value=state)
    )
    bot = Mock()
    monkeypatch.setattr(cli, "BybitBot", Mock(return_value=bot))
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "mainnet.lock")
    cli.run_bybit_command(
        tmp_path / "config",
        tmp_path / "env",
        confirm_live=True,
        resume=True,
        poll_seconds=7,
    )
    cli.BybitClient.assert_called_once_with("k", "s")
    cli.BybitState.load_or_create.assert_called_once_with(True)
    cli.BybitBot.assert_called_once_with(config, client, state)
    bot.run.assert_called_once_with(7)


def test_run_rejects_duplicate_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify run rejects duplicate instance."""
    monkeypatch.setattr(cli.PaperBotConfig, "load", Mock(return_value=Mock()))
    monkeypatch.setattr(
        cli,
        "load_env",
        Mock(return_value={"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}),
    )
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "mainnet.lock")
    monkeypatch.setattr(cli.fcntl, "flock", Mock(side_effect=BlockingIOError))
    with pytest.raises(RuntimeError, match="Another Bybit mainnet"):
        cli.run_bybit_command(
            tmp_path / "config", tmp_path / "env", confirm_live=True
        )


def test_main_forwards_arguments_and_formats_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify main forwards arguments and formats errors."""
    args = argparse.Namespace(
        config="config",
        env="env",
        confirm_live=True,
        resume=True,
        poll_seconds=4,
    )
    monkeypatch.setattr(cli, "parse_args", lambda: args)
    called = Mock()
    monkeypatch.setattr(cli, "run_bybit_command", called)
    cli.main()
    called.assert_called_once_with(
        "config", "env", confirm_live=True, resume=True, poll_seconds=4
    )
    called.side_effect = ValueError("bad")
    with pytest.raises(SystemExit, match="Error: bad"):
        cli.main()


def test_parse_args_defaults_and_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify parse args defaults and flags."""
    monkeypatch.setattr(
        "sys.argv",
        ["bybit", "run", "--confirm-live", "--resume", "--poll-seconds", "3"],
    )
    args = cli.parse_args()
    if not (args.confirm_live):
        pytest.fail("Expected args.confirm_live")
    if not (args.resume):
        pytest.fail("Expected args.resume")
    if args.poll_seconds != 3:
        pytest.fail("Expected args.poll_seconds == 3")
    if args.config != cli.PAPER_BOT_CONFIG_FILE:
        pytest.fail("Expected args.config == cli.PAPER_BOT_CONFIG_FILE")
    if args.env != cli.ENV_FILE:
        pytest.fail("Expected args.env == cli.ENV_FILE")


def test_inherited_sizing_uses_ninety_percent_of_mainnet_balance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify inherited sizing uses ninety percent of mainnet balance."""
    monkeypatch.setattr(
        "bybit_demo_bot.cli.STATE_FILE", tmp_path / "unused.json"
    )
    client = Mock()
    client.instruments.return_value = {
        "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1"}
    }
    client.available_usdt.return_value = Decimal(15)
    config = Mock(
        data_source="Bybit REST",
        reverse_ticker=False,
        ticker="XRP_USDT",
        minimum_order_size=0.01,
        leverage=1,
    )
    bot = BybitBot(config, client, Mock())
    if bot._quantity(Decimal("1.38")) != Decimal("9.7"):
        pytest.fail("Expected bot._quantity(Decimal('1.38')) == Decimal('9.7')")
