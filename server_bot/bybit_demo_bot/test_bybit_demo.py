# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for bybit demo bot test bybit demo."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Self
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pandas as pd
import pytest

from bybit_demo_bot import cli
from bybit_demo_bot import client as client_module
from bybit_demo_bot.cli import BybitDemoBot, DemoState
from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from reference_bot.config import PaperBotConfig


class Response:
    """Represent response."""

    def __init__(self, payload: object) -> None:
        """Initialize the test fixture."""
        self.payload = payload

    def __enter__(self) -> Self:
        """Enter the response context."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Release the response context."""
        return

    def read(self) -> bytes:
        """Read."""
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


def test_client_public_and_private_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client public and private helpers."""
    payloads = iter([
        {
            "retCode": 0,
            "result": {"list": [{"lotSizeFilter": {"qtyStep": "0.001"}}]},
        },
        {"retCode": 0, "result": {"list": [{"lastPrice": "100"}]}},
        {
            "retCode": 0,
            "result": {
                "list": [{"coin": [{"coin": "USDT", "walletBalance": "50"}]}]
            },
        },
        {"retCode": 0, "result": {"list": [{"side": "Buy", "size": "1"}]}},
        {"retCode": 0, "result": {"orderId": "abc"}},
        {"retCode": 0, "result": {}},
    ])
    requests = []

    def open_(request, timeout):
        """Open."""
        requests.append(request)
        return Response(next(payloads))

    monkeypatch.setattr(client_module, "urlopen", open_)
    client = BybitDemoClient("key", "secret")
    if not (client.instruments("BTCUSDT")["lotSizeFilter"]):
        pytest.fail("Expected client.instruments('BTCUSDT')['lotSizeFilter']")
    if client.last_price("BTCUSDT") != 100:
        pytest.fail("Expected client.last_price('BTCUSDT') == 100")
    if client.available_usdt() != 50:
        pytest.fail("Expected client.available_usdt() == 50")
    if client.position("BTCUSDT")["side"] != "Buy":
        pytest.fail("Expected client.position('BTCUSDT')['side'] == 'Buy'")
    if client.market_order("BTCUSDT", "Buy", Decimal(".01")) != "abc":
        pytest.fail(
            "Expected client.market_order('BTCUSDT', 'Buy', Decimal('.01')) =="
            " 'abc'"
        )
    client.set_protection("BTCUSDT", Decimal(99), Decimal(110))
    if not (
        requests[-1].full_url.startswith(
            "https://api-demo.bybit.com/v5/position/trading-stop"
        )
    ):
        pytest.fail(
            "Expected"
            " requests[-1].full_url.startswith('https://api-demo.bybit.com/v5/position/trading-stop')"
        )
    if not (requests[-1].headers["X-bapi-sign"]):
        pytest.fail("Expected requests[-1].headers['X-bapi-sign']")
    if json.loads(requests[-1].data) != {
        "category": "linear",
        "symbol": "BTCUSDT",
        "positionIdx": 0,
        "tpslMode": "Full",
        "stopLoss": "99",
        "takeProfit": "110",
        "slOrderType": "Market",
        "tpOrderType": "Market",
    }:
        pytest.fail(
            "Expected json.loads(requests[-1].data) == {'category': 'linear',"
            " 'symbol': 'BTCUSDT', 'positionIdx': 0, 'tpslMode': 'Full',"
            " 'stopLoss': '99', 'takeProfit': '110', 'slOrderType': 'Market',"
            " 'tpOrderType': 'Market'}"
        )


def test_client_rejects_api_errors_and_missing_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client rejects api errors and missing rows."""
    monkeypatch.setattr(
        client_module,
        "urlopen",
        lambda *_a, **_k: Response({"retCode": 10001, "retMsg": "bad"}),
    )
    with pytest.raises(BybitDemoError, match="bad"):
        _ = BybitDemoClient("k", "s").last_price("BTCUSDT")
    monkeypatch.setattr(
        client_module,
        "urlopen",
        lambda *_a, **_k: Response({"retCode": 0, "result": {"list": []}}),
    )
    with pytest.raises(BybitDemoError, match="no instrument"):
        _ = BybitDemoClient("k", "s").instruments("BTCUSDT")


