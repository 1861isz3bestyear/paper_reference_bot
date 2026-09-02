from __future__ import annotations

import argparse
import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from bybit_bot import cli
from bybit_bot.cli import BybitBot, BybitState
from bybit_bot.client import BybitClient
from bybit_demo_bot.cli import BybitDemoBot


def test_mainnet_client_and_bot_identity():
    assert BybitClient("key", "secret").base_url == "https://api.bybit.com"
    assert issubclass(BybitBot, BybitDemoBot)
    assert BybitBot.EXECUTOR_NAME == "Bybit MAINNET"


def test_state_create_resume_and_invalid(monkeypatch, tmp_path):
    path = tmp_path / "bybit_state.json"
    monkeypatch.setattr(cli, "STATE_FILE", path)
    state = BybitState.load_or_create(False)
    assert path.is_file() and state.launched_at
    state.pending_protection_side = "Buy"
    state.save()
    assert BybitState.load_or_create(True).pending_protection_side == "Buy"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cannot read"):
        BybitState.load_or_create(True)


def test_run_requires_confirmation_and_positive_poll(tmp_path):
    with pytest.raises(ValueError, match="--confirm-live"):
        cli.run_bybit_command(tmp_path / "config", tmp_path / "env")
    with pytest.raises(ValueError, match="positive"):
        cli.run_bybit_command(tmp_path / "config", tmp_path / "env", confirm_live=True, poll_seconds=0)


def test_run_builds_mainnet_bot_with_isolated_state(monkeypatch, tmp_path):
    config = Mock()
    monkeypatch.setattr(cli.PaperBotConfig, "load", Mock(return_value=config))
    monkeypatch.setattr(cli, "load_env", Mock(return_value={"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}))
    client = Mock()
    monkeypatch.setattr(cli, "BybitClient", Mock(return_value=client))
    state = Mock()
    monkeypatch.setattr(cli.BybitState, "load_or_create", Mock(return_value=state))
    bot = Mock()
    monkeypatch.setattr(cli, "BybitBot", Mock(return_value=bot))
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "mainnet.lock")
    cli.run_bybit_command(tmp_path / "config", tmp_path / "env", confirm_live=True, resume=True, poll_seconds=7)
    cli.BybitClient.assert_called_once_with("k", "s")
    cli.BybitState.load_or_create.assert_called_once_with(True)
    cli.BybitBot.assert_called_once_with(config, client, state)
    bot.run.assert_called_once_with(7)


def test_run_rejects_duplicate_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.PaperBotConfig, "load", Mock(return_value=Mock()))
    monkeypatch.setattr(cli, "load_env", Mock(return_value={"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}))
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "mainnet.lock")
    monkeypatch.setattr(cli.fcntl, "flock", Mock(side_effect=BlockingIOError))
    with pytest.raises(RuntimeError, match="Another Bybit mainnet"):
        cli.run_bybit_command(tmp_path / "config", tmp_path / "env", confirm_live=True)


def test_main_forwards_arguments_and_formats_errors(monkeypatch):
    args = argparse.Namespace(
        config="config", env="env", confirm_live=True, resume=True, poll_seconds=4,
    )
    monkeypatch.setattr(cli, "parse_args", lambda: args)
    called = Mock()
    monkeypatch.setattr(cli, "run_bybit_command", called)
    cli.main()
    called.assert_called_once_with("config", "env", confirm_live=True, resume=True, poll_seconds=4)
    called.side_effect = ValueError("bad")
    with pytest.raises(SystemExit, match="Error: bad"):
        cli.main()


def test_parse_args_defaults_and_flags(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["bybit", "run", "--confirm-live", "--resume", "--poll-seconds", "3"])
    args = cli.parse_args()
    assert args.confirm_live and args.resume and args.poll_seconds == 3
    assert args.config == cli.PAPER_BOT_CONFIG_FILE and args.env == cli.ENV_FILE


def test_inherited_sizing_uses_ninety_percent_of_mainnet_balance(monkeypatch, tmp_path):
    monkeypatch.setattr("bybit_demo_bot.cli.STATE_FILE", tmp_path / "unused.json")
    client = Mock()
    client.instruments.return_value = {"lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1"}}
    client.available_usdt.return_value = Decimal("15")
    config = Mock(data_source="Bybit REST", reverse_ticker=False, ticker="XRP_USDT", minimum_order_size=.01)
    bot = BybitBot(config, client, Mock())
    assert bot._quantity(Decimal("1.38")) == Decimal("9.7")
