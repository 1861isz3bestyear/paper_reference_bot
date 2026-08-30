from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class MEXCError(RuntimeError):
    """A safe-to-log MEXC API error."""


@dataclass
class MEXCFuturesClient:
    api_key: str
    api_secret: str
    base_url: str = "https://contract.mexc.com"
    timeout: float = 20.0

    def _request(self, method: str, path: str, values: Any = None) -> Any:
        values = values or {}
        timestamp = str(int(time.time() * 1000))
        body: bytes | None = None
        url = f"{self.base_url.rstrip('/')}{path}"
        if method == "GET":
            query = urlencode(sorted(values.items()))
            if query:
                url += f"?{query}"
            signed = query
        else:
            signed = json.dumps(values, separators=(",", ":"))
            body = signed.encode()
        signature = hmac.new(
            self.api_secret.encode(), f"{self.api_key}{timestamp}{signed}".encode(), hashlib.sha256
        ).hexdigest()
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "ApiKey": self.api_key,
                "Request-Time": timestamp,
                "Signature": signature,
                "Content-Type": "application/json",
                "User-Agent": "server-bot-real/1.0",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise MEXCError(f"MEXC HTTP {exc.code}: {detail}") from None
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise MEXCError(f"MEXC request failed: {exc}") from None
        if not isinstance(payload, dict) or not payload.get("success", False):
            message = payload.get("message", "unexpected response") if isinstance(payload, dict) else "unexpected response"
            raise MEXCError(f"MEXC API error: {message}")
        return payload.get("data")

    def assets(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v1/private/account/assets")
        return data if isinstance(data, list) else []

    def positions(self, symbol: str) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v1/private/position/open_positions", {"symbol": symbol})
        return data if isinstance(data, list) else []

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/api/v1/private/order/list/open_orders/{symbol}")
        if isinstance(data, dict):
            data = data.get("resultList", [])
        return data if isinstance(data, list) else []

    def cancel_orders(self, symbol: str, order_ids: list[str]) -> Any:
        if not order_ids:
            return None
        return self._request("POST", "/api/v1/private/order/cancel", [
            {"symbol": symbol, "orderId": order_id} for order_id in order_ids
        ])

    def contract(self, symbol: str) -> dict[str, Any]:
        try:
            with urlopen(f"{self.base_url.rstrip('/')}/api/v1/contract/detail?symbol={symbol}", timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise MEXCError(f"Could not load MEXC contract limits: {exc}") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            data = next((item for item in data if item.get("symbol") == symbol), None)
        if not isinstance(payload, dict) or not payload.get("success", False) or not isinstance(data, dict):
            raise MEXCError(f"MEXC returned no contract limits for {symbol}")
        return data

    def submit_market_order(
        self, *, symbol: str, side: int, volume: int, leverage: int, open_type: int, external_oid: str
    ) -> Any:
        return self._request("POST", "/api/v1/private/order/submit", {
            "symbol": symbol,
            "price": 0,
            "vol": volume,
            "side": side,
            "type": 5,
            "openType": open_type,
            "leverage": leverage,
            "externalOid": external_oid,
        })

    def submit_limit_order(
        self,
        *,
        symbol: str,
        side: int,
        volume: int,
        price: Decimal,
        take_profit_price: Decimal,
        stop_loss_price: Decimal | None,
        leverage: int,
        open_type: int,
        external_oid: str,
    ) -> Any:
        payload: dict[str, Any] = {
            "symbol": symbol,
            "price": format(price, "f"),
            "vol": volume,
            "side": side,
            "type": 1,
            "openType": open_type,
            "leverage": leverage,
            "takeProfitPrice": format(take_profit_price, "f"),
            "externalOid": external_oid,
        }
        if stop_loss_price is not None:
            payload["stopLossPrice"] = format(stop_loss_price, "f")
        return self._request("POST", "/api/v1/private/order/submit", payload)

    def available_usdt(self) -> Decimal:
        asset = next((item for item in self.assets() if item.get("currency") == "USDT"), None)
        return Decimal(str(asset.get("availableBalance", 0))) if asset else Decimal("0")