def test_client_accepts_idempotent_protection_not_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client accepts idempotent protection not modified."""
    monkeypatch.setattr(
        client_module,
        "urlopen",
        lambda *_a, **_k: Response({
            "retCode": 34040,
            "retMsg": "not modified",
            "result": {},
        }),
    )
    client = BybitDemoClient("k", "s")
    client.set_protection("XRPUSDT", Decimal("0.50"), Decimal("0.60"))


def test_client_still_rejects_real_protection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client still rejects real protection errors."""
    monkeypatch.setattr(
        client_module,
        "urlopen",
        lambda *_a, **_k: Response({
            "retCode": 10001,
            "retMsg": "invalid take profit",
            "result": {},
        }),
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
        "bybit_demo_bot.cli.STATE_FILE", tmp_path / "state.json"
    )
    client = Mock()
    client.instruments.return_value = {
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}
    }
    client.available_usdt.return_value = Decimal(100)
    bot = BybitDemoBot(config(), client, DemoState.load_or_create(False))
    if bot._quantity(Decimal(1000)) != Decimal("0.090"):
        pytest.fail("Expected bot._quantity(Decimal(1000)) == Decimal('0.090')")
    with pytest.raises(ValueError, match="requires"):
        _ = BybitDemoBot(config(data_source="MEXC REST"), client, bot.state)
    client.available_usdt.return_value = Decimal(0)
    with pytest.raises(RuntimeError, match="minimum"):
        _ = bot._quantity(Decimal(1000))


def test_quantity_obeys_exchange_notional_step_and_available_balance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify quantity obeys exchange notional step and available balance."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.instruments.return_value = {
        "lotSizeFilter": {
            "qtyStep": "1",
            "minOrderQty": "1",
            "minNotionalValue": "5",
        }
    }
    client.available_usdt.return_value = Decimal(8)
    bot = BybitDemoBot(
        config(ticker="XRP_USDT", initial_capital=1),
        client,
        DemoState.load_or_create(False),
    )
    if bot._quantity(Decimal(2)) != Decimal(3):
        pytest.fail(
            "Expected bot._quantity(Decimal(2)) == Decimal(3)"
        )  # 90% of balance, then floor to whole XRP.
    client.available_usdt.return_value = Decimal(6)
    with pytest.raises(RuntimeError, match="minimum"):
        _ = bot._quantity(Decimal(2))


