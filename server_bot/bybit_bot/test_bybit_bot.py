# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for bybit bot test bybit bot."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from bybit_bot import cli
from bybit_bot.cli import BybitBot, BybitState
from bybit_bot.client import BybitClient
from bybit_demo_bot.cli import BybitDemoBot
from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from tests.mock_support import mock_child

if TYPE_CHECKING:
    from pathlib import Path


POLL_SECONDS = 3


def test_mainnet_client_and_bot_identity() -> None:
    """Verify mainnet client and bot identity."""
    assert BybitClient("key", "secret").base_url == "https://api.bybit.com"
    assert issubclass(BybitBot, BybitDemoBot)
    assert BybitBot.EXECUTOR_NAME == "Bybit MAINNET"


def test_state_create_resume_and_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify state create resume and invalid."""
    path = tmp_path / "bybit_state.json"
    monkeypatch.setattr(cli, "STATE_FILE", path)
    state = BybitState.load_or_create(resume=False)
    assert path.is_file()
    assert state.launched_at
    state.pending_protection_side = "Buy"
    state.save()
    assert (
        BybitState.load_or_create(resume=True).pending_protection_side == "Buy"
    )
    _ = path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cannot read"):
        _ = BybitState.load_or_create(resume=True)


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
    monkeypatch.setattr(cli, "archive_while_running", nullcontext)
    config = Mock()
    monkeypatch.setattr(PaperBotConfig, "load", Mock(return_value=config))
    monkeypatch.setattr(
        cli,
        "load_env",
        Mock(return_value={"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}),
    )
    client = Mock()
    client_type = Mock(return_value=client)
    monkeypatch.setattr(cli, "BybitClient", client_type)
    state = Mock()
    load_state = Mock(return_value=state)
    monkeypatch.setattr(cli.BybitState, "load_or_create", load_state)
    bot = Mock()
    bot_type = Mock(return_value=bot)
    monkeypatch.setattr(cli, "BybitBot", bot_type)
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "mainnet.lock")
    cli.run_bybit_command(
        tmp_path / "config",
        tmp_path / "env",
        confirm_live=True,
        resume=True,
        poll_seconds=7,
    )
    client_type.assert_called_once_with("k", "s")
    load_state.assert_called_once_with(resume=True)
    bot_type.assert_called_once_with(config, client, state)
    mock_child(bot, "run").assert_called_once_with(7)


def test_run_rejects_duplicate_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify run rejects duplicate instance."""
    monkeypatch.setattr(PaperBotConfig, "load", Mock(return_value=Mock()))
    monkeypatch.setattr(
        cli,
        "load_env",
        Mock(return_value={"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}),
    )
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "mainnet.lock")
    monkeypatch.setattr("fcntl.flock", Mock(side_effect=BlockingIOError))
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


def test_parse_args_defaults_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify parse args defaults and flags."""
    monkeypatch.setattr(
        "sys.argv",
        ["bybit", "run", "--confirm-live", "--resume", "--poll-seconds", "3"],
    )
    args = cli.parse_args()
    assert args.confirm_live
    assert args.resume
    assert args.poll_seconds == POLL_SECONDS
    assert args.config == PAPER_BOT_CONFIG_FILE
    assert args.env == cli.ENV_FILE


def test_inherited_sizing_uses_ninety_percent_of_mainnet_balance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify inherited sizing uses ninety percent of mainnet balance."""
    monkeypatch.setattr(
        "bybit_demo_bot.cli.STATE_FILE", tmp_path / "unused.json"
    )
    client = Mock()
    mock_child(client, "instruments").return_value = {
        "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1"}
    }
    mock_child(client, "available_usdt").return_value = Decimal(15)
    config = Mock(
        data_source="Bybit REST",
        reverse_ticker=False,
        ticker="XRP_USDT",
        minimum_order_size=0.01,
        leverage=1,
    )
    bot = BybitBot(config, client, Mock())
    assert bot.order_quantity(Decimal("1.38")) == Decimal("9.7")
