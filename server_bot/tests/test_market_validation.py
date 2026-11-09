# Copyright 2026 anchored-vwam-backtest contributors
"""Reject malformed exchange values before they enter paper market state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from live_paper_bot import market as live_market
from reference_bot import market as reference_market

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal


@dataclass(frozen=True, kw_only=True)
class ExchangeResponse:
    """Deterministic decoded exchange response."""

    payload: object

    def json(self) -> object:
        """Return the configured response value.

        Returns:
            Unvalidated external data.

        """
        return self.payload

    @staticmethod
    def raise_for_status() -> None:
        """Accept the successful HTTP status used by these fixtures."""


@pytest.mark.parametrize(
    "fetch_price",
    [live_market.fetch_live_price, reference_market.fetch_live_price],
)
@pytest.mark.parametrize("price", ["NaN", "Infinity", "-Infinity"])
def test_bybit_price_must_be_finite(
    monkeypatch: pytest.MonkeyPatch,
    fetch_price: Callable[[str], float],
    price: str,
) -> None:
    """Reject nonfinite Bybit prices even when the API reports success."""
    response = ExchangeResponse(
        payload={
            "retCode": 0,
            "result": {"list": [{"lastPrice": price}]},
        }
    )
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    with pytest.raises(ValueError, match="finite"):
        _ = fetch_price("BTCUSDT")


@pytest.mark.parametrize(
    "fetch_fee",
    [
        live_market.fetch_mexc_taker_fee_rate,
        reference_market.fetch_mexc_taker_fee_rate,
    ],
)
@pytest.mark.parametrize("rate", ["NaN", "Infinity", "-Infinity"])
def test_mexc_commission_must_be_finite(
    monkeypatch: pytest.MonkeyPatch,
    fetch_fee: Callable[[str], Decimal],
    rate: str,
) -> None:
    """Reject nonfinite commissions before they can contaminate balances."""
    response = ExchangeResponse(
        payload={
            "symbols": [{"takerCommission": rate}],
        }
    )
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    with pytest.raises(ValueError, match="invalid taker commission"):
        _ = fetch_fee("BTCUSDT")


@pytest.mark.parametrize(
    "fetch_price",
    [live_market.fetch_mexc_live_price, reference_market.fetch_mexc_live_price],
)
@pytest.mark.parametrize("price", ["NaN", "Infinity", "-Infinity"])
def test_mexc_price_must_be_finite(
    monkeypatch: pytest.MonkeyPatch,
    fetch_price: Callable[[str], float],
    price: str,
) -> None:
    """Reject nonfinite MEXC prices before calculating an order size."""
    response = ExchangeResponse(payload={"price": price})
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    with pytest.raises(ValueError, match="finite"):
        _ = fetch_price("BTCUSDT")