def test_reconcile_opens_then_protects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify reconcile opens then protects."""
    monkeypatch.setattr(
        "bybit_demo_bot.cli.STATE_FILE", tmp_path / "state.json"
    )
    state = DemoState("2026-01-01T00:00:00+00:00")
    client = Mock()
    client.position.side_effect = [
        None,
        {"side": "Buy", "size": "0.01", "avgPrice": "100"},
    ]
    client.last_price.return_value = Decimal(100)
    client.available_usdt.return_value = Decimal(100)
    client.instruments.return_value = {
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}
    }
    candle = pd.DataFrame([
        {"time": pd.Timestamp("2026-01-01T00:00:00Z")},
        {"time": pd.Timestamp("2026-01-01T00:01:00Z")},
    ])
    monkeypatch.setattr(
        "bybit_demo_bot.cli.fetch_completed_linear_klines", lambda *_: candle
    )
    monkeypatch.setattr(
        "bybit_demo_bot.cli.calculate_strategy_decision",
        lambda *_: Mock(side="Long"),
    )
    monkeypatch.setattr(BybitDemoBot, "_exit_band", lambda *_: Decimal(110))
    bot = BybitDemoBot(
        config(anchor_before_strategy_start=True, anchor_before_days=1),
        client,
        state,
    )
    now = datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC)
    if "opened Buy" not in (bot.reconcile_once(now)):
        pytest.fail("Expected 'opened Buy' in bot.reconcile_once(now)")
    if state.pending_protection_side != "Buy":
        pytest.fail("Expected state.pending_protection_side == 'Buy'")
    if state.pending_take_profit != "110":
        pytest.fail("Expected state.pending_take_profit == '110'")
    if "protected" not in (bot.reconcile_once(now)):
        pytest.fail("Expected 'protected' in bot.reconcile_once(now)")
    client.set_protection.assert_called_once_with(
        "BTCUSDT", Decimal("99.600"), Decimal(110)
    )


def test_exit_band_matches_configured_strategy_close_sigma(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify exit band matches configured strategy close sigma."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    rows = pd.DataFrame([{"anchored_vwap": 100.0, "anchored_std": 5.0}])
    monkeypatch.setattr(cli, "add_launch_weekly_anchored_vwap", lambda *_: rows)
    launched = pd.Timestamp("2026-01-01T00:00:00Z")
    bot = BybitDemoBot(
        config(close_order_vwap_sigma=2),
        Mock(),
        DemoState.load_or_create(False),
    )
    if bot._exit_band(pd.DataFrame([{}]), launched, "Buy") != Decimal("110.0"):
        pytest.fail(
            "Expected bot._exit_band(pd.DataFrame([{}]), launched, 'Buy') =="
            " Decimal('110.0')"
        )
    if bot._exit_band(pd.DataFrame([{}]), launched, "Sell") != Decimal("90.0"):
        pytest.fail(
            "Expected bot._exit_band(pd.DataFrame([{}]), launched, 'Sell') =="
            " Decimal('90.0')"
        )


def test_existing_position_refreshes_tp_to_latest_close_band(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify existing position refreshes tp to latest close band."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = {
        "side": "Buy",
        "size": "1",
        "avgPrice": "100",
    }
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        cli, "fetch_completed_linear_klines", lambda *_: candles
    )
    monkeypatch.setattr(
        cli, "calculate_strategy_decision", lambda *_: Mock(side="Long")
    )
    monkeypatch.setattr(BybitDemoBot, "_exit_band", lambda *_: Decimal(112))
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))
    result = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))
    if result != "updated Buy protection to current VWAP close band":
        pytest.fail(
            "Expected result == 'updated Buy protection to current VWAP close"
            " band'"
        )
    client.set_protection.assert_called_once_with(
        "BTCUSDT", Decimal("99.600"), Decimal(112)
    )


def test_existing_position_keeps_protection_when_new_band_is_not_profitable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify existing position keeps protection when new band is not profitable."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = {
        "side": "Sell",
        "size": "1",
        "avgPrice": "100",
    }
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        cli, "fetch_completed_linear_klines", lambda *_: candles
    )
    monkeypatch.setattr(
        cli, "calculate_strategy_decision", lambda *_: Mock(side="Short")
    )
    monkeypatch.setattr(
        BybitDemoBot, "_exit_band", lambda *_: Decimal("100.01")
    )
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))
    result = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))
    if not (result.startswith("kept existing Sell protection")):
        pytest.fail(
            "Expected result.startswith('kept existing Sell protection')"
        )
    client.set_protection.assert_not_called()


def test_skips_entry_when_target_is_already_crossed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify skips entry when target is already crossed."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock(
        position=Mock(return_value=None),
        last_price=Mock(return_value=Decimal(100)),
    )
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        cli, "fetch_completed_linear_klines", lambda *_: candles
    )
    monkeypatch.setattr(
        cli, "calculate_strategy_decision", lambda *_: Mock(side="Short")
    )
    monkeypatch.setattr(
        BybitDemoBot, "_exit_band", lambda *_: Decimal("100.01")
    )
    state = DemoState("2026-01-01T00:00:00+00:00")
    result = BybitDemoBot(config(), client, state).reconcile_once(
        datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC)
    )
    if not (result.startswith("skipped Sell entry")):
        pytest.fail("Expected result.startswith('skipped Sell entry')")
    if state.last_processed_candle != "2026-01-01T00:01:00+00:00":
        pytest.fail(
            "Expected state.last_processed_candle =="
            " '2026-01-01T00:01:00+00:00'"
        )
    client.market_order.assert_not_called()


def test_reconcile_closes_opposite_position(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify reconcile closes opposite position."""
    monkeypatch.setattr(
        "bybit_demo_bot.cli.STATE_FILE", tmp_path / "state.json"
    )
    client = Mock()
    client.position.return_value = {
        "side": "Sell",
        "size": "0.02",
        "avgPrice": "100",
    }
    candle = pd.DataFrame([
        {"time": pd.Timestamp("2026-01-01T00:00:00Z")},
        {"time": pd.Timestamp("2026-01-01T00:01:00Z")},
    ])
    monkeypatch.setattr(
        "bybit_demo_bot.cli.fetch_completed_linear_klines", lambda *_: candle
    )
    monkeypatch.setattr(
        "bybit_demo_bot.cli.calculate_strategy_decision",
        lambda *_: Mock(side=None),
    )
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))
    if (
        bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 10, tzinfo=UTC))
        != "closed Sell"
    ):
        pytest.fail(
            "Expected bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 10,"
            " tzinfo=UTC)) == 'closed Sell'"
        )
    client.market_order.assert_called_once_with(
        "BTCUSDT", "Buy", Decimal("0.02"), reduce_only=True
    )


