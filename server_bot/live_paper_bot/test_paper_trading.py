# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for live paper bot test paper trading."""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from live_paper_bot.trading import (
    LocalPaperTrader,
    fetch_bybit_order_limits,
    fetch_bybit_taker_fee_rate,
    latest_strategy_side,
)
from shared.models import Trade


class TestPaperTrading:
    """Represent test paper trading."""

    def test_fetches_symbol_specific_bybit_order_limits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify fetches symbol specific bybit order limits."""
        response = Mock()
        response.json.return_value = {
            "retCode": 0,
            "result": {
                "list": [{
                    "lotSizeFilter": {
                        "qtyStep": "1",
                        "minOrderQty": "1",
                        "minNotionalValue": "5",
                    }
                }]
            },
        }
        get = Mock(return_value=response)
        monkeypatch.setattr("live_paper_bot.trading.requests.get", get)
        fetch_bybit_order_limits.cache_clear()
        if fetch_bybit_order_limits("XRPUSDT") != (
            Decimal(1),
            Decimal(1),
            Decimal(5),
        ):
            pytest.fail(
                "Expected fetch_bybit_order_limits('XRPUSDT') == (Decimal(1),"
                " Decimal(1), Decimal(5))"
            )
        if get.call_args.kwargs["params"]["symbol"] != "XRPUSDT":
            pytest.fail(
                "Expected get.call_args.kwargs['params']['symbol'] == 'XRPUSDT'"
            )

    def test_build_target_uses_symbol_specific_limits(self) -> None:
        """Verify build target uses symbol specific limits."""
        target = LocalPaperTrader(Path("unused.json")).build_target(
            "XRPUSDT",
            "Long",
            2.5,
            14,
            quantity_step=Decimal(1),
            minimum_quantity=Decimal(1),
            exchange_minimum_notional=Decimal(5),
        )
        if target.quantity != Decimal(5):
            pytest.fail("Expected target.quantity == Decimal(5)")

    def test_paper_strategy_start_is_persisted_independently(self) -> None:
        """Verify paper strategy start is persisted independently."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            first = datetime(2026, 1, 1, tzinfo=UTC)
            later_default = first + timedelta(days=10)

            if trader.strategy_started_at(first) != first:
                pytest.fail(
                    "Expected trader.strategy_started_at(first) == first"
                )
            if trader.strategy_started_at(later_default) != first:
                pytest.fail(
                    "Expected trader.strategy_started_at(later_default) =="
                    " first"
                )
            if trader.reset_strategy_start(later_default) != later_default:
                pytest.fail(
                    "Expected trader.reset_strategy_start(later_default) =="
                    " later_default"
                )

    def test_fetches_account_taker_fee_with_signed_request(self) -> None:
        """Verify fetches account taker fee with signed request."""
        response = Mock()
        response.json.return_value = {
            "retCode": 0,
            "result": {
                "list": [{"symbol": "BTCUSDT", "takerFeeRate": "0.00055"}]
            },
        }
        request_get = Mock(return_value=response)

        rate = fetch_bybit_taker_fee_rate(
            "api-key",
            "api-secret",
            "BTCUSDT",
            timestamp_ms=1_700_000_000_000,
            request_get=request_get,
        )

        if rate != Decimal("0.00055"):
            pytest.fail("Expected rate == Decimal('0.00055')")
        headers = request_get.call_args.kwargs["headers"]
        if headers["X-BAPI-API-KEY"] != "api-key":
            pytest.fail("Expected headers['X-BAPI-API-KEY'] == 'api-key'")
        if len(headers["X-BAPI-SIGN"]) != 64:
            pytest.fail("Expected len(headers['X-BAPI-SIGN']) == 64")

    def test_build_target_rounds_quantity_down(self) -> None:
        """Verify build target rounds quantity down."""
        trader = LocalPaperTrader(Path("unused.json"))

        target = trader.build_target("BTCUSDT", "Long", 30_000, 100)

        if target.quantity != Decimal("0.003"):
            pytest.fail("Expected target.quantity == Decimal('0.003')")

    def test_position_is_persisted_and_has_unrealized_pnl(self) -> None:
        """Verify position is persisted and has unrealized pnl."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            target = trader.build_target("BTCUSDT", "Long", 30_000, 600)

            action = trader.reconcile("BTCUSDT", target, Decimal("0.00055"))
            position = trader.current_position("BTCUSDT")

        if "Set local Long" not in (action or ""):
            pytest.fail("Expected 'Set local Long' in (action or '')")
        if position.side != "Long":
            pytest.fail("Expected position.side == 'Long'")
        if position.quantity != Decimal("0.020"):
            pytest.fail("Expected position.quantity == Decimal('0.020')")
        if position.entry_price != 30000:
            pytest.fail("Expected position.entry_price == 30000")
        if position.entry_fee != Decimal("0.3300000"):
            pytest.fail("Expected position.entry_fee == Decimal('0.3300000')")
        if position.unrealized_pnl(31000) != pytest.approx(19.67):
            pytest.fail(
                "Expected position.unrealized_pnl(31000) =="
                " pytest.approx(19.67)"
            )

    def test_reconcile_closes_local_position(self) -> None:
        """Verify reconcile closes local position."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", "Short", 30_000, 600),
                Decimal("0.00055"),
            )

            action = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", None, 31_000, 600),
                Decimal("0.0006"),
            )
            position = trader.current_position("BTCUSDT")
            history = __import__("json").loads(
                (Path(directory) / "account.json").read_text()
            )["history"]

        if "Closed local Short" not in (action or ""):
            pytest.fail("Expected 'Closed local Short' in (action or '')")
        if (position.side) is not (None):
            pytest.fail("Expected position.side is None")
        if history[0]["exit_fee_rate"] != "0.0006":
            pytest.fail("Expected history[0]['exit_fee_rate'] == '0.0006'")
        if history[0]["exit_reason"] != "Strategy target flat":
            pytest.fail(
                "Expected history[0]['exit_reason'] == 'Strategy target flat'"
            )
        if "return_pct" not in (history[0]):
            pytest.fail("Expected 'return_pct' in history[0]")

    def test_trade_history_is_newest_first_and_filtered_by_symbol(self) -> None:
        """Verify trade history is newest first and filtered by symbol."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", "Long", 30_000, 600),
                Decimal("0.00055"),
            )
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", None, 31_000, 600),
                Decimal("0.00055"),
            )

            history = trader.trade_history("BTCUSDT")

        if len(history) != 1:
            pytest.fail("Expected len(history) == 1")
        if history[0]["side"] != "Long":
            pytest.fail("Expected history[0]['side'] == 'Long'")
        if history[0]["deposit_size"] != "600.000":
            pytest.fail("Expected history[0]['deposit_size'] == '600.000'")

    def test_available_capital_compounds_completed_trade_net_pnl(self) -> None:
        """Verify available capital compounds completed trade net pnl."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            fee_rate = Decimal("0.001")
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", "Long", 100, 1_000),
                fee_rate,
            )
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", None, 110, 1_000),
                fee_rate,
            )

            capital = trader.available_capital("BTCUSDT", 1_000)

        if capital != Decimal("1097.900"):
            pytest.fail("Expected capital == Decimal('1097.900')")

    def test_available_capital_includes_prospective_reversal_close(
        self,
    ) -> None:
        """Verify available capital includes prospective reversal close."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            fee_rate = Decimal("0.001")
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", "Long", 100, 1_000),
                fee_rate,
            )

            capital = trader.available_capital(
                "BTCUSDT", 1_000, closing_price=110, taker_fee_rate=fee_rate
            )

        if capital != Decimal("1097.900"):
            pytest.fail("Expected capital == Decimal('1097.900')")

    def test_reconcile_does_not_repeat_matching_position(self) -> None:
        """Verify reconcile does not repeat matching position."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            target = trader.build_target("BTCUSDT", "Long", 30_000, 600)
            _ = trader.reconcile("BTCUSDT", target, Decimal("0.00055"))

            if (
                trader.reconcile("BTCUSDT", target, Decimal("0.00055"))
            ) is not (None):
                pytest.fail(
                    "Expected trader.reconcile('BTCUSDT', target,"
                    " Decimal('0.00055')) is None"
                )

    def test_reconcile_does_not_resize_repeated_same_side_signal(self) -> None:
        """Verify reconcile does not resize repeated same side signal."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            first = trader.build_target("BTCUSDT", "Long", 30_000, 600)
            changed_live_price = trader.build_target(
                "BTCUSDT", "Long", 31_000, 600
            )
            _ = trader.reconcile("BTCUSDT", first, Decimal("0.00055"))

            action = trader.reconcile(
                "BTCUSDT", changed_live_price, Decimal("0.00055")
            )
            position = trader.current_position("BTCUSDT")

        if (action) is not (None):
            pytest.fail("Expected action is None")
        if position.quantity != first.quantity:
            pytest.fail("Expected position.quantity == first.quantity")
        if position.entry_price != first.price:
            pytest.fail("Expected position.entry_price == first.price")

    def test_concurrent_duplicate_orders_create_one_position(self) -> None:
        """Verify concurrent duplicate orders create one position."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            target = trader.build_target("BTCUSDT", "Short", 30_000, 600)
            with ThreadPoolExecutor(max_workers=4) as pool:
                actions = list(
                    pool.map(
                        lambda _: trader.reconcile(
                            "BTCUSDT", target, Decimal("0.00055")
                        ),
                        range(8),
                    )
                )

        if sum(action is not None for action in actions) != 1:
            pytest.fail(
                "Expected sum((action is not None for action in actions)) == 1"
            )

    def test_latest_side_ignores_normal_strategy_exit(self) -> None:
        """Verify latest side ignores normal strategy exit."""
        end = pd.Timestamp("2026-01-01 01:00", tz="UTC")
        trade = Trade(
            100,
            "Long",
            end - pd.Timedelta(hours=1),
            end,
            10,
            11,
            10,
            10,
            10,
            "End of strategy",
        )
        if latest_strategy_side([trade], end) != "Long":
            pytest.fail("Expected latest_strategy_side([trade], end) == 'Long'")
        normal_exit = Trade(
            100,
            "Long",
            end - pd.Timedelta(hours=1),
            end,
            10,
            11,
            10,
            10,
            10,
            "VWAP cross",
        )
        if (latest_strategy_side([normal_exit], end)) is not (None):
            pytest.fail(
                "Expected latest_strategy_side([normal_exit], end) is None"
            )
