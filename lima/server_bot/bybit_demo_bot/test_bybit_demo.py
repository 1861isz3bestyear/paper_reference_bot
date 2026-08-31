from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import Mock

import pandas as pd
import pytest

from bybit_demo_bot import client as client_module
from bybit_demo_bot import cli
from bybit_demo_bot.cli import BybitDemoBot, DemoState
from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from reference_bot.config import PaperBotConfig


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def config(**changes):
    values = dict(
        strategy_mode="VWAP band mean reversion", timeframe="1m", trend=True,
        open_order_vwap_sigma=1, close_order_vwap_sigma=2, initial_capital=10,
        stop_loss_pct=0.4, allow_immediate_reentry=True, vwap_anchor_reset_weeks=2,
        data_source="Bybit REST", ticker="BTC_USDT", minimum_order_size=.01,
    )
    values.update(changes)
    return PaperBotConfig(**values)


def test_client_public_and_private_helpers(monkeypatch):
    payloads = iter([
        {"retCode": 0, "result": {"list": [{"lotSizeFilter": {"qtyStep": "0.001"}}]}},
        {"retCode": 0, "result": {"list": [{"lastPrice": "100"}]}},
        {"retCode": 0, "result": {"list": [{"coin": [{"coin": "USDT", "walletBalance": "50"}]}]}},
        {"retCode": 0, "result": {"list": [{"side": "Buy", "size": "1"}]}},
        {"retCode": 0, "result": {"orderId": "abc"}},
        {"retCode": 0, "result": {}},
    ])
    requests = []
    def open_(request, timeout):
        requests.append(request)
        return Response(next(payloads))
    monkeypatch.setattr(client_module, "urlopen", open_)
    client = BybitDemoClient("key", "secret")
    assert client.instruments("BTCUSDT")["lotSizeFilter"]
    assert client.last_price("BTCUSDT") == 100
    assert client.available_usdt() == 50
    assert client.position("BTCUSDT")["side"] == "Buy"
    assert client.market_order("BTCUSDT", "Buy", Decimal(".01")) == "abc"
    client.set_protection("BTCUSDT", Decimal("99"))
    assert requests[-1].full_url.startswith("https://api-demo.bybit.com/v5/position/trading-stop")
    assert requests[-1].headers["X-bapi-sign"]


def test_client_rejects_api_errors_and_missing_rows(monkeypatch):
    monkeypatch.setattr(client_module, "urlopen", lambda *_a, **_k: Response({"retCode": 10001, "retMsg": "bad"}))
    with pytest.raises(BybitDemoError, match="bad"):
        BybitDemoClient("k", "s").last_price("BTCUSDT")
    monkeypatch.setattr(client_module, "urlopen", lambda *_a, **_k: Response({"retCode": 0, "result": {"list": []}}))
    with pytest.raises(BybitDemoError, match="no instrument"):
        BybitDemoClient("k", "s").instruments("BTCUSDT")


def test_bot_validates_config_and_sizes(monkeypatch, tmp_path):
    monkeypatch.setattr("bybit_demo_bot.cli.STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.instruments.return_value = {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}}
    client.available_usdt.return_value = Decimal("100")
    bot = BybitDemoBot(config(), client, DemoState.load_or_create(False))
    assert bot._quantity(Decimal("1000")) == Decimal("0.010")
    with pytest.raises(ValueError, match="requires"):
        BybitDemoBot(config(data_source="MEXC REST"), client, bot.state)
    client.available_usdt.return_value = Decimal("0")
    with pytest.raises(RuntimeError, match="minimum"):
        bot._quantity(Decimal("1000"))


def test_reconcile_opens_then_protects(monkeypatch, tmp_path):
    monkeypatch.setattr("bybit_demo_bot.cli.STATE_FILE", tmp_path / "state.json")
    state = DemoState("2026-01-01T00:00:00+00:00")
    client = Mock()
    client.position.side_effect = [None, {"side": "Buy", "size": "0.01", "avgPrice": "100"}]
    client.last_price.return_value = Decimal("100")
    client.available_usdt.return_value = Decimal("100")
    client.instruments.return_value = {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}}
    candle = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:00:00Z")}, {"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr("bybit_demo_bot.cli.fetch_completed_linear_klines", lambda *_: candle)
    monkeypatch.setattr("bybit_demo_bot.cli.calculate_strategy_decision", lambda *_: Mock(side="Long"))
    bot = BybitDemoBot(config(anchor_before_strategy_start=True, anchor_before_days=1), client, state)
    now = datetime(2026, 1, 1, 0, 2, 30, tzinfo=timezone.utc)
    assert "opened Buy" in bot.reconcile_once(now)
    assert state.pending_protection_side == "Buy"
    assert "protected" in bot.reconcile_once(now)
    client.set_protection.assert_called_once()