def test_reconcile_reverses_position_with_reduce_only_close_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify reconcile reverses position with reduce only close first."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = {
        "side": "Sell",
        "size": "2",
        "avgPrice": "2",
    }
    client.last_price.return_value = Decimal(2)
    client.available_usdt.return_value = Decimal(100)
    client.instruments.return_value = {
        "lotSizeFilter": {
            "qtyStep": "1",
            "minOrderQty": "1",
            "minNotionalValue": "5",
        }
    }
    candles = pd.DataFrame([{"time": pd.Timestamp("2026-01-01T00:01:00Z")}])
    monkeypatch.setattr(
        cli, "fetch_completed_linear_klines", lambda *_: candles
    )
    monkeypatch.setattr(
        cli, "calculate_strategy_decision", lambda *_: Mock(side="Long")
    )
    monkeypatch.setattr(BybitDemoBot, "_exit_band", lambda *_: Decimal(3))
    bot = BybitDemoBot(
        config(ticker="XRP_USDT"),
        client,
        DemoState("2026-01-01T00:00:00+00:00"),
    )
    result = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))
    if result != "closed Sell; opened Buy; awaiting fill for protection":
        pytest.fail(
            "Expected result == 'closed Sell; opened Buy; awaiting fill for"
            " protection'"
        )
    if client.market_order.call_args_list[0].kwargs != {"reduce_only": True}:
        pytest.fail(
            "Expected client.market_order.call_args_list[0].kwargs =="
            " {'reduce_only': True}"
        )
    if client.market_order.call_args_list[1].kwargs != {}:
        pytest.fail(
            "Expected client.market_order.call_args_list[1].kwargs == {}"
        )


