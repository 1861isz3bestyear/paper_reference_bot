# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for tests test leverage system."""

import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from bybit_demo_bot.cli import BybitDemoBot
from reference_bot.config import PaperBotConfig
from shared import leverage_data

pytestmark = pytest.mark.integration


def test_persisted_leverage_config_runs_with_real_http_and_resumes(
    system, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify persisted leverage config runs with real http and resumes."""
    path = system.state_path.with_name("config.json")
    cfg = replace(PaperBotConfig.load(path), leverage=10, slippage_bps=2.0)
    cfg.save(path)
    monkeypatch.setattr(
        leverage_data, "BASE_URL", system.exchange.url + "/v5/market/"
    )
    bot = system.producer()
    if bot.process_available_candles() != 2:
        pytest.fail("Expected bot.process_available_candles() == 2")
    saved = json.loads(system.state_path.read_text())
    position = saved["margin_state"]["positions"]["Long"]
    if not ((position["quantity"]) > (90)):
        pytest.fail("Expected position['quantity'] > 90")
    if not ((position["margin"]) < (1000)):
        pytest.fail("Expected position['margin'] < 1000")
    if not ((position["entry_fee"]) > (9)):
        pytest.fail("Expected position['entry_fee'] > 9")
    if system.producer().process_available_candles() != 0:
        pytest.fail(
            "Expected system.producer().process_available_candles() == 0"
        )
    if (
        json.loads(system.state_path.read_text())["margin_state"]
        != saved["margin_state"]
    ):
        pytest.fail(
            "Expected json.loads(system.state_path.read_text())['margin_state']"
            " == saved['margin_state']"
        )
    system.clock.value += timedelta(minutes=1)
    if system.producer().process_available_candles() != 1:
        pytest.fail(
            "Expected system.producer().process_available_candles() == 1"
        )
    ledger = json.loads(system.state_path.read_text())["margin_state"]
    if ledger["positions"]:
        pytest.fail("Expected not ledger['positions']")
    if ledger["history"][0]["leverage"] != 10:
        pytest.fail("Expected ledger['history'][0]['leverage'] == 10")
    if not ((ledger["cash"]) > (1000)):
        pytest.fail("Expected ledger['cash'] > 1000")
    system.producer().run_forever()
    if json.loads(system.target.read_text())["version"] != 3:
        pytest.fail(
            "Expected json.loads(system.target.read_text())['version'] == 3"
        )
    if system.exchange.submissions:
        pytest.fail("Expected not system.exchange.submissions")
    if not (
        {
            "/v5/market/mark-price-kline",
            "/v5/market/funding/history",
            "/v5/market/risk-limit",
        }.issubset({r[1] for r in system.exchange.requests})
    ):
        pytest.fail(
            "Expected {'/v5/market/mark-price-kline',"
            " '/v5/market/funding/history',"
            " '/v5/market/risk-limit'}.issubset({r[1] for r in"
            " system.exchange.requests})"
        )
    with pytest.raises(ValueError, match="local Lima paper"):
        _ = BybitDemoBot(cfg, Mock(), Mock())


def test_resume_rejects_changed_leverage_or_market(
    system, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify resume rejects changed leverage or market."""
    path = system.state_path.with_name("config.json")
    cfg = replace(PaperBotConfig.load(path), leverage=2)
    cfg.save(path)
    monkeypatch.setattr(
        leverage_data, "BASE_URL", system.exchange.url + "/v5/market/"
    )
    system.producer().process_available_candles()
    before = json.loads(system.state_path.read_text())["margin_state"]
    replace(cfg, leverage=3).save(path)
    with pytest.raises(ValueError, match="settings changed"):
        system.producer().process_available_candles()
    if json.loads(system.state_path.read_text())["margin_state"] != before:
        pytest.fail(
            "Expected json.loads(system.state_path.read_text())['margin_state']"
            " == before"
        )


def test_live_paper_persists_margin_and_status_reports_it(
    system, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify live paper persists margin and status reports it."""
    from live_paper_bot import cli, market, trading

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

    def create():
        """Create."""
        bot = cli.PaperBot(
            path, state_path, system.state_path.with_name("live_candles.sqlite")
        )
        bot.market_feed = market.RestMarketFeed()
        return bot

    if create().process_available_candles() != 2:
        pytest.fail("Expected create().process_available_candles() == 2")
    if create().process_available_candles() != 0:
        pytest.fail("Expected create().process_available_candles() == 0")
    monkeypatch.setattr(cli, "STATE_FILE", state_path)
    monkeypatch.setattr(cli, "read_pid", lambda: None)
    cli.status_command(path)
    if "Isolated leverage: 2x" not in (capsys.readouterr().out):
        pytest.fail(
            "Expected 'Isolated leverage: 2x' in capsys.readouterr().out"
        )
    system.clock.value += timedelta(minutes=1)
    if create().process_available_candles() != 1:
        pytest.fail("Expected create().process_available_candles() == 1")
    cli.stats_command(path)
    if "Leverage: 2x" not in (capsys.readouterr().out):
        pytest.fail("Expected 'Leverage: 2x' in capsys.readouterr().out")
