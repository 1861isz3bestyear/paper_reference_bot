# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for bybit demo bot test bybit demo."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from email.message import Message
from functools import partial
from io import BytesIO
from typing import TYPE_CHECKING, Self
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pandas as pd
import pytest

from bybit_bot import cli as mainnet_cli
from bybit_demo_bot import cli, executor
from bybit_demo_bot import client as client_module
from bybit_demo_bot import state as execution_state
from bybit_demo_bot.cli import BybitDemoBot, DemoState
from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from reference_bot.config import PaperBotConfig
from shared.testing_support import ignoring_arguments
from shared.validation import json_object
from tests.mock_support import call_kwargs_at, mock_child

if TYPE_CHECKING:
    from pathlib import Path
    from urllib.request import Request


LAST_PRICE = 100
WALLET_BALANCE = 50
EXPECTED_ENTRY_COUNT = 2


class Response:
    """Represent response."""

    def __init__(self, payload: object) -> None:
        """Initialize the test fixture."""
        self.payload: object = payload

    def __enter__(self) -> Self:
        """Enter the response context.

        Returns:
            The enter result.

        """
        return self

    def __exit__(self, *_args: object) -> None:
        """Release the response context."""
        return

    def read(self) -> bytes:
        """Read.

        Returns:
            The read result.

        """
        return json.dumps(self.payload).encode()


def config(**changes: object) -> PaperBotConfig:
    """Build a configuration with explicit test overrides.

    Returns:
        A configuration containing the requested changes.

    """
    defaults = PaperBotConfig(
        strategy_mode="VWAP band mean reversion",
        timeframe="1m",
        trend=True,
        open_order_vwap_sigma=1,
        close_order_vwap_sigma=2,
        initial_capital=10,
        stop_loss_pct=0.4,
        allow_immediate_reentry=True,
        vwap_anchor_reset_weeks=2,
        data_source="Bybit REST",
        ticker="BTC_USDT",
        minimum_order_size=0.01,
    )
    return replace(defaults, **changes)


def completed_candles(
    _symbol: str, _interval: str, *, start_ms: int, end_ms: int
) -> pd.DataFrame:
    """Return the latest completed candle for the requested range.

    Returns:
        A single completed observation before the request cutoff.

    """
    assert start_ms <= end_ms
    return pd.DataFrame([{
        "time": pd.Timestamp(
            end_ms // 60000 * 60000 - 60000, unit="ms", tz="UTC"
        )
    }])


def test_client_public_and_private_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client public and private helpers."""
    payloads = iter(
        list[dict[str, object]]([
            {
                "retCode": 0,
                "result": {"list": [{"lotSizeFilter": {"qtyStep": "0.001"}}]},
            },
            {"retCode": 0, "result": {"list": [{"lastPrice": "100"}]}},
            {
                "retCode": 0,
                "result": {
                    "list": [
                        {"coin": [{"coin": "USDT", "walletBalance": "50"}]}
                    ]
                },
            },
            {"retCode": 0, "result": {"list": [{"side": "Buy", "size": "1"}]}},
            {"retCode": 0, "result": {"orderId": "abc"}},
            {"retCode": 0, "result": {}},
        ])
    )
    requests: list[Request] = []

    def open_(request: Request, timeout: float) -> Response:
        """Open.

        Returns:
            The open result.

        """
        del timeout
        requests.append(request)
        return Response(next(payloads))

    monkeypatch.setattr(client_module, "open_http", open_)
    client = BybitDemoClient("key", "secret")
    assert client.instruments("BTCUSDT")["lotSizeFilter"]
    assert client.last_price("BTCUSDT") == LAST_PRICE
    assert client.available_usdt() == WALLET_BALANCE
    position = client.position("BTCUSDT")
    assert position is not None
    assert position["side"] == "Buy"
    assert client.market_order("BTCUSDT", "Buy", Decimal(".01")) == "abc"
    client.set_protection("BTCUSDT", Decimal(99), Decimal(110))
    assert requests[-1].full_url.startswith(
        "https://api-demo.bybit.com/v5/position/trading-stop"
    )
    assert requests[-1].headers["X-bapi-sign"]
    assert isinstance(requests[-1].data, bytes)
    assert json_object(requests[-1].data.decode()) == {
        "category": "linear",
        "symbol": "BTCUSDT",
        "positionIdx": 0,
        "tpslMode": "Full",
        "stopLoss": "99",
        "takeProfit": "110",
        "slOrderType": "Market",
        "tpOrderType": "Market",
    }


def test_client_rejects_api_errors_and_missing_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client rejects api errors and missing rows."""
    monkeypatch.setattr(
        client_module,
        "open_http",
        ignoring_arguments(
            lambda: Response({"retCode": 10001, "retMsg": "bad"})
        ),
    )
    with pytest.raises(BybitDemoError, match="bad"):
        _ = BybitDemoClient("k", "s").last_price("BTCUSDT")
    monkeypatch.setattr(
        client_module,
        "open_http",
        ignoring_arguments(
            lambda: Response({"retCode": 0, "result": {"list": []}})
        ),
    )
    with pytest.raises(BybitDemoError, match="no instrument"):
        _ = BybitDemoClient("k", "s").instruments("BTCUSDT")


