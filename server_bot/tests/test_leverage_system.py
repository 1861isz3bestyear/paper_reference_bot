from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import Mock

import pytest

from reference_bot.config import PaperBotConfig
from shared import leverage_data
from bybit_demo_bot.cli import BybitDemoBot

pytestmark = pytest.mark.integration


def test_persisted_leverage_config_runs_with_real_http_and_resumes(system, monkeypatch):
    path = system.state_path.with_name("config.json")
    cfg = replace(PaperBotConfig.load(path), leverage=10, slippage_bps=2.0)
    cfg.save(path)
    monkeypatch.setattr(leverage_data, "BASE_URL", system.exchange.url + "/v5/market/")
    bot = system.producer()
    assert bot.process_available_candles() == 2
    saved = json.loads(system.state_path.read_text())
    position = saved["margin_state"]["positions"]["Long"]
    assert position["quantity"] > 90
    assert position["margin"] < 1000
    assert position["entry_fee"] > 9
    assert system.producer().process_available_candles() == 0
    assert (
        json.loads(system.state_path.read_text())["margin_state"]
        == saved["margin_state"]
    )
    system.clock.value += timedelta(minutes=1)
    assert system.producer().process_available_candles() == 1
    ledger = json.loads(system.state_path.read_text())["margin_state"]
    assert not ledger["positions"]
    assert ledger["history"][0]["leverage"] == 10
    assert ledger["cash"] > 1000
    system.producer().run_forever()
    assert json.loads(system.target.read_text())["version"] == 3
    assert not system.exchange.submissions
    assert {
        "/v5/market/mark-price-kline",
        "/v5/market/funding/history",
        "/v5/market/risk-limit",
    }.issubset({r[1] for r in system.exchange.requests})
    with pytest.raises(ValueError, match="local Lima paper"):
        BybitDemoBot(cfg, Mock(), Mock())


def test_resume_rejects_changed_leverage_or_market(system, monkeypatch):
    path = system.state_path.with_name("config.json")
    cfg = replace(PaperBotConfig.load(path), leverage=2)
    cfg.save(path)
    monkeypatch.setattr(leverage_data, "BASE_URL", system.exchange.url + "/v5/market/")
    system.producer().process_available_candles()
    before = json.loads(system.state_path.read_text())["margin_state"]
    replace(cfg, leverage=3).save(path)
    with pytest.raises(ValueError, match="settings changed"):
        system.producer().process_available_candles()
    assert json.loads(system.state_path.read_text())["margin_state"] == before


def test_live_paper_persists_margin_and_status_reports_it(system, monkeypatch, capsys):
    from live_paper_bot import cli, market, trading

    path = system.state_path.with_name("config.json")
    cfg = replace(PaperBotConfig.load(path), leverage=2)
    cfg.save(path)
    monkeypatch.setattr(leverage_data, "BASE_URL", system.exchange.url + "/v5/market/")
    monkeypatch.setattr(cli, "datetime", system.clock)
    monkeypatch.setattr(
        market, "BYBIT_KLINES_URL", system.exchange.url + "/v5/market/kline"
    )
    monkeypatch.setattr(
        trading, "BYBIT_FEE_RATE_URL", system.exchange.url + "/v5/account/fee-rate"
    )
    monkeypatch.setattr(
        cli, "load_bybit_credentials", lambda: ("test-key", "test-secret")
    )
    monkeypatch.setattr(
        cli,
        "LocalPaperTrader",
        lambda: trading.LocalPaperTrader(system.account.with_name("live_account.json")),
    )
    state_path = system.state_path.with_name("live_state.json")
    cli.BotState("2026-01-01T00:00:00+00:00").save(state_path)

    def create():
        bot = cli.PaperBot(
            path, state_path, system.state_path.with_name("live_candles.sqlite")
        )
        bot.market_feed = market.RestMarketFeed()
        return bot

    assert create().process_available_candles() == 2
    assert create().process_available_candles() == 0
    monkeypatch.setattr(cli, "STATE_FILE", state_path)
    monkeypatch.setattr(cli, "read_pid", lambda: None)
    cli.status_command(path)
    assert "Isolated leverage: 2x" in capsys.readouterr().out
    system.clock.value += timedelta(minutes=1)
    assert create().process_available_candles() == 1
    cli.stats_command(path)
    assert "Leverage: 2x" in capsys.readouterr().out
