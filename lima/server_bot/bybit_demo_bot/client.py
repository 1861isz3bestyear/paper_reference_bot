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


class BybitDemoError(RuntimeError):
    """A credential-safe Bybit demo API error."""


@dataclass
class BybitDemoClient:
    api_key: str
    api_secret: str
    base_url: str = "https://api-demo.bybit.com"
    timeout: float = 20.0
    recv_window: str = "5000"

    def _request(self, method: str, path: str, values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = values or {}
        timestamp = str(int(time.time() * 1000))
        query = urlencode(sorted(values.items())) if method == "GET" else ""
        body_text = "" if method == "GET" else json.dumps(values, separators=(",", ":"))
        signature = hmac.new(
            self.api_secret.encode(),
            f"{timestamp}{self.api_key}{self.recv_window}{query or body_text}".encode(),
            hashlib.sha256,
        ).hexdigest()
        url = f"{self.base_url.rstrip('/')}{path}" + (f"?{query}" if query else "")
        request = Request(url, data=body_text.encode() if body_text else None, method=method, headers={
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "X-BAPI-SIGN": signature,
            "Content-Type": "application/json",
            "User-Agent": "server-bot-bybit-demo/1.0",
        })
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
        except HTTPError as exc:
            raise BybitDemoError(f"Bybit demo HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}") from None
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BybitDemoError(f"Bybit demo request failed: {exc}") from None
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            message = payload.get("retMsg", "unexpected response") if isinstance(payload, dict) else "unexpected response"
            raise BybitDemoError(f"Bybit demo {method} {path} API error: {message}")
        result = payload.get("result", {})
        return result if isinstance(result, dict) else {}

    def instruments(self, symbol: str) -> dict[str, Any]:
        rows = self._request("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": symbol}).get("list", [])
        if not rows:
            raise BybitDemoError(f"Bybit demo returned no instrument data for {symbol}")
        return rows[0]

    def last_price(self, symbol: str) -> Decimal:
        rows = self._request("GET", "/v5/market/tickers", {"category": "linear", "symbol": symbol}).get("list", [])
        if not rows:
            raise BybitDemoError(f"Bybit demo returned no ticker for {symbol}")
        return Decimal(str(rows[0]["lastPrice"]))

    def available_usdt(self) -> Decimal:
        rows = self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"}).get("list", [])
        coins = rows[0].get("coin", []) if rows else []
        coin = next((item for item in coins if item.get("coin") == "USDT"), None)
        return Decimal(str((coin or {}).get("walletBalance", 0)))

    def position(self, symbol: str) -> dict[str, Any] | None:
        rows = self._request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol}).get("list", [])
        return next((row for row in rows if Decimal(str(row.get("size", 0))) > 0), None)

    def market_order(self, symbol: str, side: str, quantity: Decimal, *, reduce_only: bool = False) -> str:
        result = self._request("POST", "/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": side, "orderType": "Market",
            "qty": format(quantity, "f"), "reduceOnly": reduce_only,
        })
        order_id = result.get("orderId")
        if not order_id:
            raise BybitDemoError("Bybit demo returned no order ID")
        return str(order_id)

    def set_protection(self, symbol: str, stop_loss: Decimal, take_profit: Decimal) -> None:
        try:
            self._request("POST", "/v5/position/trading-stop", {
                "category": "linear", "symbol": symbol, "positionIdx": 0, "tpslMode": "Full",
                "stopLoss": format(stop_loss, "f"), "takeProfit": format(take_profit, "f"),
                "slOrderType": "Market", "tpOrderType": "Market",
            })
        except BybitDemoError as exc:
            # Bybit uses an error retCode for an idempotent protection update.
            # Existing exchange-side SL/TP is already the requested protection.
            if "not modified" not in str(exc).lower():
                raise
