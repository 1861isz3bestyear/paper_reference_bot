from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock
from urllib.error import URLError

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
    client.set_protection("BTCUSDT", Decimal("99"), Decimal("110"))
    assert requests[-1].full_url.startswith("https://api-demo.bybit.com/v5/position/trading-stop")
    assert requests[-1].headers["X-bapi-sign"]
    assert json.loads(requests[-1].data) == {
        "category": "linear", "symbol": "BTCUSDT", "positionIdx": 0, "tpslMode": "Full",
        "stopLoss": "99", "takeProfit": "110", "slOrderType": "Market", "tpOrderType": "Market",
    }


def test_client_rejects_api_errors_and_missing_rows(monkeypatch):
    monkeypatch.setattr(client_module, "urlopen", lambda *_a, **_k: Response({"retCode": 10001, "retMsg": "bad"}))
    with pytest.raises(BybitDemoError, match="bad"):
        BybitDemoClient("k", "s").last_price("BTCUSDT")
    monkeypatch.setattr(client_module, "urlopen", lambda *_a, **_k: Response({"retCode": 0, "result": {"list": []}}))
    with pytest.raises(BybitDemoError, match="no instrument"):
        BybitDemoClient("k", "s").instruments("BTCUSDT")


def test_client_accepts_idempotent_protection_not_modified(monkeypatch):
    monkeypatch.setattr(client_module, "urlopen", lambda *_a, **_k: Response({
        "retCode": 34040, "retMsg": "not modified", "result": {},
    }))
    client = BybitDemoClient("k", "s")
    client.set_protection("XRPUSDT", Decimal("0.50"), Decimal("0.60"))


def test_client_still_rejects_real_protection_errors(monkeypatch):
    monkeypatch.setattr(client_module, "urlopen", lambda *_a, **_k: Response({
        "retCode": 10001, "retMsg": "invalid take profit", "result": {},
    }))
    with pytest.raises(BybitDemoError, match="invalid take profit"):
        BybitDemoClient("k", "s").set_protection("XRPUSDT", Decimal("0.50"), Decimal("0.60"))


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


def test_quantity_obeys_exchange_notional_step_and_available_balance(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.instruments.return_value = {"lotSizeFilter": {
        "qtyStep": "1", "minOrderQty": "1", "minNotionalValue": "5",
    }}
    client.available_usdt.return_value = Decimal("8")
    bot = BybitDemoBot(config(ticker="XRP_USDT", initial_capital=100), client, DemoState.load_or_create(False))
    assert bot._quantity(Decimal("2")) == Decimal("3")  # 98% balance cap, then floor to whole XRP.
    client.available_usdt.return_value = Decimal("6")
    with pytest.raises(RuntimeError, match="minimum"):
        bot._quantity(Decimal("2"))


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
    monkeypatch.setattr(BybitDemoBot, "_exit_band", lambda *_: Decimal("110"))
    bot = BybitDemoBot(config(anchor_before_strategy_start=True, anchor_before_days=1), client, state)
    now = datetime(2026, 1, 1, 0, 2, 30, tzinfo=timezone.utc)
    assert "opened Buy" in bot.reconcile_once(now)
    assert state.pending_protection_side == "Buy"
    assert state.pending_take_profit == "110"
    assert "protected" in bot.reconcile_once(now)
    client.set_protection.assert_called_once_with("BTCUSDT", Decimal("99.600"), Decimal("110"))


def test_exit_band_matches_configured_strategy_close_sigma(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    rows = pd.DataFrame([{"anchored_vwap": 100.0, "anchored_std": 5.0}])
    monkeypatch.setattr(cli, "add_launch_weekly_anchored_vwap", lambda *_: rows)
    launched = pd.Timestamp("2026-01-01T00:00:00Z")
    bot = BybitDemoBot(config(close_order_vwap_sigma=2), Mock(), DemoState.load_or_create(False))
    assert bot._exit_band(pd.DataFrame([{}]), launched, "Buy") == Decimal("110.0")
    assert bot._exit_band(pd.DataFrame([{}]), launched, "Sell") == Decimal("90.0")


def test_existing_position_refreshes_tp_to_latest_close_band(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = {"side": "Buy", "size": "1", "avgPrice": "100"}
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(cli, "fetch_completed_linear_klines", lambda *_: candles)
    monkeypatch.setattr(cli, "calculate_strategy_decision", lambda *_: Mock(side="Long"))
    monkeypatch.setattr(BybitDemoBot, "_exit_band", lambda *_: Decimal("112"))
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))
    result = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=timezone.utc))
    assert result == "updated Buy protection to current VWAP close band"
    client.set_protection.assert_called_once_with("BTCUSDT", Decimal("99.600"), Decimal("112"))


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


