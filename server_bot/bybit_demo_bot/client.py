# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of bybit demo bot client."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal
from time import time as epoch_seconds
from typing import TYPE_CHECKING, Protocol, Self, cast, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shared.validation import json_object, object_list, object_mapping

if TYPE_CHECKING:
    from collections.abc import Mapping


@runtime_checkable
class ResponseStream(Protocol):
    """Readable context manager returned by the HTTP transport."""

    def read(self) -> bytes:
        """Read the response body."""
        ...

    def __enter__(self) -> Self:
        """Enter the response context."""
        ...

    def __exit__(self, *args: object) -> None:
        """Release the response context."""
        ...


def response_rows(value: object) -> list[dict[str, object]]:
    """Validate the object rows returned by a Bybit list endpoint.

    Returns:
        Rows with validated string keys.

    """
    return [object_mapping(row) for row in object_list(value)]


def _decode_response(handle: object) -> dict[str, object]:
    if not isinstance(handle, ResponseStream):
        message = "Expected a readable HTTP response"
        raise TypeError(message)
    with handle as response:
        return json_object(response.read().decode())


@dataclass(frozen=True, kw_only=True)
class HedgeEntry:
    """Prices and quantity for one protected hedge entry."""

    side: str
    quantity: Decimal
    price: Decimal
    take_profit: Decimal
    stop_loss: Decimal
    link: str


class BybitDemoError(RuntimeError):
    """A credential-safe Bybit demo API error."""