def test_client_accepts_idempotent_protection_not_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client accepts idempotent protection not modified."""
    monkeypatch.setattr(
        client_module,
        "open_http",
        ignoring_arguments(
            lambda: Response(
                {"retCode": 34040, "retMsg": "not modified", "result": {}}
            )
        ),
    )
    client = BybitDemoClient("k", "s")
    client.set_protection("XRPUSDT", Decimal("0.50"), Decimal("0.60"))


def test_client_still_rejects_real_protection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client still rejects real protection errors."""
    monkeypatch.setattr(
        client_module,
        "open_http",
        ignoring_arguments(
            lambda: Response({
                "retCode": 10001,
                "retMsg": "invalid take profit",
                "result": {},
            })
        ),
    )
    with pytest.raises(BybitDemoError, match="invalid take profit"):
        BybitDemoClient("k", "s").set_protection(
            "XRPUSDT", Decimal("0.50"), Decimal("0.60")
        )


def test_bot_validates_config_and_sizes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify bot validates config and sizes."""
    monkeypatch.setattr(
        "bybit_demo_bot.state.STATE_FILE", tmp_path / "state.json"
    )
    client = Mock()
    mock_child(client, "instruments").return_value = {
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}
    }
    mock_child(client, "available_usdt").return_value = Decimal(100)
    bot = BybitDemoBot(config(), client, DemoState.load_or_create(resume=False))
    assert bot.order_quantity(Decimal(1000)) == Decimal("0.090")
    with pytest.raises(ValueError, match="requires"):
        _ = BybitDemoBot(config(data_source="MEXC REST"), client, bot.state)
    mock_child(client, "available_usdt").return_value = Decimal(0)
    with pytest.raises(RuntimeError, match="minimum"):
        _ = bot.order_quantity(Decimal(1000))


def test_quantity_obeys_exchange_notional_step_and_available_balance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify quantity obeys exchange notional step and available balance."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    mock_child(client, "instruments").return_value = {
        "lotSizeFilter": {
            "qtyStep": "1",
            "minOrderQty": "1",
            "minNotionalValue": "5",
        }
    }
    mock_child(client, "available_usdt").return_value = Decimal(8)
    bot = BybitDemoBot(
        config(ticker="XRP_USDT", initial_capital=1),
        client,
        DemoState.load_or_create(resume=False),
    )
    assert bot.order_quantity(Decimal(2)) == Decimal(3)
    mock_child(client, "available_usdt").return_value = Decimal(6)
    with pytest.raises(RuntimeError, match="minimum"):
        _ = bot.order_quantity(Decimal(2))


def test_reconcile_opens_then_protects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify reconcile opens then protects."""
    monkeypatch.setattr(
        "bybit_demo_bot.state.STATE_FILE", tmp_path / "state.json"
    )
    state = DemoState("2026-01-01T00:00:00+00:00")
    client = Mock()
    mock_child(client, "position").side_effect = [
        None,
        {"side": "Buy", "size": "0.01", "avgPrice": "100"},
    ]
    mock_child(client, "last_price").return_value = Decimal(100)
    mock_child(client, "available_usdt").return_value = Decimal(100)
    mock_child(client, "instruments").return_value = {
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}
    }
    candle = pd.DataFrame([
        {"time": pd.Timestamp("2026-01-01T00:00:00Z")},
        {"time": pd.Timestamp("2026-01-01T00:01:00Z")},
    ])
    monkeypatch.setattr(
        "bybit_demo_bot.executor.fetch_completed_linear_klines",
        Mock(return_value=candle),
    )
    monkeypatch.setattr(
        "bybit_demo_bot.executor.calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side="Long")),
    )
    monkeypatch.setattr(
        BybitDemoBot, "exit_band", ignoring_arguments(lambda: Decimal(110))
    )
    bot = BybitDemoBot(
        config(anchor_before_strategy_start=True, anchor_before_days=1),
        client,
        state,
    )
    now = datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC)
    assert "opened Buy" in (bot.reconcile_once(now) or "")
    assert state.pending_protection_side == "Buy"
    assert state.pending_take_profit == "110"
    assert "protected" in (bot.reconcile_once(now) or "")
    mock_child(client, "set_protection").assert_called_once_with(
        "BTCUSDT", Decimal("99.600"), Decimal(110)
    )