def test_state_resume_stop_and_same_candle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify state resume stop and same candle."""
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(cli, "STATE_FILE", state_path)
    created = DemoState.load_or_create(False)
    resumed = DemoState.load_or_create(True)
    if resumed.launched_at != created.launched_at:
        pytest.fail("Expected resumed.launched_at == created.launched_at")
    client = Mock()
    client.position.return_value = None
    bot = BybitDemoBot(config(), client, resumed)
    bot.stop()
    if (bot.running) is not (False):
        pytest.fail("Expected bot.running is False")
    latest = pd.Timestamp("2026-01-01T00:01:00Z")
    bot.state.last_processed_candle = latest.isoformat()
    if (bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))) is not (
        None
    ):
        pytest.fail(
            "Expected bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30,"
            " tzinfo=UTC)) is None"
        )
    client.position.assert_not_called()


def test_reconcile_fetches_only_incremental_candles_after_initial_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify reconcile fetches only incremental candles after initial history."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    requests = []

    def fetch(_symbol, _timeframe, start_ms, end_ms):
        """Fetch."""
        requests.append((start_ms, end_ms))
        latest_ms = end_ms // 60_000 * 60_000 - 60_000
        return pd.DataFrame(
            [{"time": pd.Timestamp(latest_ms, unit="ms", tz="UTC")}]
        )

    monkeypatch.setattr(cli, "fetch_completed_linear_klines", fetch)
    monkeypatch.setattr(
        cli, "calculate_strategy_decision", lambda *_: Mock(side=None)
    )
    client = Mock(position=Mock(return_value=None))
    state = DemoState("2026-01-01T00:00:00+00:00")
    bot = BybitDemoBot(config(), client, state)

    _ = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))
    _ = bot.reconcile_once(datetime(2026, 1, 1, 0, 3, 30, tzinfo=UTC))

    if requests[0][0] != int(
        pd.Timestamp(state.launched_at).timestamp() * 1000
    ):
        pytest.fail(
            "Expected requests[0][0] =="
            " int(pd.Timestamp(state.launched_at).timestamp() * 1000)"
        )
    if requests[1][0] != int(
        pd.Timestamp("2026-01-01T00:00:00Z").timestamp() * 1000
    ):
        pytest.fail(
            "Expected requests[1][0] =="
            " int(pd.Timestamp('2026-01-01T00:00:00Z').timestamp() * 1000)"
        )
    if client.position.call_count != 2:
        pytest.fail("Expected client.position.call_count == 2")


def test_protection_failure_emergency_closes_and_halts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify protection failure emergency closes and halts."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    position = {"side": "Buy", "size": "0.01", "avgPrice": "100"}
    client = Mock()
    client.position.return_value = position
    client.set_protection.side_effect = BybitDemoError("no protection")
    state = DemoState(
        "2026-01-01T00:00:00+00:00",
        pending_protection_side="Buy",
        pending_take_profit="110",
    )
    bot = BybitDemoBot(config(), client, state)
    with pytest.raises(RuntimeError, match="emergency close submitted"):
        _ = bot.reconcile_once()
    client.market_order.assert_called_once_with(
        "BTCUSDT", "Sell", Decimal("0.01"), reduce_only=True
    )
    with pytest.raises(RuntimeError, match="trading halted"):
        _ = bot.reconcile_once()


def test_unprofitable_fill_emergency_closes_without_permanent_halt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify unprofitable fill emergency closes without permanent halt."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    position = {"side": "Sell", "size": "7.5", "avgPrice": "1.3197"}
    client = Mock(position=Mock(return_value=position))
    state = DemoState(
        "2026-01-01T00:00:00+00:00",
        last_processed_candle="2026-01-01T00:01:00+00:00",
        pending_protection_side="Sell",
        pending_take_profit="1.319761476",
    )
    result = BybitDemoBot(config(), client, state).reconcile_once()
    if "emergency close submitted; waiting for next candle" not in (result):
        pytest.fail(
            "Expected 'emergency close submitted; waiting for next candle' in"
            " result"
        )
    client.market_order.assert_called_once_with(
        "BTCUSDT", "Buy", Decimal("7.5"), reduce_only=True
    )
    if (state.halted_reason) is not (None):
        pytest.fail("Expected state.halted_reason is None")
    if (state.pending_protection_side) is not (None):
        pytest.fail("Expected state.pending_protection_side is None")
    if (state.pending_take_profit) is not (None):
        pytest.fail("Expected state.pending_take_profit is None")


def test_unprofitable_fill_halts_when_emergency_close_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify unprofitable fill halts when emergency close fails."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
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
    if not (state.halted_reason):
        pytest.fail("Expected state.halted_reason")
    if "invalid entry protection" not in (state.halted_reason):
        pytest.fail(
            "Expected 'invalid entry protection' in state.halted_reason"
        )


def test_protection_and_emergency_close_failure_persists_halt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify protection and emergency close failure persists halt."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = {
        "side": "Buy",
        "size": "1",
        "avgPrice": "100",
    }
    client.set_protection.side_effect = BybitDemoError("protection rejected")
    client.market_order.side_effect = BybitDemoError("close rejected")
    state = DemoState(
        "2026-01-01T00:00:00+00:00",
        pending_protection_side="Buy",
        pending_take_profit="110",
    )
    with pytest.raises(RuntimeError, match="emergency close failed"):
        _ = BybitDemoBot(config(), client, state).reconcile_once()
    if "emergency close failed" not in (state.halted_reason or ""):
        pytest.fail(
            "Expected 'emergency close failed' in (state.halted_reason or '')"
        )
    if (
        json.loads((tmp_path / "state.json").read_text())["halted_reason"]
        != state.halted_reason
    ):
        pytest.fail(
            "Expected json.loads((tmp_path /"
            " 'state.json').read_text())['halted_reason'] =="
            " state.halted_reason"
        )


def test_stale_history_and_invalid_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify stale history and invalid state."""
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(cli, "STATE_FILE", state_path)
    _ = state_path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cannot read"):
        _ = DemoState.load_or_create(True)
    bot = BybitDemoBot(
        config(),
        Mock(position=Mock(return_value=None)),
        DemoState("2026-01-01T00:00:00+00:00"),
    )
    monkeypatch.setattr(
        cli, "fetch_completed_linear_klines", lambda *_: pd.DataFrame()
    )
    with pytest.raises(RuntimeError, match="not current"):
        _ = bot.reconcile_once(datetime(2026, 1, 1, 0, 2, 30, tzinfo=UTC))


