from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from real_bot import cli
from real_bot.client import MEXCFuturesClient


def config() -> Mock:
    return Mock(data_source="MEXC REST", reverse_ticker=False, strategy_mode="VWAP band mean reversion",
                trend=True, ticker="SHIB_USDT", timeframe="1m", initial_capital=10.0,
                minimum_order_size=0.01, stop_loss_pct=0.4)


def bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[cli.RealBot, Mock]:
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(cli, "REFERENCE_CANDLES", tmp_path / "candles.sqlite")
    client = Mock()
    client.open_plan_orders.return_value = []
    client.positions.return_value = []
    client.available_usdt.return_value = Decimal("20")
    client.contract.return_value = {"contractSize": "1000", "priceUnit": "0.000001", "minVol": 1, "volUnit": 1}
    client.submit_trigger_order.side_effect = ["one", "two"]
    instance = cli.RealBot(config(), client, leverage=1, open_type=1)
    monkeypatch.setattr(instance, "_latest_candle", lambda _: (pd.Timestamp("2026-08-29T12:00:00Z"), pd.Timestamp("2026-08-01T00:00:00Z")))
    monkeypatch.setattr(instance, "_bands", lambda *_: tuple(map(Decimal, ("0.000011", "0.000012", "0.000009", "0.000008"))))
    return instance, client


def test_places_plus_and_minus_trigger_pair_and_saves_protection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    action = instance.reconcile_once(180)
    assert "refreshed" in action
    calls = client.submit_trigger_order.call_args_list
    assert [call.kwargs["side"] for call in calls] == [1, 3]
    assert [call.kwargs["trigger_price"] for call in calls] == [Decimal("0.000011"), Decimal("0.000009")]
    assert instance.state.protections["one"]["take_profit"] == "0.000012"
    assert instance.state.protections["two"]["take_profit"] == "0.000008"


def test_new_candle_cancels_previous_before_replacement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.order_ids = ["old-one", "old-two"]
    instance.state.orders_placed_candle = "2026-08-29T11:59:00+00:00"
    client.open_plan_orders.return_value = [{"orderId": "old-one"}, {"orderId": "old-two"}]
    events = []
    client.cancel_plan_orders.side_effect = lambda *_: events.append("cancel")
    client.submit_trigger_order.side_effect = lambda **_: events.append("submit") or f"new-{len(events)}"
    monkeypatch.setattr(instance, "_bands", lambda *_: events.append("calculate") or tuple(map(Decimal, ("0.000011", "0.000012", "0.000009", "0.000008"))))
    instance.reconcile_once(180)
    assert events == ["calculate", "cancel", "submit", "submit"]


def test_fill_cancels_sibling_and_places_no_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.order_ids = ["filled", "sibling"]
    instance.state.protections = {
        "filled": {"side": "Long", "stop_loss": "0.000010", "take_profit": "0.000012"},
        "sibling": {"side": "Short", "stop_loss": "0.000010", "take_profit": "0.000008"},
    }
    client.open_plan_orders.return_value = [{"orderId": "sibling"}]
    client.positions.return_value = [{"positionId": 99, "holdVol": 5, "positionType": 1}]
    action = instance.reconcile_once(180)
    assert "exchange-side SL/TP installed" in action
    client.set_position_protection.assert_called_once_with(
        99, stop_loss_price=Decimal("0.000010"), take_profit_price=Decimal("0.000012")
    )
    client.cancel_plan_orders.assert_called_once_with("SHIB_USDT", ["sibling"])
    client.submit_trigger_order.assert_not_called()


def test_second_submit_failure_cancels_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    client.submit_trigger_order.side_effect = ["one", RuntimeError("failed")]
    with pytest.raises(RuntimeError, match="failed"):
        instance.reconcile_once(180)
    client.cancel_plan_orders.assert_called_once_with("SHIB_USDT", ["one"])


def test_pair_expires_after_ten_wall_clock_minutes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.order_ids = ["one", "two"]
    instance.state.orders_placed_at = (datetime.now(timezone.utc) - timedelta(minutes=10, seconds=1)).isoformat()
    client.open_plan_orders.return_value = [{"orderId": "one"}, {"orderId": "two"}]
    action = instance.reconcile_once(180, order_lifetime_minutes=10)
    assert "after 10 minutes" in action
    client.cancel_plan_orders.assert_called_once_with("SHIB_USDT", ["one", "two"])
    client.submit_trigger_order.assert_not_called()


def test_stop_prices_use_actual_deposit_and_leverage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.leverage = 2
    client.contract.return_value["priceUnit"] = "0.000000001"
    instance.reconcile_once(180)
    assert instance.state.protections["one"]["stop_loss"] == "0.000010978"
    assert instance.state.protections["two"]["stop_loss"] == "0.000009018"


def test_sizing_reserves_half_available_margin_per_leg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    client.available_usdt.return_value = Decimal("9")
    instance.reconcile_once(180)
    volumes = [call.kwargs["volume"] for call in client.submit_trigger_order.call_args_list]
    assert volumes == [400, 400]


def test_failed_submit_and_cancel_keeps_recovery_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    client.submit_trigger_order.side_effect = ["123", RuntimeError("insufficient")]
    client.cancel_plan_orders.side_effect = RuntimeError("parameter error")
    with pytest.raises(RuntimeError, match="cancellation failed"):
        instance.reconcile_once(180)
    assert instance.state.order_ids == ["123"]
    assert instance.state.side_protections["Long"]["take_profit"] == "0.000012"


def test_missing_trigger_waits_for_position_without_losing_recovery_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.order_ids = ["filled", "sibling"]
    instance.state.protections = {"filled": {"side": "Long", "stop_loss": "1", "take_profit": "2"}}
    client.open_plan_orders.return_value = [{"orderId": "sibling"}]
    action = instance.reconcile_once(180)
    assert "waiting for position confirmation" in action
    client.cancel_plan_orders.assert_called_once_with("SHIB_USDT", ["sibling"])
    assert instance.state.order_ids == ["filled", "sibling"]
    assert instance.state.protections["filled"]["take_profit"] == "2"


def test_client_submits_directional_market_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MEXCFuturesClient("key", "secret")
    request = Mock(return_value=456)
    monkeypatch.setattr(client, "_request", request)
    result = client.submit_trigger_order(
        symbol="SHIB_USDT", side=3, volume=1300, trigger_price=Decimal("0.000009"),
        leverage=1, open_type=1,
    )
    assert result == 456
    path, payload = request.call_args.args[1:]
    assert path == "/api/v1/private/planorder/place"
    assert payload == {
        "symbol": "SHIB_USDT", "vol": 1300, "side": 3, "type": 5,
        "openType": 1, "leverage": 1, "triggerPrice": "0.000009",
        "triggerType": 2, "executeCycle": 1, "trend": 1,
    }


def test_client_installs_both_position_protection_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MEXCFuturesClient("key", "secret")
    request = Mock(return_value=True)
    monkeypatch.setattr(client, "_request", request)
    client.set_position_protection(
        99, stop_loss_price=Decimal("0.000010"), take_profit_price=Decimal("0.000012")
    )
    request.assert_called_once_with("POST", "/api/v1/private/stoporder/change_plan_order", {
        "positionId": 99,
        "stopLossPrice": "0.000010",
        "takeProfitPrice": "0.000012",
    })