def test_reconcile_reverses_position_with_reduce_only_close_first(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = {"side": "Sell", "size": "2", "avgPrice": "2"}
    client.last_price.return_value = Decimal("2")
    client.available_usdt.return_value = Decimal("100")
    client.instruments.return_value = {"lotSizeFilter": {"qtyStep": "1", "minOrderQty": "1", "minNotionalValue": "5"}}
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(cli, "fetch_completed_linear_klines", lambda *_: candles)
    monkeypatch.setattr(cli, "calculate_strategy_decision", lambda *_: Mock(side="Long"))
    monkeypatch.setattr(BybitDemoBot, "_exit_band", lambda *_: Decimal("3"))
    bot = BybitDemoBot(config(ticker="XRP_USDT"), client, DemoState("2026-01-01T00:00:00+00:00"))
    result = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=timezone.utc))
    assert result == "closed Sell; opened Buy; awaiting fill for protection"
    assert client.market_order.call_args_list[0].kwargs == {"reduce_only": True}
    assert client.market_order.call_args_list[1].kwargs == {}


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
    state = DemoState("2026-01-01T00:00:00+00:00", pending_protection_side="Buy", pending_take_profit="110")
    bot = BybitDemoBot(config(), client, state)
    with pytest.raises(RuntimeError, match="emergency close submitted"):
        bot.reconcile_once()
    client.market_order.assert_called_once_with("BTCUSDT", "Sell", Decimal("0.01"), reduce_only=True)
    with pytest.raises(RuntimeError, match="trading halted"):
        bot.reconcile_once()


def test_protection_and_emergency_close_failure_persists_halt(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = {"side": "Buy", "size": "1", "avgPrice": "100"}
    client.set_protection.side_effect = BybitDemoError("protection rejected")
    client.market_order.side_effect = BybitDemoError("close rejected")
    state = DemoState("2026-01-01T00:00:00+00:00", pending_protection_side="Buy", pending_take_profit="110")
    with pytest.raises(RuntimeError, match="emergency close failed"):
        BybitDemoBot(config(), client, state).reconcile_once()
    assert "emergency close failed" in (state.halted_reason or "")
    assert json.loads((tmp_path / "state.json").read_text())["halted_reason"] == state.halted_reason


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


def test_demo_default_env_name_and_credentials_are_wired_without_network(monkeypatch, tmp_path):
    assert cli.ENV_FILE.name == "bybitapidemo.env"
    config_path = tmp_path / "config.json"
    config_path.write_text(config().to_json(), encoding="utf-8")
    env_path = tmp_path / "bybitapidemo.env"
    env_path.write_text("# demo only\nBYBIT_API_KEY='demo-key'\nBYBIT_API_SECRET=demo-secret\n", encoding="utf-8")
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client_factory = Mock(return_value=Mock())
    monkeypatch.setattr(cli, "BybitDemoClient", client_factory)
    monkeypatch.setattr(cli.BybitDemoBot, "run", Mock())
    cli.run_demo_command(config_path, env_path)
    client_factory.assert_called_once_with("demo-key", "demo-secret")


@pytest.mark.parametrize("contents, message", [
    ("BYBIT_API_SECRET=s\n", "BYBIT_API_KEY"),
    ("BYBIT_API_KEY=k\n", "BYBIT_API_SECRET"),
    ("BYBIT_API_KEY=\nBYBIT_API_SECRET=s\n", "BYBIT_API_KEY"),
])
def test_demo_rejects_missing_or_blank_credentials(monkeypatch, tmp_path, contents, message):
    config_path = tmp_path / "config.json"
    config_path.write_text(config().to_json(), encoding="utf-8")
    env_path = tmp_path / "bybitapidemo.env"
    env_path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "lock")
    with pytest.raises(ValueError, match=message):
        cli.run_demo_command(config_path, env_path)


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


def test_client_wraps_transport_and_invalid_json_errors(monkeypatch):
    monkeypatch.setattr(client_module, "urlopen", Mock(side_effect=URLError("offline")))
    with pytest.raises(BybitDemoError, match="request failed"):
        BybitDemoClient("k", "s").last_price("BTCUSDT")

    bad = Mock()
    bad.__enter__ = Mock(return_value=bad)
    bad.__exit__ = Mock(return_value=None)
    bad.read.return_value = b"not-json"
    monkeypatch.setattr(client_module, "urlopen", Mock(return_value=bad))
    with pytest.raises(BybitDemoError, match="request failed"):
        BybitDemoClient("k", "s").last_price("BTCUSDT")


def test_client_never_targets_mainnet_and_signs_post_body(monkeypatch):
    captured = []
    monkeypatch.setattr(client_module.time, "time", lambda: 1234.567)
    monkeypatch.setattr(client_module, "urlopen", lambda request, timeout: captured.append(request) or Response({"retCode": 0, "result": {"orderId": "id"}}))
    BybitDemoClient("demo-key", "demo-secret").market_order("XRPUSDT", "Buy", Decimal("2"))
    request = captured[0]
    assert request.full_url == "https://api-demo.bybit.com/v5/order/create"
    assert request.full_url != "https://api.bybit.com/v5/order/create"
    assert request.headers["X-bapi-api-key"] == "demo-key"
    assert request.headers["X-bapi-sign"]
