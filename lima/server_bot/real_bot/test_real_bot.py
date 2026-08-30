from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from real_bot import cli


def config() -> Mock:
    return Mock(data_source="MEXC REST", reverse_ticker=False, strategy_mode="VWAP band mean reversion",
                trend=True, ticker="SHIB_USDT", timeframe="1m", initial_capital=10.0,
                minimum_order_size=0.01, stop_loss_pct=0.4)


def bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[cli.RealBot, Mock]:
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(cli, "REFERENCE_CANDLES", tmp_path / "candles.sqlite")
    client = Mock()
    client.open_orders.return_value = []
    client.positions.return_value = []
    client.available_usdt.return_value = Decimal("20")
    client.contract.return_value = {"contractSize": "1000", "priceUnit": "0.000001", "minVol": 1, "volUnit": 1}
    client.submit_limit_order.side_effect = ["one", "two"]
    instance = cli.RealBot(config(), client, leverage=1, open_type=1)
    monkeypatch.setattr(instance, "_latest_candle", lambda _: (pd.Timestamp("2026-08-29T12:00:00Z"), pd.Timestamp("2026-08-01T00:00:00Z")))
    monkeypatch.setattr(instance, "_bands", lambda *_: tuple(map(Decimal, ("0.000011", "0.000012", "0.000009", "0.000008"))))
    return instance, client


def test_places_plus_and_minus_limit_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    action = instance.reconcile_once(180)
    assert "refreshed" in action
    assert [call.kwargs["side"] for call in client.submit_limit_order.call_args_list] == [1, 3]
    assert client.submit_limit_order.call_args_list[0].kwargs["take_profit_price"] == Decimal("0.000012")
    assert client.submit_limit_order.call_args_list[1].kwargs["take_profit_price"] == Decimal("0.000008")


def test_new_candle_cancels_previous_before_replacement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.order_ids = ["old-one", "old-two"]
    instance.state.orders_placed_candle = "2026-08-29T11:59:00+00:00"
    client.open_orders.return_value = [{"orderId": "old-one"}, {"orderId": "old-two"}]
    events = []
    client.cancel_orders.side_effect = lambda *_: events.append("cancel")
    client.submit_limit_order.side_effect = lambda **_: events.append("submit") or f"new-{len(events)}"
    monkeypatch.setattr(instance, "_bands", lambda *_: events.append("calculate") or tuple(map(Decimal, ("0.000011", "0.000012", "0.000009", "0.000008"))))
    instance.reconcile_once(180)
    assert events == ["calculate", "cancel", "submit", "submit"]


def test_fill_cancels_sibling_and_places_no_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.order_ids = ["filled", "sibling"]
    client.open_orders.return_value = [{"orderId": "sibling"}]
    client.positions.return_value = [{"holdVol": 5, "positionType": 1}]
    action = instance.reconcile_once(180)
    assert "canceled sibling" in action
    client.cancel_orders.assert_called_once_with("SHIB_USDT", ["sibling"])
    client.submit_limit_order.assert_not_called()


def test_second_submit_failure_cancels_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    client.submit_limit_order.side_effect = ["one", RuntimeError("failed")]
    with pytest.raises(RuntimeError, match="failed"):
        instance.reconcile_once(180)
    client.cancel_orders.assert_called_once_with("SHIB_USDT", ["one"])


def test_pair_expires_after_ten_wall_clock_minutes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.order_ids = ["one", "two"]
    instance.state.orders_placed_at = (datetime.now(timezone.utc) - timedelta(minutes=10, seconds=1)).isoformat()
    client.open_orders.return_value = [{"orderId": "one"}, {"orderId": "two"}]
    action = instance.reconcile_once(180, order_lifetime_minutes=10)
    assert "after 10 minutes" in action
    client.cancel_orders.assert_called_once_with("SHIB_USDT", ["one", "two"])
    client.submit_limit_order.assert_not_called()


def test_stop_prices_use_actual_deposit_and_leverage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.leverage = 2
    client.contract.return_value["priceUnit"] = "0.000000001"
    instance.reconcile_once(180)
    long_order, short_order = client.submit_limit_order.call_args_list
    assert long_order.kwargs["stop_loss_price"] == Decimal("0.000010978")
    assert short_order.kwargs["stop_loss_price"] == Decimal("0.000009018")
