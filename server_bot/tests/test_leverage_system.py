# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for tests test leverage system."""

import json
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from bybit_demo_bot.cli import BybitDemoBot
from live_paper_bot import cli, market, trading
from reference_bot.cli import BotState
from reference_bot.config import PaperBotConfig
from shared import leverage_data
from shared.margin_validation import parse_margin_snapshot
from shared.validation import json_object, object_mapping

if TYPE_CHECKING:
    from shared.margin_types import MarginOrder
    from tests.conftest import TradingSystem

INITIAL_CANDLE_COUNT = 2
MINIMUM_EXPECTED_QUANTITY = 90
INITIAL_CAPITAL = 1000
MINIMUM_ENTRY_FEE = 9
CONFIGURED_LEVERAGE = 10
MARGIN_TARGET_VERSION = 3


pytestmark = pytest.mark.integration


def test_persisted_leverage_config_runs_with_real_http_and_resumes(
    system: TradingSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify persisted leverage config runs with real http and resumes."""
    path = system.state_path.with_name("config.json")
    cfg = replace(PaperBotConfig.load(path), leverage=10, slippage_bps=2.0)
    cfg.save(path)
    monkeypatch.setattr(
        leverage_data, "BASE_URL", system.exchange.url + "/v5/market/"
    )
    bot = system.producer()
    assert bot.process_available_candles() == INITIAL_CANDLE_COUNT
    saved = json_object(system.state_path.read_text())
    position = parse_margin_snapshot(saved["margin_state"])["positions"]["Long"]
    assert position["quantity"] > MINIMUM_EXPECTED_QUANTITY
    assert position["margin"] < INITIAL_CAPITAL
    assert position["entry_fee"] > MINIMUM_ENTRY_FEE
    assert system.producer().process_available_candles() == 0
    assert (
        json_object(system.state_path.read_text())["margin_state"]
        == saved["margin_state"]
    )
    system.clock.value += timedelta(minutes=1)
    assert system.producer().process_available_candles() == 1
    ledger = parse_margin_snapshot(
        json_object(system.state_path.read_text())["margin_state"]
    )
    assert not ledger["positions"]
    assert ledger["history"][0]["leverage"] == CONFIGURED_LEVERAGE
    assert ledger["cash"] > INITIAL_CAPITAL
    system.producer().run_forever()
    assert (
        json_object(system.target.read_text())["version"]
        == MARGIN_TARGET_VERSION
    )
    assert not system.exchange.submissions
    assert {
        "/v5/market/mark-price-kline",
        "/v5/market/funding/history",
        "/v5/market/risk-limit",
    }.issubset({r[1] for r in system.exchange.requests})
    with pytest.raises(ValueError, match="local Lima paper"):
        _ = BybitDemoBot(cfg, Mock(), Mock())


def test_resume_rejects_changed_leverage_or_market(
    system: TradingSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify resume rejects changed leverage or market."""
    path = system.state_path.with_name("config.json")
    cfg = replace(PaperBotConfig.load(path), leverage=2)
    cfg.save(path)
    monkeypatch.setattr(
        leverage_data, "BASE_URL", system.exchange.url + "/v5/market/"
    )
    _ = system.producer().process_available_candles()
    before = json_object(system.state_path.read_text())["margin_state"]
    replace(cfg, leverage=3).save(path)
    with pytest.raises(ValueError, match="settings changed"):
        _ = system.producer().process_available_candles()
    assert json_object(system.state_path.read_text())["margin_state"] == before


def test_live_paper_persists_margin_and_status_reports_it(
    system: TradingSystem,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify live paper persists margin and status reports it."""
    path = system.state_path.with_name("config.json")
    cfg = replace(PaperBotConfig.load(path), leverage=2)
    cfg.save(path)
    monkeypatch.setattr(
        leverage_data, "BASE_URL", system.exchange.url + "/v5/market/"
    )
    monkeypatch.setattr(cli, "datetime", system.clock)
    monkeypatch.setattr(
        market, "BYBIT_KLINES_URL", system.exchange.url + "/v5/market/kline"
    )
    monkeypatch.setattr(
        trading,
        "BYBIT_FEE_RATE_URL",
        system.exchange.url + "/v5/account/fee-rate",
    )
    monkeypatch.setattr(
        cli, "load_bybit_credentials", lambda: ("test-key", "test-secret")
    )
    monkeypatch.setattr(
        cli,
        "LocalPaperTrader",
        lambda: trading.LocalPaperTrader(
            system.account.with_name("live_account.json")
        ),
    )
    state_path = system.state_path.with_name("live_state.json")
    cli.BotState("2026-01-01T00:00:00+00:00").save(state_path)

    def create() -> cli.PaperBot:
        """Create.

        Returns:
            The create result.

        """
        bot = cli.PaperBot(
            path, state_path, system.state_path.with_name("live_candles.sqlite")
        )
        bot.market_feed = market.RestMarketFeed()
        return bot

    assert create().process_available_candles() == INITIAL_CANDLE_COUNT
    assert create().process_available_candles() == 0
    monkeypatch.setattr(cli, "STATE_FILE", state_path)
    monkeypatch.setattr(cli, "read_pid", lambda: None)
    cli.status_command(path)
    assert "Isolated leverage: 2x" in capsys.readouterr().out
    system.clock.value += timedelta(minutes=1)
    assert create().process_available_candles() == 1
    cli.stats_command(path)
    assert "Leverage: 2x" in capsys.readouterr().out


@pytest.mark.parametrize("target", [None, 101.0])
def test_margin_pending_orders_survive_state_reload(
    system: TradingSystem, monkeypatch: pytest.MonkeyPatch, target: float | None
) -> None:
    """Preserve pending entry allocations and optional targets on resume."""
    path = system.state_path.with_name("config.json")
    replace(PaperBotConfig.load(path), leverage=2).save(path)
    monkeypatch.setattr(
        leverage_data, "BASE_URL", system.exchange.url + "/v5/market/"
    )
    _ = system.producer().process_available_candles()
    state = BotState.load_or_create(system.state_path)
    assert state.margin_state is not None
    pending: MarginOrder = {
        "price": 100.0,
        "allocation": 50.0,
        "target": target,
    }
    state.margin_state["pending"]["Long"] = pending
    state.save(system.state_path)
    resumed = BotState.load_or_create(system.state_path)
    assert resumed.margin_state is not None
    assert resumed.margin_state["pending"]["Long"] == pending
    payload = json_object(system.state_path.read_text())
    margin = object_mapping(payload["margin_state"])
    margin["pending"] = {"Long": {**pending, "allocation": "invalid"}}
    payload["margin_state"] = margin
    _ = system.state_path.write_text(json.dumps(payload))
    before = system.state_path.read_bytes()
    with pytest.raises(TypeError, match="number"):
        _ = BotState.load_or_create(system.state_path)
    assert system.state_path.read_bytes() == before
