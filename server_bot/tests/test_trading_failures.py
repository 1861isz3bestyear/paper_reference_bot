# Copyright 2026 anchored-vwam-backtest contributors
"""Reject corrupt ledger data and exchange failures in both paper bots."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest

from live_paper_bot import trading as live_module
from reference_bot import trading as reference_module

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(params=[False, True])
def live(request: pytest.FixtureRequest) -> bool:
    """Select a deployed bot and isolate its runtime artifacts.

    Returns:
        Whether the live-paper implementation is selected.

    Raises:
        TypeError: If a fixture parameter is not a boolean.

    """
    value = cast("object", request.param)
    if not isinstance(value, bool):
        message = "Expected a boolean implementation selector."
        raise TypeError(message)
    return value


def test_credential_file_and_environment(
    *, live: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read quoted credentials, ignore comments and fall back to environment."""
    trading = live_module if live else reference_module
    path = tmp_path / "credentials.env"
    monkeypatch.setattr(trading, "BYBIT_ENV_FILES", (path,))
    monkeypatch.setenv("BYBIT_API_KEY", "env-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "env-secret")
    assert trading.load_bybit_credentials() == ("env-key", "env-secret")
    _ = path.write_text(
        "# comment\nignored\n\nBYBIT_API_KEY='file-key'\n"
        'BYBIT_API_SECRET="file-secret"\n'
    )
    assert trading.load_bybit_credentials() == ("file-key", "file-secret")


@pytest.mark.parametrize(
    "payload",
    [
        {"retCode": 1, "retMsg": "denied"},
        {"retCode": 0, "result": {"list": []}},
        {"retCode": 0, "result": {"list": [{"takerFeeRate": "-1"}]}},
    ],
)
def test_fee_request_errors(*, live: bool, payload: dict[str, object]) -> None:
    """Reject refused, empty and negative exchange fee responses."""
    trading = live_module if live else reference_module
    response = Mock(json=Mock(return_value=payload))
    with pytest.raises(RuntimeError):
        _ = trading.fetch_bybit_taker_fee_rate(
            "key", "secret", "BTCUSDT", request_get=Mock(return_value=response)
        )
    with pytest.raises(ValueError, match=r"required"):
        _ = trading.fetch_bybit_taker_fee_rate("", "", "BTCUSDT")


@pytest.mark.parametrize(
    "payload",
    [
        {"retCode": 1},
        {"retCode": 0},
        {
            "retCode": 0,
            "result": {"list": [{"lotSizeFilter": dict[str, object]()}]},
        },
        {
            "retCode": 0,
            "result": {
                "list": [
                    {"lotSizeFilter": {"qtyStep": "0", "minOrderQty": "1"}}
                ]
            },
        },
    ],
)
def test_order_limit_errors(
    *, live: bool, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never cache invalid instrument responses as usable order limits."""
    trading = live_module if live else reference_module
    response = Mock(json=Mock(return_value=payload))
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    trading.fetch_bybit_order_limits.cache_clear()
    with pytest.raises(RuntimeError):
        _ = trading.fetch_bybit_order_limits("INVALID")
    trading.fetch_bybit_order_limits.cache_clear()


@pytest.mark.parametrize("content", ["[]", "null", "{bad"])
def test_corrupt_account_root(
    *, live: bool, tmp_path: Path, content: str
) -> None:
    """Do not silently replace a damaged account with a fresh ledger."""
    trading = live_module if live else reference_module
    path = tmp_path / "account.json"
    _ = path.write_text(content)
    trader = trading.LocalPaperTrader(path)
    with pytest.raises(RuntimeError, match=r"Could not read"):
        _ = trader.current_position("BTCUSDT")
    assert path.read_text() == content


def test_corrupt_position_and_history(*, live: bool, tmp_path: Path) -> None:
    """Reject missing positions and invalid history at ledger boundaries."""
    trading = live_module if live else reference_module
    path = tmp_path / "account.json"
    trader = trading.LocalPaperTrader(path)
    assert trader.current_position("BTCUSDT").unrealized_pnl(100) == 0
    _ = path.write_text(
        json.dumps({"positions": {"BTCUSDT": {"side": "Long"}}})
    )
    with pytest.raises(RuntimeError, match=r"Invalid local paper position"):
        _ = trader.current_position("BTCUSDT")
    _ = path.write_text('{"history": {}}')
    with pytest.raises(RuntimeError, match=r"Invalid trade history"):
        _ = trader.trade_history("BTCUSDT")
    with pytest.raises(RuntimeError, match=r"Invalid trade history"):
        _ = trader.available_capital("BTCUSDT", 1000)
    _ = path.write_text('{"history": [{"symbol": "BTCUSDT"}]}')
    with pytest.raises(RuntimeError, match=r"Invalid trade history"):
        _ = trader.available_capital("BTCUSDT", 1000)


@pytest.mark.parametrize(
    ("price", "notional", "side"),
    [
        (0, 100, "Long"),
        (100, 0, "Long"),
        (100, 100, "invalid"),
        (1000000, 0.01, "Long"),
    ],
)
def test_invalid_target(
    *, live: bool, tmp_path: Path, price: float, notional: float, side: str
) -> None:
    """Reject invalid signals and allocations below exchange quantity limits."""
    trading = live_module if live else reference_module
    with pytest.raises(ValueError, match=r"side|positive|too small"):
        _ = trading.LocalPaperTrader(tmp_path / "account.json").build_target(
            "BTCUSDT", side, price, notional=notional
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity_step", Decimal(0)),
        ("minimum_quantity", Decimal(0)),
        ("exchange_minimum_notional", Decimal(-1)),
    ],
)
def test_invalid_exchange_limits(
    *, live: bool, tmp_path: Path, field: str, value: Decimal
) -> None:
    """Refuse malformed quantity grids and negative minimum notional."""
    if live:
        build_target = partial(
            live_module.LocalPaperTrader(
                tmp_path / "account.json"
            ).build_target,
            "BTCUSDT",
            "Long",
            100,
            notional=100,
            limits=live_module.OrderLimits(**{field: value}),
        )
    else:
        build_target = partial(
            reference_module.LocalPaperTrader(
                tmp_path / "account.json"
            ).build_target,
            "BTCUSDT",
            "Long",
            100,
            notional=100,
            limits=reference_module.OrderLimits(**{field: value}),
        )
    with pytest.raises(ValueError, match=r"limits"):
        _ = build_target()


def test_account_validation_before_mutation(*, tmp_path: Path) -> None:
    """Reject negative fees and naive execution timestamps before writing."""
    trading = reference_module
    path = tmp_path / "account.json"
    trader = trading.LocalPaperTrader(path)
    with pytest.raises(ValueError, match=r"capital"):
        _ = trader.available_capital("BTCUSDT", 0)
    with pytest.raises(ValueError, match=r"fee"):
        _ = trader.available_capital("BTCUSDT", 100, taker_fee_rate=Decimal(-1))
    with pytest.raises(ValueError, match=r"fee"):
        _ = trader.reconcile(
            "BTCUSDT",
            trader.build_target("BTCUSDT", "Long", 100, notional=100),
            taker_fee_rate=Decimal(-1),
        )
    with pytest.raises(ValueError, match=r"timezone"):
        _ = trader.reconcile(
            "BTCUSDT",
            trader.build_target("BTCUSDT", "Long", 100, notional=100),
            taker_fee_rate=Decimal(0),
            executed_at=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        )
    assert not path.exists()
    _ = trader.reconcile(
        "BTCUSDT",
        trader.build_target("BTCUSDT", "Long", 100, notional=100),
        taker_fee_rate=Decimal(0),
    )
    with pytest.raises(ValueError, match=r"Closing price"):
        _ = trader.available_capital("BTCUSDT", 100, closing_price=0)


def test_account_write_failure(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surface atomic persistence failures and preserve the prior ledger."""
    trading = reference_module
    path = tmp_path / "account.json"
    trader = trading.LocalPaperTrader(path)
    _ = trader.reconcile(
        "BTCUSDT",
        trader.build_target("BTCUSDT", "Long", 100, notional=100),
        taker_fee_rate=Decimal(0),
    )
    original = path.read_bytes()
    monkeypatch.setattr(
        type(path), "replace", Mock(side_effect=OSError("disk failure"))
    )
    with pytest.raises(RuntimeError, match=r"Could not write"):
        _ = trader.reset_strategy_start(datetime.now().astimezone())
    assert path.read_bytes() == original


def test_account_write_failure_live(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surface atomic persistence failures and preserve the prior ledger."""
    trading = live_module
    path = tmp_path / "account.json"
    trader = trading.LocalPaperTrader(path)
    _ = trader.reconcile(
        "BTCUSDT",
        trader.build_target("BTCUSDT", "Long", 100, notional=100),
        taker_fee_rate=Decimal(0),
    )
    original = path.read_bytes()
    monkeypatch.setattr(
        type(path), "replace", Mock(side_effect=OSError("disk failure"))
    )
    with pytest.raises(RuntimeError, match=r"Could not write"):
        _ = trader.reset_strategy_start(datetime.now().astimezone())
    assert path.read_bytes() == original


def test_account_validation_before_mutation_live(*, tmp_path: Path) -> None:
    """Reject negative fees and naive execution timestamps before writing."""
    trading = live_module
    path = tmp_path / "account.json"
    trader = trading.LocalPaperTrader(path)
    with pytest.raises(ValueError, match=r"capital"):
        _ = trader.available_capital("BTCUSDT", 0)
    with pytest.raises(ValueError, match=r"fee"):
        _ = trader.available_capital("BTCUSDT", 100, taker_fee_rate=Decimal(-1))
    with pytest.raises(ValueError, match=r"fee"):
        _ = trader.reconcile(
            "BTCUSDT",
            trader.build_target("BTCUSDT", "Long", 100, notional=100),
            taker_fee_rate=Decimal(-1),
        )
    with pytest.raises(ValueError, match=r"timezone"):
        _ = trader.reconcile(
            "BTCUSDT",
            trader.build_target("BTCUSDT", "Long", 100, notional=100),
            taker_fee_rate=Decimal(0),
            executed_at=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        )
    assert not path.exists()
    _ = trader.reconcile(
        "BTCUSDT",
        trader.build_target("BTCUSDT", "Long", 100, notional=100),
        taker_fee_rate=Decimal(0),
    )
    with pytest.raises(ValueError, match=r"Closing price"):
        _ = trader.available_capital("BTCUSDT", 100, closing_price=0)