def testexit_band_matches_configured_strategy_close_sigma(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify exit band matches configured strategy close sigma."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    rows = pd.DataFrame([{"anchored_vwap": 100.0, "anchored_std": 5.0}])
    monkeypatch.setattr(
        executor,
        "add_launch_weekly_anchored_vwap",
        ignoring_arguments(lambda: rows),
    )
    launched = pd.Timestamp("2026-01-01T00:00:00Z")
    bot = BybitDemoBot(
        config(close_order_vwap_sigma=2),
        Mock(),
        DemoState.load_or_create(resume=False),
    )
    assert bot.exit_band(pd.DataFrame([{}]), launched, "Buy") == Decimal(
        "110.0"
    )
    assert bot.exit_band(pd.DataFrame([{}]), launched, "Sell") == Decimal(
        "90.0"
    )


def test_existing_position_refreshes_tp_to_latest_close_band(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify existing position refreshes tp to latest close band."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    mock_child(client, "position").return_value = {
        "side": "Buy",
        "size": "1",
        "avgPrice": "100",
    }
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        executor, "fetch_completed_linear_klines", Mock(return_value=candles)
    )
    monkeypatch.setattr(
        executor,
        "calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side="Long")),
    )
    monkeypatch.setattr(
        BybitDemoBot, "exit_band", ignoring_arguments(lambda: Decimal(112))
    )
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))
    result = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))
    assert result == "updated Buy protection to current VWAP close band"
    mock_child(client, "set_protection").assert_called_once_with(
        "BTCUSDT", Decimal("99.600"), Decimal(112)
    )


def test_existing_position_keeps_protection_when_new_band_is_not_profitable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Preserve protection when the current band is unprofitable."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "1",
        "avgPrice": "100",
    }
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        executor, "fetch_completed_linear_klines", Mock(return_value=candles)
    )
    monkeypatch.setattr(
        executor,
        "calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side="Short")),
    )
    monkeypatch.setattr(
        BybitDemoBot,
        "exit_band",
        ignoring_arguments(lambda: Decimal("100.01")),
    )
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))
    result = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))
    assert result is not None
    assert result.startswith("kept existing Sell protection")
    mock_child(client, "set_protection").assert_not_called()


def test_skips_entry_when_target_is_already_crossed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify skips entry when target is already crossed."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client = Mock(
        position=Mock(return_value=None),
        last_price=Mock(return_value=Decimal(100)),
    )
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        executor, "fetch_completed_linear_klines", Mock(return_value=candles)
    )
    monkeypatch.setattr(
        executor,
        "calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side="Short")),
    )
    monkeypatch.setattr(
        BybitDemoBot,
        "exit_band",
        ignoring_arguments(lambda: Decimal("100.01")),
    )
    state = DemoState("2026-01-01T00:00:00+00:00")
    result = BybitDemoBot(config(), client, state).reconcile_once(
        datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC)
    )
    assert result is not None
    assert result.startswith("skipped Sell entry")
    assert state.last_processed_candle == "2026-01-01T00:01:00+00:00"
    mock_child(client, "market_order").assert_not_called()