@dataclass
class BybitDemoClient:
    """Represent bybit demo client."""

    api_key: str
    api_secret: str
    base_url: str = "https://api-demo.bybit.com"
    timeout: float = 20.0
    recv_window: str = "5000"
    environment_name: str = "Bybit demo"
    user_agent: str = "server-bot-bybit-demo/1.0"

    def _request(
        self, method: str, path: str, values: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        # Only replay reads: a failed order response can still mean it executed.
        try:
            return self._request_once(method, path, values)
        except BybitDemoError as exc:
            if method != "GET" or "API error [10002]" not in str(exc):
                raise
            return self._request_once(method, path, values)

    def _request_once(
        self, method: str, path: str, values: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        values = values or {}
        timestamp = str(int(epoch_seconds() * 1000))
        query = urlencode(sorted(values.items())) if method == "GET" else ""
        body_text = (
            "" if method == "GET" else json.dumps(values, separators=(",", ":"))
        )
        signature = hmac.new(
            self.api_secret.encode(),
            f"{timestamp}{self.api_key}{self.recv_window}{query or body_text}"
            .encode(),
            hashlib.sha256,
        ).hexdigest()
        url = f"{self.base_url.rstrip('/')}{path}" + (
            f"?{query}" if query else ""
        )
        request = Request(
            url,
            data=body_text.encode() if body_text else None,
            method=method,
            headers={
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": self.recv_window,
                "X-BAPI-SIGN": signature,
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        try:
            payload = _decode_response(
                cast("object", urlopen(request, timeout=self.timeout))
            )
        except HTTPError as exc:
            with exc:
                detail = exc.read().decode(errors="replace")[:500]
            message = f"{self.environment_name} HTTP {exc.code}: {detail}"
            raise BybitDemoError(message) from None
        except (URLError, TimeoutError, TypeError, json.JSONDecodeError) as exc:
            message = f"{self.environment_name} request failed: {exc}"
            raise BybitDemoError(message) from None
        if payload.get("retCode") != 0:
            detail = payload.get("retMsg", "unexpected response")
            message = (
                f"{self.environment_name} {method} {path} API error"
                f" [{payload.get('retCode')}]: {detail}"
            )
            raise BybitDemoError(message)
        return object_mapping(payload.get("result", {}))

    def instruments(self, symbol: str) -> dict[str, object]:
        """Read exchange metadata for a linear instrument.

        Returns:
            The validated instrument object.

        Raises:
            BybitDemoError: If no instrument is returned.

        """
        rows = response_rows(
            self._request(
                "GET",
                "/v5/market/instruments-info",
                {"category": "linear", "symbol": symbol},
            ).get("list", [])
        )
        if not rows:
            message = (
                f"{self.environment_name} returned no instrument data for"
                f" {symbol}"
            )
            raise BybitDemoError(message)
        return rows[0]

    def last_price(self, symbol: str) -> Decimal:
        """Read the latest traded price for a symbol.

        Returns:
            The exchange price as a decimal.

        Raises:
            BybitDemoError: If no ticker is returned.

        """
        rows = response_rows(
            self._request(
                "GET",
                "/v5/market/tickers",
                {"category": "linear", "symbol": symbol},
            ).get("list", [])
        )
        if not rows:
            message = f"{self.environment_name} returned no ticker for {symbol}"
            raise BybitDemoError(message)
        return Decimal(str(rows[0]["lastPrice"]))

    def available_usdt(self) -> Decimal:
        """Read the USDT wallet balance from the unified account.

        Returns:
            The wallet balance, or zero when USDT is absent.

        """
        rows = response_rows(
            self._request(
                "GET",
                "/v5/account/wallet-balance",
                {"accountType": "UNIFIED", "coin": "USDT"},
            ).get("list", [])
        )
        coins = response_rows(rows[0].get("coin", [])) if rows else []
        coin = next(
            (item for item in coins if item.get("coin") == "USDT"), None
        )
        return Decimal(str((coin or {}).get("walletBalance", 0)))

    def position(self, symbol: str) -> Mapping[str, object] | None:
        """Read the first nonzero position for a symbol.

        Returns:
            An exchange position, or None when the account is flat.

        """
        rows = response_rows(
            self._request(
                "GET",
                "/v5/position/list",
                {"category": "linear", "symbol": symbol},
            ).get("list", [])
        )
        return next(
            (row for row in rows if Decimal(str(row.get("size", 0))) > 0), None
        )

    def market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        *,
        reduce_only: bool = False,
        order_link_id: str | None = None,
    ) -> str:
        """Submit a market order with optional reduction and deduplication.

        Returns:
            The exchange order identifier.

        Raises:
            BybitDemoError: If the response lacks an order identifier.

        """
        values: dict[str, object] = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": format(quantity, "f"),
            "reduceOnly": reduce_only,
        }
        if order_link_id is not None:
            values["orderLinkId"] = order_link_id
        result = self._request("POST", "/v5/order/create", values)
        order_id = result.get("orderId")
        if not order_id:
            message = f"{self.environment_name} returned no order ID"
            raise BybitDemoError(message)
        return str(order_id)

    def set_protection(
        self, symbol: str, stop_loss: Decimal, take_profit: Decimal
    ) -> None:
        """Set full-position stop loss and take profit.

        Raises:
            BybitDemoError: If the exchange rejects a protection update.

        """
        try:
            _ = self._request(
                "POST",
                "/v5/position/trading-stop",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "positionIdx": 0,
                    "tpslMode": "Full",
                    "stopLoss": format(stop_loss, "f"),
                    "takeProfit": format(take_profit, "f"),
                    "slOrderType": "Market",
                    "tpOrderType": "Market",
                },
            )
        except BybitDemoError as exc:
            # Bybit uses an error retCode for an idempotent protection update.
            # Existing exchange-side SL/TP is already the requested protection.
            if "not modified" not in str(exc).lower():
                raise

    def order_by_link(
        self, symbol: str, order_link_id: str
    ) -> Mapping[str, object] | None:
        """Find an order in active orders or execution history.

        Returns:
            The matching order, or None when neither endpoint finds it.

        """
        values = {
            "category": "linear",
            "symbol": symbol,
            "orderLinkId": order_link_id,
        }
        for endpoint in ("/v5/order/realtime", "/v5/order/history"):
            rows = response_rows(
                self._request("GET", endpoint, values).get("list", [])
            )
            if rows:
                return rows[0]
        return None

    def cancel_by_link(self, symbol: str, order_link_id: str) -> None:
        """Cancel the order identified by its client link."""
        _ = self._request(
            "POST",
            "/v5/order/cancel",
            {
                "category": "linear",
                "symbol": symbol,
                "orderLinkId": order_link_id,
            },
        )

    def has_open_orders(self, symbol: str) -> bool:
        """Check for active orders or additional result pages.

        Returns:
            Whether the exchange reports any open orders.

        """
        result = self._request(
            "GET",
            "/v5/order/realtime",
            {
                "category": "linear",
                "symbol": symbol,
                "openOnly": 0,
            },
        )
        return bool(result.get("list") or result.get("nextPageCursor"))

    def hedge_positions(self, symbol: str) -> list[dict[str, object]]:
        """Read active hedge legs and reject one-way position mode.

        Returns:
            The nonzero hedge positions.

        Raises:
            BybitDemoError: If the symbol is configured for one-way mode.

        """
        rows = response_rows(
            self._request(
                "GET",
                "/v5/position/list",
                {"category": "linear", "symbol": symbol},
            ).get("list", [])
        )
        if any(int(str(row.get("positionIdx", 0))) == 0 for row in rows):
            message = (
                "VWAP zero hedge requires Bybit hedge position mode for this"
                " symbol"
            )
            raise BybitDemoError(message)
        return [row for row in rows if Decimal(str(row.get("size", 0))) > 0]

    def hedge_limit_order(self, symbol: str, entry: HedgeEntry) -> str:
        """Submit a protected limit entry for one hedge leg.

        Returns:
            The exchange order identifier.

        Raises:
            BybitDemoError: If the response lacks an order identifier.

        """
        values: dict[str, object] = {
            "category": "linear",
            "symbol": symbol,
            "side": entry.side,
            "orderType": "Limit",
            "qty": format(entry.quantity, "f"),
            "price": format(entry.price, "f"),
            "timeInForce": "GTC",
            "positionIdx": 1 if entry.side == "Buy" else 2,
            "orderLinkId": entry.link,
            "takeProfit": format(entry.take_profit, "f"),
            "stopLoss": format(entry.stop_loss, "f"),
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market",
        }
        result = self._request("POST", "/v5/order/create", values)
        if not result.get("orderId"):
            message = "Hedge entry returned no order ID"
            raise BybitDemoError(message)
        return str(result["orderId"])

    def hedge_close(self, symbol: str, position: dict[str, object]) -> None:
        """Submit a reduce-only market close for a hedge leg."""
        _ = self._request(
            "POST",
            "/v5/order/create",
            {
                "category": "linear",
                "symbol": symbol,
                "side": "Sell" if position["side"] == "Buy" else "Buy",
                "orderType": "Market",
                "qty": str(position["size"]),
                "reduceOnly": True,
                "positionIdx": int(str(position["positionIdx"])),
            },
        )

    def hedge_protection(
        self,
        symbol: str,
        position_idx: int,
        *,
        stop_loss: Decimal,
        take_profit: Decimal,
    ) -> None:
        """Update exchange protection for one hedge leg.

        Raises:
            BybitDemoError: If the exchange rejects a protection update.

        """
        try:
            _ = self._request(
                "POST",
                "/v5/position/trading-stop",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "positionIdx": position_idx,
                    "tpslMode": "Full",
                    "stopLoss": format(stop_loss, "f"),
                    "takeProfit": format(take_profit, "f"),
                    "slOrderType": "Market",
                    "tpOrderType": "Market",
                },
            )
        except BybitDemoError as exc:
            if "not modified" not in str(exc).lower():
                raise
