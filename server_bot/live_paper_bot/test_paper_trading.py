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
    OrderLimits,
    fetch_bybit_order_limits,
    fetch_bybit_taker_fee_rate,
    latest_strategy_side,
)
from shared.models import Trade
from shared.validation import json_object, object_list, object_mapping, string
from tests.mock_support import call_kwargs, mock_child

SHA256_HEX_LENGTH = 64
ENTRY_PRICE = 30000


class TestPaperTrading:
    """Represent test paper trading."""

    @staticmethod
    def test_fetches_symbol_specific_bybit_order_limits(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify fetches symbol specific bybit order limits."""
        response = Mock()
        mock_child(response, "json").return_value = {
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
        assert fetch_bybit_order_limits("XRPUSDT") == (
            Decimal(1),
            Decimal(1),
            Decimal(5),
        )
        assert object_mapping(call_kwargs(get)["params"])["symbol"] == "XRPUSDT"

    @staticmethod
    def test_build_target_uses_symbol_specific_limits() -> None:
        """Verify build target uses symbol specific limits."""
        target = LocalPaperTrader(Path("unused.json")).build_target(
            "XRPUSDT",
            "Long",
            2.5,
            notional=14,
            limits=OrderLimits(
                quantity_step=Decimal(1),
                minimum_quantity=Decimal(1),
                exchange_minimum_notional=Decimal(5),
            ),
        )
        assert target.quantity == Decimal(5)

    @staticmethod
    def test_paper_strategy_start_is_persisted_independently() -> None:
        """Verify paper strategy start is persisted independently."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            first = datetime(2026, 1, 1, tzinfo=UTC)
            later_default = first + timedelta(days=10)

            assert trader.strategy_started_at(first) == first
            assert trader.strategy_started_at(later_default) == first
            assert trader.reset_strategy_start(later_default) == later_default

    @staticmethod
    def test_fetches_account_taker_fee_with_signed_request() -> None:
        """Verify fetches account taker fee with signed request."""
        response = Mock()
        mock_child(response, "json").return_value = {
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

        assert rate == Decimal("0.00055")
        headers = object_mapping(call_kwargs(request_get)["headers"])
        assert headers["X-BAPI-API-KEY"] == "api-key"
        assert len(string(headers["X-BAPI-SIGN"])) == SHA256_HEX_LENGTH

    @staticmethod
    def test_build_target_rounds_quantity_down() -> None:
        """Verify build target rounds quantity down."""
        trader = LocalPaperTrader(Path("unused.json"))

        target = trader.build_target("BTCUSDT", "Long", 30_000, notional=100)

        assert target.quantity == Decimal("0.003")

    @staticmethod
    def test_position_is_persisted_and_has_unrealized_pnl() -> None:
        """Verify position is persisted and has unrealized pnl."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            target = trader.build_target(
                "BTCUSDT", "Long", 30_000, notional=600
            )

            action = trader.reconcile(
                "BTCUSDT", target, taker_fee_rate=Decimal("0.00055")
            )
            position = trader.current_position("BTCUSDT")

        assert "Set local Long" in (action or "")
        assert position.side == "Long"
        assert position.quantity == Decimal("0.020")
        assert position.entry_price == ENTRY_PRICE
        assert position.entry_fee == Decimal("0.3300000")
        assert position.unrealized_pnl(31000) == pytest.approx(19.67)

    @staticmethod
    def test_reconcile_closes_local_position() -> None:
        """Verify reconcile closes local position."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", "Short", 30_000, notional=600),
                taker_fee_rate=Decimal("0.00055"),
            )

            action = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", None, 31_000, notional=600),
                taker_fee_rate=Decimal("0.0006"),
            )
            position = trader.current_position("BTCUSDT")
            history = [
                object_mapping(row)
                for row in object_list(
                    json_object((Path(directory) / "account.json").read_text())[
                        "history"
                    ]
                )
            ]

        assert "Closed local Short" in (action or "")
        assert position.side is None
        assert history[0]["exit_fee_rate"] == "0.0006"
        assert history[0]["exit_reason"] == "Strategy target flat"
        assert "return_pct" in history[0]

    @staticmethod
    def test_trade_history_is_newest_first_and_filtered_by_symbol() -> None:
        """Verify trade history is newest first and filtered by symbol."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", "Long", 30_000, notional=600),
                taker_fee_rate=Decimal("0.00055"),
            )
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", None, 31_000, notional=600),
                taker_fee_rate=Decimal("0.00055"),
            )

            history = trader.trade_history("BTCUSDT")

        assert len(history) == 1
        assert history[0]["side"] == "Long"
        assert history[0]["deposit_size"] == "600.000"

    @staticmethod
    def test_available_capital_compounds_completed_trade_net_pnl() -> None:
        """Verify available capital compounds completed trade net pnl."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            fee_rate = Decimal("0.001")
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", "Long", 100, notional=1_000),
                taker_fee_rate=fee_rate,
            )
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", None, 110, notional=1_000),
                taker_fee_rate=fee_rate,
            )

            capital = trader.available_capital("BTCUSDT", 1_000)

        assert capital == Decimal("1097.900")

    @staticmethod
    def test_available_capital_includes_prospective_reversal_close() -> None:
        """Verify available capital includes prospective reversal close."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            fee_rate = Decimal("0.001")
            _ = trader.reconcile(
                "BTCUSDT",
                trader.build_target("BTCUSDT", "Long", 100, notional=1_000),
                taker_fee_rate=fee_rate,
            )

            capital = trader.available_capital(
                "BTCUSDT", 1_000, closing_price=110, taker_fee_rate=fee_rate
            )

        assert capital == Decimal("1097.900")

    @staticmethod
    def test_reconcile_does_not_repeat_matching_position() -> None:
        """Verify reconcile does not repeat matching position."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            target = trader.build_target(
                "BTCUSDT", "Long", 30_000, notional=600
            )
            _ = trader.reconcile(
                "BTCUSDT", target, taker_fee_rate=Decimal("0.00055")
            )

            assert (
                trader.reconcile(
                    "BTCUSDT", target, taker_fee_rate=Decimal("0.00055")
                )
                is None
            )

    @staticmethod
    def test_reconcile_does_not_resize_repeated_same_side_signal() -> None:
        """Verify reconcile does not resize repeated same side signal."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            first = trader.build_target("BTCUSDT", "Long", 30_000, notional=600)
            changed_live_price = trader.build_target(
                "BTCUSDT", "Long", 31_000, notional=600
            )
            _ = trader.reconcile(
                "BTCUSDT", first, taker_fee_rate=Decimal("0.00055")
            )

            action = trader.reconcile(
                "BTCUSDT", changed_live_price, taker_fee_rate=Decimal("0.00055")
            )
            position = trader.current_position("BTCUSDT")

        assert action is None
        assert position.quantity == first.quantity
        assert position.entry_price == first.price

    @staticmethod
    def test_concurrent_duplicate_orders_create_one_position() -> None:
        """Verify concurrent duplicate orders create one position."""
        with tempfile.TemporaryDirectory() as directory:
            trader = LocalPaperTrader(Path(directory) / "account.json")
            target = trader.build_target(
                "BTCUSDT", "Short", 30_000, notional=600
            )
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(
                        trader.reconcile,
                        "BTCUSDT",
                        target,
                        taker_fee_rate=Decimal("0.00055"),
                    )
                    for _ in range(8)
                ]
                actions = [future.result() for future in futures]

        assert sum(action is not None for action in actions) == 1

    @staticmethod
    def test_latest_side_ignores_normal_strategy_exit() -> None:
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
        assert latest_strategy_side([trade], end) == "Long"
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
        assert latest_strategy_side([normal_exit], end) is None