def test_reconcile_closes_opposite_position(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify reconcile closes opposite position."""
    monkeypatch.setattr(
        "bybit_demo_bot.state.STATE_FILE", tmp_path / "state.json"
    )
    client = Mock()
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "0.02",
        "avgPrice": "100",
    }
    candle = pd.DataFrame([
        {"time": pd.Timestamp("2026-01-01T00:00:00Z")},
        {"time": pd.Timestamp("2026-01-01T00:01:00Z")},
    ])
    monkeypatch.setattr(
        "bybit_demo_bot.executor.fetch_completed_linear_klines",
        Mock(return_value=candle),
    )
    monkeypatch.setattr(
        "bybit_demo_bot.executor.calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side=None)),
    )
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))
    assert (
        bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 10, tzinfo=UTC))
        == "closed Sell"
    )
    mock_child(client, "market_order").assert_called_once_with(
        "BTCUSDT", "Buy", Decimal("0.02"), reduce_only=True
    )


def test_reconcile_reverses_position_with_reduce_only_close_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify reconcile reverses position with reduce only close first."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "2",
        "avgPrice": "2",
    }
    mock_child(client, "last_price").return_value = Decimal(2)
    mock_child(client, "available_usdt").return_value = Decimal(100)
    mock_child(client, "instruments").return_value = {
        "lotSizeFilter": {
            "qtyStep": "1",
            "minOrderQty": "1",
            "minNotionalValue": "5",
        }
    }
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        executor, "fetch_completed_linear_klines", Mock(return_value=candles)
    )
    monkeypatch.setattr(
        executor,
        "calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side="Long")),
    )
    monkeypatch.setattr(
        BybitDemoBot, "exit_band", ignoring_arguments(lambda: Decimal(3))
    )
    bot = BybitDemoBot(
        config(ticker="XRP_USDT"),
        client,
        DemoState("2026-01-01T00:00:00+00:00"),
    )
    result = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))
    assert result == "closed Sell; opened Buy; awaiting fill for protection"
    assert call_kwargs_at(mock_child(client, "market_order"), 0) == {
        "reduce_only": True
    }
    assert call_kwargs_at(mock_child(client, "market_order"), 1) == {}


def test_state_resume_stop_and_same_candle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify state resume stop and same candle."""
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(execution_state, "STATE_FILE", state_path)
    created = DemoState.load_or_create(resume=False)
    resumed = DemoState.load_or_create(resume=True)
    assert resumed.launched_at == created.launched_at
    client = Mock()
    mock_child(client, "position").return_value = None
    bot = BybitDemoBot(config(), client, resumed)
    bot.stop()
    assert bot.running is False
    latest = pd.Timestamp("2026-01-01T00:01:00Z")
    bot.state.last_processed_candle = latest.isoformat()
    assert (
        bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC)) is None
    )
    mock_child(client, "position").assert_not_called()


def test_reconcile_fetches_only_incremental_candles_after_initial_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fetch only incremental candles after loading initial history."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    requests: list[tuple[int, int]] = []

    def fetch(
        _symbol: str, _timeframe: str, start_ms: int, end_ms: int
    ) -> pd.DataFrame:
        """Fetch.

        Returns:
            The validated candle observations.

        """
        requests.append((start_ms, end_ms))
        latest_ms = end_ms // 60_000 * 60_000 - 60_000
        return pd.DataFrame(
            [{"time": pd.Timestamp(latest_ms, unit="ms", tz="UTC")}]
        )

    monkeypatch.setattr(executor, "fetch_completed_linear_klines", fetch)
    monkeypatch.setattr(
        executor,
        "calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side=None)),
    )
    client = Mock(position=Mock(return_value=None))
    state = DemoState("2026-01-01T00:00:00+00:00")
    bot = BybitDemoBot(config(), client, state)

    _ = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))
    _ = bot.reconcile_once(datetime(2026, 1, 1, 0, 3, 30, tzinfo=UTC))

    assert requests[0][0] == int(
        pd.Timestamp(state.launched_at).timestamp() * 1000
    )
    assert requests[1][0] == int(
        pd.Timestamp("2026-01-01T00:00:00Z").timestamp() * 1000
    )
    assert mock_child(client, "position").call_count == EXPECTED_ENTRY_COUNT