def test_run_demo_wires_env_config_and_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify run demo wires env config and lock."""
    config_path = tmp_path / "config.json"
    _ = config_path.write_text(config().to_json(), encoding="utf-8")
    env_path = tmp_path / "bybitapi.env"
    _ = env_path.write_text(
        "BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    run = Mock()
    monkeypatch.setattr(cli.BybitDemoBot, "run", run)
    cli.run_demo_command(config_path, env_path, poll_seconds=3)
    run.assert_called_once_with(2)
    with pytest.raises(ValueError, match="positive"):
        cli.run_demo_command(config_path, env_path, poll_seconds=0)


@pytest.mark.parametrize(
    "key_name, secret_name",
    [("BYBIT_API_KEY", "BYBIT_API_SECRET"), ("API_KEY", "SECRET_API_KEY")],
)
def test_demo_default_env_name_and_credentials_are_wired_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    key_name: str,
    secret_name: str,
) -> None:
    """Verify demo default env name and credentials are wired without network."""
    if (cli.ENV_FILE.name) not in ({"bybitdemoapi.env", "bybitapidemo.env"}):
        pytest.fail(
            "Expected cli.ENV_FILE.name in {'bybitdemoapi.env',"
            " 'bybitapidemo.env'}"
        )
    config_path = tmp_path / "config.json"
    _ = config_path.write_text(config().to_json(), encoding="utf-8")
    env_path = tmp_path / "bybitapidemo.env"
    _ = env_path.write_text(
        f"# demo only\n{key_name}='demo-key'\n{secret_name}=demo-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client_factory = Mock(return_value=Mock())
    monkeypatch.setattr(cli, "BybitDemoClient", client_factory)
    monkeypatch.setattr(cli.BybitDemoBot, "run", Mock())
    cli.run_demo_command(config_path, env_path)
    client_factory.assert_called_once_with("demo-key", "demo-secret")


@pytest.mark.parametrize(
    "contents, message",
    [
        ("BYBIT_API_SECRET=s\n", "BYBIT_API_KEY"),
        ("BYBIT_API_KEY=k\n", "BYBIT_API_SECRET"),
        ("BYBIT_API_KEY=\nBYBIT_API_SECRET=s\n", "BYBIT_API_KEY"),
    ],
)
def test_demo_rejects_missing_or_blank_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents, message
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
    payloads = iter([
        {"retCode": 0, "result": {"list": []}},
        {"retCode": 0, "result": {"list": []}},
        {"retCode": 0, "result": {}},
    ])
    monkeypatch.setattr(
        client_module, "urlopen", lambda *_a, **_k: Response(next(payloads))
    )
    client = BybitDemoClient("k", "s")
    with pytest.raises(BybitDemoError, match="no ticker"):
        _ = client.last_price("BTCUSDT")
    if (client.position("BTCUSDT")) is not (None):
        pytest.fail("Expected client.position('BTCUSDT') is None")
    with pytest.raises(BybitDemoError, match="no order ID"):
        _ = client.market_order("BTCUSDT", "Buy", Decimal(1))


def test_client_wraps_transport_and_invalid_json_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client wraps transport and invalid json errors."""
    monkeypatch.setattr(
        client_module, "urlopen", Mock(side_effect=URLError("offline"))
    )
    with pytest.raises(BybitDemoError, match="request failed"):
        _ = BybitDemoClient("k", "s").last_price("BTCUSDT")

    bad = Mock()
    bad.__enter__ = Mock(return_value=bad)
    bad.__exit__ = Mock(return_value=None)
    bad.read.return_value = b"not-json"
    monkeypatch.setattr(client_module, "urlopen", Mock(return_value=bad))
    with pytest.raises(BybitDemoError, match="request failed"):
        _ = BybitDemoClient("k", "s").last_price("BTCUSDT")