def test_reconcile_closes_opposite_position(monkeypatch, tmp_path):
    monkeypatch.setattr("bybit_demo_bot.cli.STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = {"side": "Sell", "size": "0.02", "avgPrice": "100"}
    candle = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:00:00Z")}, {"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr("bybit_demo_bot.cli.fetch_completed_linear_klines", lambda *_: candle)
    monkeypatch.setattr("bybit_demo_bot.cli.calculate_strategy_decision", lambda *_: Mock(side=None))
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))
    assert bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 10, tzinfo=timezone.utc)) == "closed Sell"
    client.market_order.assert_called_once_with("BTCUSDT", "Buy", Decimal("0.02"), reduce_only=True)


def test_state_resume_stop_and_same_candle(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(cli, "STATE_FILE", state_path)
    created = DemoState.load_or_create(False)
    resumed = DemoState.load_or_create(True)
    assert resumed.launched_at == created.launched_at
    client = Mock()
    client.position.return_value = None
    bot = BybitDemoBot(config(), client, resumed)
    bot.stop()
    assert bot.running is False
    latest = pd.Timestamp("2026-01-01T00:01:00Z")
    bot.state.last_processed_candle = latest.isoformat()
    assert bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=timezone.utc)) is None


def test_protection_failure_emergency_closes_and_halts(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    position = {"side": "Buy", "size": "0.01", "avgPrice": "100"}
    client = Mock()
    client.position.return_value = position
    client.set_protection.side_effect = BybitDemoError("no protection")
    state = DemoState("2026-01-01T00:00:00+00:00", pending_protection_side="Buy")
    bot = BybitDemoBot(config(), client, state)
    with pytest.raises(RuntimeError, match="emergency close submitted"):
        bot.reconcile_once()
    client.market_order.assert_called_once_with("BTCUSDT", "Sell", Decimal("0.01"), reduce_only=True)
    with pytest.raises(RuntimeError, match="trading halted"):
        bot.reconcile_once()


def test_stale_history_and_invalid_state(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(cli, "STATE_FILE", state_path)
    state_path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cannot read"):
        DemoState.load_or_create(True)
    bot = BybitDemoBot(config(), Mock(position=Mock(return_value=None)), DemoState("2026-01-01T00:00:00+00:00"))
    monkeypatch.setattr(cli, "fetch_completed_linear_klines", lambda *_: pd.DataFrame())
    with pytest.raises(RuntimeError, match="not current"):
        bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=timezone.utc))


def test_run_demo_wires_env_config_and_lock(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(config().to_json(), encoding="utf-8")
    env_path = tmp_path / "bybitapi.env"
    env_path.write_text("BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n", encoding="utf-8")
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    run = Mock()
    monkeypatch.setattr(cli.BybitDemoBot, "run", run)
    cli.run_demo_command(config_path, env_path, poll_seconds=3)
    run.assert_called_once_with(3)
    with pytest.raises(ValueError, match="positive"):
        cli.run_demo_command(config_path, env_path, poll_seconds=0)


def test_client_missing_ticker_position_and_order_id(monkeypatch):
    payloads = iter([
        {"retCode": 0, "result": {"list": []}},
        {"retCode": 0, "result": {"list": []}},
        {"retCode": 0, "result": {}},
    ])
    monkeypatch.setattr(client_module, "urlopen", lambda *_a, **_k: Response(next(payloads)))
    client = BybitDemoClient("k", "s")
    with pytest.raises(BybitDemoError, match="no ticker"):
        client.last_price("BTCUSDT")
    assert client.position("BTCUSDT") is None
    with pytest.raises(BybitDemoError, match="no order ID"):
        client.market_order("BTCUSDT", "Buy", Decimal("1"))