def test_protection_failure_emergency_closes_and_halts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify protection failure emergency closes and halts."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    position = {"side": "Buy", "size": "0.01", "avgPrice": "100"}
    client = Mock()
    mock_child(client, "position").return_value = position
    mock_child(client, "set_protection").side_effect = BybitDemoError(
        "no protection"
    )
    state = DemoState(
        "2026-01-01T00:00:00+00:00",
        pending_protection_side="Buy",
        pending_take_profit="110",
    )
    bot = BybitDemoBot(config(), client, state)
    with pytest.raises(RuntimeError, match="emergency close submitted"):
        _ = bot.reconcile_once()
    mock_child(client, "market_order").assert_called_once_with(
        "BTCUSDT", "Sell", Decimal("0.01"), reduce_only=True
    )
    with pytest.raises(RuntimeError, match="trading halted"):
        _ = bot.reconcile_once()


def test_unprofitable_fill_emergency_closes_without_permanent_halt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify unprofitable fill emergency closes without permanent halt."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    position = {"side": "Sell", "size": "7.5", "avgPrice": "1.3197"}
    client = Mock(position=Mock(return_value=position))
    state = DemoState(
        "2026-01-01T00:00:00+00:00",
        last_processed_candle="2026-01-01T00:01:00+00:00",
        pending_protection_side="Sell",
        pending_take_profit="1.319761476",
    )
    result = BybitDemoBot(config(), client, state).reconcile_once()
    assert result is not None
    assert "emergency close submitted; waiting for next candle" in result
    mock_child(client, "market_order").assert_called_once_with(
        "BTCUSDT", "Buy", Decimal("7.5"), reduce_only=True
    )
    assert state.halted_reason is None
    assert state.pending_protection_side is None
    assert state.pending_take_profit is None


def test_unprofitable_fill_halts_when_emergency_close_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify unprofitable fill halts when emergency close fails."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client = Mock(
        position=Mock(
            return_value={"side": "Sell", "size": "1", "avgPrice": "100"}
        ),
        market_order=Mock(side_effect=BybitDemoError("close rejected")),
    )
    state = DemoState(
        "2026-01-01T00:00:00+00:00",
        pending_protection_side="Sell",
        pending_take_profit="101",
    )
    with pytest.raises(RuntimeError, match="emergency close failed"):
        _ = BybitDemoBot(config(), client, state).reconcile_once()
    assert state.halted_reason
    assert "invalid entry protection" in state.halted_reason


def test_protection_and_emergency_close_failure_persists_halt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify protection and emergency close failure persists halt."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    mock_child(client, "position").return_value = {
        "side": "Buy",
        "size": "1",
        "avgPrice": "100",
    }
    mock_child(client, "set_protection").side_effect = BybitDemoError(
        "protection rejected"
    )
    mock_child(client, "market_order").side_effect = BybitDemoError(
        "close rejected"
    )
    state = DemoState(
        "2026-01-01T00:00:00+00:00",
        pending_protection_side="Buy",
        pending_take_profit="110",
    )
    with pytest.raises(RuntimeError, match="emergency close failed"):
        _ = BybitDemoBot(config(), client, state).reconcile_once()
    assert "emergency close failed" in (state.halted_reason or "")
    assert (
        json.loads((tmp_path / "state.json").read_text())["halted_reason"]
        == state.halted_reason
    )