def test_client_never_targets_mainnet_and_signs_post_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client never targets mainnet and signs post body."""
    captured = []
    monkeypatch.setattr(client_module.time, "time", lambda: 1234.567)
    monkeypatch.setattr(
        client_module,
        "urlopen",
        lambda request, timeout: captured.append(request)
        or Response({"retCode": 0, "result": {"orderId": "id"}}),
    )
    _ = BybitDemoClient("demo-key", "demo-secret").market_order(
        "XRPUSDT", "Buy", Decimal(2)
    )
    request = captured[0]
    if request.full_url != "https://api-demo.bybit.com/v5/order/create":
        pytest.fail(
            "Expected request.full_url =="
            " 'https://api-demo.bybit.com/v5/order/create'"
        )
    if request.full_url == "https://api.bybit.com/v5/order/create":
        pytest.fail(
            "Expected request.full_url !="
            " 'https://api.bybit.com/v5/order/create'"
        )
    if request.headers["X-bapi-api-key"] != "demo-key":
        pytest.fail("Expected request.headers['X-bapi-api-key'] == 'demo-key'")
    if not (request.headers["X-bapi-sign"]):
        pytest.fail("Expected request.headers['X-bapi-sign']")


@pytest.mark.parametrize(
    "side,opposite", [("Long", "Short"), ("Short", "Long")]
)
@pytest.mark.parametrize("next_signal", ["flat", "opposite"])
def test_exchange_exit_blocks_reentry_across_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, side, opposite, next_signal
) -> None:
    """Verify exchange exit blocks reentry across restart."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    client = Mock()
    client.position.return_value = None
    client.last_price.return_value = Decimal(100)
    signal_side = [side]
    monkeypatch.setattr(
        cli, "calculate_strategy_decision", lambda *_: Mock(side=signal_side[0])
    )
    monkeypatch.setattr(
        cli,
        "fetch_completed_linear_klines",
        lambda _s, _t, _start, end: pd.DataFrame([{
            "time": pd.Timestamp(
                end // 60000 * 60000 - 60000, unit="ms", tz="UTC"
            )
        }]),
    )
    monkeypatch.setattr(BybitDemoBot, "_quantity", lambda *_: Decimal(1))
    monkeypatch.setattr(
        BybitDemoBot,
        "_exit_band",
        lambda _b, _c, _l, direction: Decimal(
            "110" if direction == "Buy" else "90"
        ),
    )
    bot = BybitDemoBot(config(), client, DemoState("2026-01-01T00:00:00+00:00"))
    now = lambda minute: datetime(2026, 1, 1, 0, minute, 30, tzinfo=UTC)
    if "opened" not in (bot.reconcile_once(now(2))):
        pytest.fail("Expected 'opened' in bot.reconcile_once(now(2))")
    direction = "Buy" if side == "Long" else "Sell"
    client.position.return_value = {
        "side": direction,
        "size": "1",
        "avgPrice": "100",
    }
    if "protected" not in (bot.reconcile_once(now(2))):
        pytest.fail("Expected 'protected' in bot.reconcile_once(now(2))")
    client.position.return_value = (
        None  # Exchange-side stop or take-profit filled.
    )
    bot = BybitDemoBot(config(), client, DemoState.load_or_create(True))
    if "re-entry" not in (bot.reconcile_once(now(3))):
        pytest.fail("Expected 're-entry' in bot.reconcile_once(now(3))")
    if "re-entry" not in (bot.reconcile_once(now(4))):
        pytest.fail("Expected 're-entry' in bot.reconcile_once(now(4))")
    if client.market_order.call_count != 1:
        pytest.fail("Expected client.market_order.call_count == 1")
    signal_side[0] = None if next_signal == "flat" else opposite
    result = bot.reconcile_once(now(5))
    if next_signal == "flat":
        if (result) is not (None):
            pytest.fail("Expected result is None")
        signal_side[0] = side
        result = bot.reconcile_once(now(6))
    if "opened" not in (result):
        pytest.fail("Expected 'opened' in result")
    if client.market_order.call_count != 2:
        pytest.fail("Expected client.market_order.call_count == 2")


@pytest.mark.parametrize("module_name", ["bybit_demo_bot.cli", "bybit_bot.cli"])
def test_legacy_state_conservatively_consumes_current_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module_name
) -> None:
    """Verify legacy state conservatively consumes current signal."""
    import importlib

    module = importlib.import_module(module_name)
    path = tmp_path / "state.json"
    monkeypatch.setattr(module, "STATE_FILE", path)
    _ = path.write_text(
        json.dumps({
            "launched_at": "2026-01-01T00:00:00+00:00",
            "last_processed_candle": "2026-01-01T00:01:00+00:00",
        })
    )
    state_type = module.DemoState if module is cli else module.BybitState
    state = state_type.load_or_create(True)
    if state.consumed_signal_side != "legacy":
        pytest.fail("Expected state.consumed_signal_side == 'legacy'")
    client = Mock(position=Mock(return_value=None))
    monkeypatch.setattr(
        cli, "calculate_strategy_decision", lambda *_: Mock(side="Long")
    )
    monkeypatch.setattr(
        cli,
        "fetch_completed_linear_klines",
        lambda *_: pd.DataFrame(
            [{"time": pd.Timestamp("2026-01-01T00:02:00Z")}]
        ),
    )
    bot = BybitDemoBot(config(), client, state)
    if "re-entry" not in (
        bot.reconcile_once(datetime(2026, 1, 1, 0, 3, 30, tzinfo=UTC))
    ):
        pytest.fail(
            "Expected 're-entry' in bot.reconcile_once(datetime(2026, 1, 1, 0,"
            " 3, 30, tzinfo=UTC))"
        )
    client.market_order.assert_not_called()
    if state_type.load_or_create(True).consumed_signal_side != "Buy":
        pytest.fail(
            "Expected state_type.load_or_create(True).consumed_signal_side =="
            " 'Buy'"
        )