def test_stale_history_and_invalid_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify stale history and invalid state."""
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(execution_state, "STATE_FILE", state_path)
    _ = state_path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cannot read"):
        _ = DemoState.load_or_create(resume=True)
    bot = BybitDemoBot(
        config(),
        Mock(position=Mock(return_value=None)),
        DemoState("2026-01-01T00:00:00+00:00"),
    )
    monkeypatch.setattr(
        executor,
        "fetch_completed_linear_klines",
        Mock(return_value=pd.DataFrame()),
    )
    with pytest.raises(RuntimeError, match="not current"):
        _ = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))


def test_run_demo_wires_env_config_and_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify run demo wires env config and lock."""
    monkeypatch.setattr(cli, "archive_while_running", nullcontext)
    config_path = tmp_path / "config.json"
    _ = config_path.write_text(config().to_json(), encoding="utf-8")
    env_path = tmp_path / "bybitapi.env"
    _ = env_path.write_text(
        "BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    run = Mock()
    monkeypatch.setattr(cli.BybitDemoBot, "run", run)
    cli.run_demo_command(config_path, env_path, poll_seconds=3)
    run.assert_called_once_with(2)
    with pytest.raises(ValueError, match="positive"):
        cli.run_demo_command(config_path, env_path, poll_seconds=0)


@pytest.mark.parametrize(
    ("key_name", "secret_name"),
    [("BYBIT_API_KEY", "BYBIT_API_SECRET"), ("API_KEY", "SECRET_API_KEY")],
)
def test_demo_default_env_name_and_credentials_are_wired_without_network(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    key_name: str,
    secret_name: str,
) -> None:
    """Load demo credentials without accessing the network."""
    monkeypatch.setattr(cli, "archive_while_running", nullcontext)
    assert cli.ENV_FILE.name in {"bybitdemoapi.env", "bybitapidemo.env"}
    config_path = tmp_path / "config.json"
    _ = config_path.write_text(config().to_json(), encoding="utf-8")
    env_path = tmp_path / "bybitapidemo.env"
    _ = env_path.write_text(
        f"# demo only\n{key_name}='demo-key'\n{secret_name}=demo-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client_factory = Mock(return_value=Mock())
    monkeypatch.setattr(cli, "BybitDemoClient", client_factory)
    monkeypatch.setattr(cli.BybitDemoBot, "run", Mock())
    cli.run_demo_command(config_path, env_path)
    client_factory.assert_called_once_with("demo-key", "demo-secret")


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("BYBIT_API_SECRET=s\n", "BYBIT_API_KEY"),
        ("BYBIT_API_KEY=k\n", "BYBIT_API_SECRET"),
        ("BYBIT_API_KEY=\nBYBIT_API_SECRET=s\n", "BYBIT_API_KEY"),
    ],
)
def test_demo_rejects_missing_or_blank_credentials(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    """Verify demo rejects missing or blank credentials."""
    config_path = tmp_path / "config.json"
    _ = config_path.write_text(config().to_json(), encoding="utf-8")
    env_path = tmp_path / "bybitapidemo.env"
    _ = env_path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "lock")
    with pytest.raises(ValueError, match=message):
        cli.run_demo_command(config_path, env_path)


def test_client_missing_ticker_position_and_order_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client missing ticker position and order id."""
    payloads = iter(
        list[dict[str, object]]([
            {"retCode": 0, "result": {"list": []}},
            {"retCode": 0, "result": {"list": []}},
            {"retCode": 0, "result": {}},
        ])
    )
    monkeypatch.setattr(
        client_module,
        "open_http",
        ignoring_arguments(lambda: Response(next(payloads))),
    )
    client = BybitDemoClient("k", "s")
    with pytest.raises(BybitDemoError, match="no ticker"):
        _ = client.last_price("BTCUSDT")
    assert client.position("BTCUSDT") is None
    with pytest.raises(BybitDemoError, match="no order ID"):
        _ = client.market_order("BTCUSDT", "Buy", Decimal(1))


def test_client_wraps_transport_and_invalid_json_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client wraps transport and invalid json errors."""
    monkeypatch.setattr(
        client_module, "open_http", Mock(side_effect=URLError("offline"))
    )
    with pytest.raises(BybitDemoError, match="request failed"):
        _ = BybitDemoClient("k", "s").last_price("BTCUSDT")

    bad = Mock()
    bad.__enter__ = Mock(return_value=bad)
    bad.__exit__ = Mock(return_value=None)
    mock_child(bad, "read").return_value = b"not-json"
    monkeypatch.setattr(client_module, "open_http", Mock(return_value=bad))
    with pytest.raises(BybitDemoError, match="request failed"):
        _ = BybitDemoClient("k", "s").last_price("BTCUSDT")


def test_client_never_targets_mainnet_and_signs_post_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client never targets mainnet and signs post body."""
    captured: list[Request] = []

    def capture_request(request: Request, *, timeout: float) -> Response:
        """Record the signed outgoing HTTP request.

        Returns:
            The capture request result.

        """
        del timeout
        captured.append(request)
        return Response({"retCode": 0, "result": {"orderId": "id"}})

    monkeypatch.setattr(client_module, "epoch_seconds", lambda: 1234.567)
    monkeypatch.setattr(
        client_module,
        "open_http",
        capture_request,
    )
    _ = BybitDemoClient("demo-key", "demo-secret").market_order(
        "XRPUSDT", "Buy", Decimal(2)
    )
    request = captured[0]
    assert request.full_url == "https://api-demo.bybit.com/v5/order/create"
    assert request.full_url != "https://api.bybit.com/v5/order/create"
    assert request.headers["X-bapi-api-key"] == "demo-key"
    assert request.headers["X-bapi-sign"]


@pytest.mark.parametrize(
    ("side", "opposite"), [("Long", "Short"), ("Short", "Long")]
)
@pytest.mark.parametrize("next_signal", ["flat", "opposite"])
def test_exchange_exit_blocks_reentry_across_restart(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    side: str,
    opposite: str,
    next_signal: str,
) -> None:
    """Verify exchange exit blocks reentry across restart."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    mock_child(client, "position").return_value = None
    mock_child(client, "last_price").return_value = Decimal(100)
    signal_side: list[str | None] = [side]
    monkeypatch.setattr(
        executor,
        "calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side=signal_side[0])),
    )

    monkeypatch.setattr(
        executor, "fetch_completed_linear_klines", completed_candles
    )
    monkeypatch.setattr(
        BybitDemoBot, "order_quantity", ignoring_arguments(lambda: Decimal(1))
    )

    def exit_band(
        _bot: BybitDemoBot,
        _candles: pd.DataFrame,
        _launched: pd.Timestamp,
        direction: str,
    ) -> Decimal:
        """Choose a profitable exit band for the simulated direction.

        Returns:
            The calculated decimal amount.

        """
        return Decimal("110" if direction == "Buy" else "90")

    monkeypatch.setattr(
        BybitDemoBot,
        "exit_band",
        exit_band,
    )
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))

    def now(minute: int) -> datetime:
        return datetime(2026, 1, 1, 0, minute, 30, tzinfo=UTC)

    assert "opened" in (bot.reconcile_once(now(2)) or "")
    direction = "Buy" if side == "Long" else "Sell"
    mock_child(client, "position").return_value = {
        "side": direction,
        "size": "1",
        "avgPrice": "100",
    }
    assert "protected" in (bot.reconcile_once(now(2)) or "")
    mock_child(client, "position").return_value = (
        None  # Exchange-side stop or take-profit filled.
    )
    bot = BybitDemoBot(config(), client, DemoState.load_or_create(resume=True))
    assert "re-entry" in (bot.reconcile_once(now(3)) or "")
    assert "re-entry" in (bot.reconcile_once(now(4)) or "")
    assert mock_child(client, "market_order").call_count == 1
    signal_side[0] = None if next_signal == "flat" else opposite
    result = bot.reconcile_once(now(5))
    if next_signal == "flat":
        assert result is None
        signal_side[0] = side
        result = bot.reconcile_once(now(6))
    assert result is not None
    assert "opened" in result
    assert mock_child(client, "market_order").call_count == EXPECTED_ENTRY_COUNT


@pytest.mark.parametrize("module_name", ["bybit_demo_bot.cli", "bybit_bot.cli"])
def test_legacy_state_conservatively_consumes_current_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module_name: str
) -> None:
    """Verify legacy state conservatively consumes current signal."""
    module = cli if module_name == "bybit_demo_bot.cli" else mainnet_cli
    path = tmp_path / "state.json"
    monkeypatch.setattr(
        execution_state if module is cli else mainnet_cli, "STATE_FILE", path
    )
    _ = path.write_text(
        json.dumps({
            "launched_at": "2026-01-01T00:00:00+00:00",
            "last_processed_candle": "2026-01-01T00:01:00+00:00",
        })
    )
    state_type = DemoState if module is cli else mainnet_cli.BybitState
    state = state_type.load_or_create(resume=True)
    assert state.consumed_signal_side == "legacy"
    client = Mock(position=Mock(return_value=None))
    monkeypatch.setattr(
        executor,
        "calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side="Long")),
    )
    monkeypatch.setattr(
        executor,
        "fetch_completed_linear_klines",
        Mock(
            return_value=pd.DataFrame(
                [{"time": pd.Timestamp("2026-01-01T00:02:00Z")}]
            )
        ),
    )
    bot = BybitDemoBot(config(), client, state)
    assert "re-entry" in (
        bot.reconcile_once(datetime(2026, 1, 1, 0, 3, 30, tzinfo=UTC)) or ""
    )
    mock_child(client, "market_order").assert_not_called()
    assert state_type.load_or_create(resume=True).consumed_signal_side == "Buy"