@pytest.mark.parametrize("method, count", [("GET", 2), ("POST", 1)])
def test_timestamp_retry_only_replays_reads(
    monkeypatch: pytest.MonkeyPatch, method, count
) -> None:
    """Verify timestamp retry only replays reads."""
    requests = []
    ticks = iter([1000, 1001])
    monkeypatch.setattr(client_module.time, "time", lambda: next(ticks))

    def respond(request, timeout):
        """Respond."""
        requests.append(request)
        return Response({"retCode": 10002, "retMsg": "timestamp rejected"})

    monkeypatch.setattr(client_module, "urlopen", respond)
    with pytest.raises(BybitDemoError, match="10002"):
        _ = BybitDemoClient("k", "s")._request(method, "/test")
    if len(requests) != count:
        pytest.fail("Expected len(requests) == count")
    if (
        count == 2
        and requests[0].headers["X-bapi-timestamp"]
        == requests[1].headers["X-bapi-timestamp"]
    ):
        pytest.fail(
            "Expected requests[0].headers['X-bapi-timestamp'] !="
            " requests[1].headers['X-bapi-timestamp']"
        )


@pytest.mark.parametrize(
    "pending, error, expected",
    [
        (True, None, 1),
        (False, None, 10),
        (False, "Too many visits. Exceeded the API Rate Limit.", 60),
    ],
)
def test_loop_wait_for_protection_and_rate_limits(
    monkeypatch: pytest.MonkeyPatch, pending, error, expected
) -> None:
    """Verify loop wait for protection and rate limits."""
    bot = BybitDemoBot(config(), Mock(), DemoState("2026-01-01T00:00:00Z"))
    bot.state.pending_protection_side = "Buy" if pending else None
    bot.reconcile_once = (
        Mock(side_effect=BybitDemoError(error))
        if error
        else Mock(return_value=None)
    )
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    ticks = [0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: ticks[0])

    def sleep(seconds):
        """Sleep."""
        ticks[0] += seconds
        if ticks[0] >= expected:
            bot.stop()

    monkeypatch.setattr(cli.time, "sleep", sleep)
    bot.run(10)
    if ticks[0] != expected:
        pytest.fail("Expected ticks[0] == expected")
    bot.reconcile_once.assert_called_once()


def test_exchange_close_logged_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify exchange close logged once."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    state = DemoState("2026-01-01T00:00:00Z", observed_position_side="Buy")
    bot = BybitDemoBot(config(), Mock(position=Mock(return_value=None)), state)
    monkeypatch.setattr(
        cli,
        "fetch_completed_linear_klines",
        lambda *_: pd.DataFrame(
            [{"time": pd.Timestamp("2026-01-01T00:02:00Z")}]
        ),
    )
    monkeypatch.setattr(
        cli, "calculate_strategy_decision", lambda *_: Mock(side=None)
    )
    now = datetime(2026, 1, 1, 0, 3, 30, tzinfo=UTC)
    _ = bot.reconcile_once(now)
    _ = bot.reconcile_once(now)
    if capsys.readouterr().out.count("position closed on exchange") != 1:
        pytest.fail(
            "Expected capsys.readouterr().out.count('position closed on"
            " exchange') == 1"
        )
    if (
        json.loads(cli.STATE_FILE.read_text())["observed_position_side"]
    ) is not (None):
        pytest.fail(
            "Expected"
            " json.loads(cli.STATE_FILE.read_text())['observed_position_side']"
            " is None"
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
    monkeypatch.setattr(client_module, "urlopen", opener)
    client = BybitDemoClient("key", "secret")
    with pytest.raises(BybitDemoError, match="HTTP 503: service unavailable"):
        _ = client.market_order("BTCUSDT", "Buy", Decimal(1))
    if not (body.closed):
        pytest.fail("Expected body.closed")
    opener.assert_called_once()