@pytest.mark.parametrize(("method", "count"), [("GET", 2), ("POST", 1)])
def test_timestamp_retry_only_replays_reads(
    monkeypatch: pytest.MonkeyPatch, method: str, count: int
) -> None:
    """Verify timestamp retry only replays reads."""
    requests: list[Request] = []
    ticks = iter([1000, 1001])
    monkeypatch.setattr(client_module, "epoch_seconds", lambda: next(ticks))

    def respond(request: Request, timeout: float) -> Response:
        """Respond.

        Returns:
            The respond result.

        """
        del timeout
        requests.append(request)
        return Response({"retCode": 10002, "retMsg": "timestamp rejected"})

    monkeypatch.setattr(client_module, "open_http", respond)
    client = BybitDemoClient("k", "s")
    request = (
        partial(client.instruments, "BTCUSDT")
        if method == "GET"
        else partial(client.market_order, "BTCUSDT", "Buy", Decimal(1))
    )
    with pytest.raises(BybitDemoError, match="10002"):
        _ = request()
    assert len(requests) == count
    assert not (
        count == EXPECTED_ENTRY_COUNT
        and requests[0].headers["X-bapi-timestamp"]
        == requests[1].headers["X-bapi-timestamp"]
    )


@pytest.mark.parametrize(
    ("pending", "error", "expected"),
    [
        (True, None, 1),
        (False, None, 10),
        (False, "Too many visits. Exceeded the API Rate Limit.", 60),
    ],
)
def test_loop_wait_for_protection_and_rate_limits(
    *,
    monkeypatch: pytest.MonkeyPatch,
    pending: bool,
    error: str | None,
    expected: int,
) -> None:
    """Verify loop wait for protection and rate limits."""
    bot = BybitDemoBot(config(), Mock(), DemoState("2026-01-01T00:00:00Z"))
    bot.state.pending_protection_side = "Buy" if pending else None
    bot.reconcile_once = (
        Mock(side_effect=BybitDemoError(error))
        if error
        else Mock(return_value=None)
    )
    monkeypatch.setattr("signal.signal", ignoring_arguments(lambda: None))
    ticks = [0.0]
    monkeypatch.setattr(executor, "monotonic", lambda: ticks[0])

    def sleep(seconds: float) -> None:
        """Sleep."""
        ticks[0] += seconds
        if ticks[0] >= expected:
            bot.stop()

    monkeypatch.setattr(executor, "sleep", sleep)
    bot.run(10)
    assert ticks[0] == expected
    bot.reconcile_once.assert_called_once()


def test_exchange_close_logged_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify exchange close logged once."""
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "state.json")
    state = DemoState("2026-01-01T00:00:00Z", observed_position_side="Buy")
    bot = BybitDemoBot(config(), Mock(position=Mock(return_value=None)), state)
    monkeypatch.setattr(
        executor,
        "fetch_completed_linear_klines",
        Mock(
            return_value=pd.DataFrame(
                [{"time": pd.Timestamp("2026-01-01T00:02:00Z")}]
            )
        ),
    )
    monkeypatch.setattr(
        executor,
        "calculate_strategy_decision",
        ignoring_arguments(lambda: Mock(side=None)),
    )
    now = datetime(2026, 1, 1, 0, 3, 30, tzinfo=UTC)
    _ = bot.reconcile_once(now)
    _ = bot.reconcile_once(now)
    assert capsys.readouterr().out.count("position closed on exchange") == 1
    assert (
        json.loads(execution_state.STATE_FILE.read_text())[
            "observed_position_side"
        ]
        is None
    )


def test_client_closes_http_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed order requests must release their response without a retry."""
    body = BytesIO(b"service unavailable")
    error = HTTPError(
        "https://example.test", 503, "unavailable", Message(), body
    )
    opener = Mock(side_effect=error)
    monkeypatch.setattr(client_module, "open_http", opener)
    client = BybitDemoClient("key", "secret")
    with pytest.raises(BybitDemoError, match="HTTP 503: service unavailable"):
        _ = client.market_order("BTCUSDT", "Buy", Decimal(1))
    assert body.closed
    opener.assert_called_once()


@pytest.mark.parametrize("base_url", ["file:///tmp/exchange", "ftp://exchange"])
def test_client_rejects_non_http_exchange_urls(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject unsupported URL schemes before opening a transport."""
    transport = Mock()
    monkeypatch.setattr(client_module, "open_http", transport)
    client = BybitDemoClient("key", "secret", base_url=base_url)
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        _ = client.last_price("BTCUSDT")
    transport.assert_not_called()
