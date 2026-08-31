from __future__ import annotations

import json
import hashlib
import hmac
import fcntl
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Callable, Any

import pandas as pd
import requests

from shared.models import Trade


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPER_ACCOUNT_FILE = PROJECT_ROOT / "reference_account.json"
BYBIT_ENV_FILES = (PROJECT_ROOT / "bybitapi.env",)
BYBIT_FEE_RATE_URL = "https://api.bybit.com/v5/account/fee-rate"
BTC_QUANTITY_STEP = Decimal("0.001")


@dataclass(frozen=True)
class PaperTradeTarget:
    side: str | None
    price: float
    quantity: Decimal


@dataclass(frozen=True)
class LocalPosition:
    symbol: str
    side: str | None
    quantity: Decimal
    entry_price: float | None
    updated_at: str | None
    entry_fee_rate: Decimal = Decimal("0")
    entry_fee: Decimal = Decimal("0")

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side is None or self.entry_price is None:
            return 0.0
        direction = 1 if self.side == "Long" else -1
        gross = direction * (current_price - self.entry_price) * float(self.quantity)
        return gross - float(self.entry_fee)


def load_bybit_credentials() -> tuple[str, str]:
    """Load Mainnet credentials used only for the read-only fee-rate request."""
    values: dict[str, str] = {}
    for path in BYBIT_ENV_FILES:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    name, value = stripped.split("=", 1)
                    values[name.strip()] = value.strip().strip("\"'")
            break
    key = values.get("BYBIT_API_KEY") or os.getenv("BYBIT_API_KEY", "")
    secret = values.get("BYBIT_API_SECRET") or os.getenv("BYBIT_API_SECRET", "")
    return key.strip(), secret.strip()


def fetch_bybit_taker_fee_rate(
    api_key: str,
    api_secret: str,
    symbol: str,
    *,
    timestamp_ms: int | None = None,
    request_get: Callable[..., Any] = requests.get,
) -> Decimal:
    """Fetch the account-specific Mainnet linear taker fee using a signed read-only request."""
    if not api_key or not api_secret:
        raise ValueError("BYBIT_API_KEY and BYBIT_API_SECRET are required to read the live Bybit fee rate.")
    timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    recv_window = "5000"
    query = f"category=linear&symbol={symbol}"
    signature_payload = f"{timestamp}{api_key}{recv_window}{query}"
    signature = hmac.new(api_secret.encode(), signature_payload.encode(), hashlib.sha256).hexdigest()
    response = request_get(
        BYBIT_FEE_RATE_URL,
        params={"category": "linear", "symbol": symbol},
        headers={
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit fee-rate request failed: {payload.get('retMsg', 'unknown error')}")
    rates = payload.get("result", {}).get("list", [])
    if not rates:
        raise RuntimeError(f"Bybit returned no linear fee rate for {symbol}.")
    rate = Decimal(str(rates[0]["takerFeeRate"]))
    if rate < 0:
        raise RuntimeError("Bybit returned an invalid taker fee rate.")
    return rate


def latest_strategy_side(trades: list[Trade], final_candle_time: pd.Timestamp) -> str | None:
    """Return the position held before the backtest's artificial end-of-range close."""
    if not trades:
        return None
    last_trade = trades[-1]
    if last_trade.exit_reason == "End of strategy" and pd.Timestamp(last_trade.exit_time) == pd.Timestamp(
        final_candle_time
    ):
        return last_trade.side
    return None


class LocalPaperTrader:
    """Persist simulated positions locally; never submit an exchange order."""

    def __init__(self, account_path: Path = PAPER_ACCOUNT_FILE) -> None:
        self.account_path = account_path
        self.lock_path = account_path.with_suffix(account_path.suffix + ".lock")

    @contextmanager
    def _account_lock(self):
        """Serialize ledger changes across Streamlit sessions and processes."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_payload(self) -> dict[str, Any]:
        if not self.account_path.is_file():
            return {"positions": {}, "history": []}
        try:
            payload = json.loads(self.account_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("account root must be an object")
            return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read local paper account {self.account_path.name}: {exc}") from exc

    @staticmethod
    def _position_from_payload(payload: dict[str, Any], symbol: str) -> LocalPosition:
        position = payload.get("positions", {}).get(symbol)
        if not position or position.get("side") not in {"Long", "Short"}:
            return LocalPosition(symbol, None, Decimal("0"), None, None)
        try:
            return LocalPosition(
                symbol=symbol,
                side=position["side"],
                quantity=Decimal(str(position["quantity"])),
                entry_price=float(position["entry_price"]),
                updated_at=position.get("updated_at"),
                entry_fee_rate=Decimal(str(position.get("entry_fee_rate", "0"))),
                entry_fee=Decimal(str(position.get("entry_fee", "0"))),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(f"Invalid local paper position for {symbol}: {exc}") from exc

    def _write_payload(self, payload: dict[str, Any]) -> None:
        """Atomically replace the ledger so interruptions cannot leave partial JSON."""
        temporary_path = self.account_path.with_suffix(self.account_path.suffix + ".tmp")
        try:
            temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary_path, self.account_path)
        except OSError as exc:
            raise RuntimeError(f"Could not write local paper account {self.account_path.name}: {exc}") from exc

    def build_target(self, symbol: str, side: str | None, price: float, notional: float) -> PaperTradeTarget:
        if side not in {None, "Long", "Short"}:
            raise ValueError("Target side must be Long, Short, or flat.")
        if price <= 0 or notional <= 0:
            raise ValueError("Price and paper allocation must be positive.")
        raw_quantity = Decimal(str(notional)) / Decimal(str(price))
        quantity = (raw_quantity / BTC_QUANTITY_STEP).to_integral_value(rounding=ROUND_DOWN) * BTC_QUANTITY_STEP
        if side is not None and quantity < BTC_QUANTITY_STEP:
            raise ValueError(f"Paper allocation is too small; minimum {symbol} quantity is {BTC_QUANTITY_STEP}.")
        return PaperTradeTarget(side=side, price=price, quantity=quantity)

    def current_position(self, symbol: str) -> LocalPosition:
        return self._position_from_payload(self._load_payload(), symbol)

    def trade_history(self, symbol: str) -> list[dict[str, Any]]:
        """Return completed local paper trades, newest first."""
        history = self._load_payload().get("history", [])
        if not isinstance(history, list):
            raise RuntimeError(f"Invalid trade history in {self.account_path.name}.")
        return [item for item in reversed(history) if isinstance(item, dict) and item.get("symbol") == symbol]

    def available_capital(
        self,
        symbol: str,
        initial_capital: float,
        *,
        closing_price: float | None = None,
        taker_fee_rate: Decimal = Decimal("0"),
    ) -> Decimal:
        """Return initial capital plus realized P&L, optionally after closing the open position."""
        if initial_capital <= 0:
            raise ValueError("Initial paper capital must be positive.")
        if taker_fee_rate < 0:
            raise ValueError("Taker fee rate cannot be negative.")
        payload = self._load_payload()
        history = payload.get("history", [])
        if not isinstance(history, list):
            raise RuntimeError(f"Invalid trade history in {self.account_path.name}.")
        capital = Decimal(str(initial_capital))
        try:
            capital += sum(
                (Decimal(str(trade["pnl"])) for trade in history if trade.get("symbol") == symbol),
                Decimal("0"),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError(f"Invalid trade history in {self.account_path.name}: {exc}") from exc

        current = self._position_from_payload(payload, symbol)
        if closing_price is not None and current.side is not None and current.entry_price is not None:
            if closing_price <= 0:
                raise ValueError("Closing price must be positive.")
            direction = Decimal("1") if current.side == "Long" else Decimal("-1")
            price = Decimal(str(closing_price))
            gross_pnl = direction * (price - Decimal(str(current.entry_price))) * current.quantity
            exit_fee = current.quantity * price * taker_fee_rate
            capital += gross_pnl - current.entry_fee - exit_fee
        return capital

    def strategy_started_at(self, default_start: datetime) -> datetime:
        """Persist one independent start/anchor time for the live paper strategy."""
        with self._account_lock():
            payload = self._load_payload()
            stored = payload.get("strategy_started_at")
            if stored:
                return datetime.fromisoformat(str(stored)).astimezone(timezone.utc)
            started_at = default_start.astimezone(timezone.utc)
            payload["strategy_started_at"] = started_at.isoformat()
            self._write_payload(payload)
            return started_at

    def reset_strategy_start(self, started_at: datetime) -> datetime:
        """Change the paper VWAP anchor without changing the current position or history."""
        with self._account_lock():
            payload = self._load_payload()
            normalized = started_at.astimezone(timezone.utc)
            payload["strategy_started_at"] = normalized.isoformat()
            self._write_payload(payload)
            return normalized

    def reconcile(
        self,
        symbol: str,
        target: PaperTradeTarget,
        taker_fee_rate: Decimal,
        exit_reason: str | None = None,
        executed_at: datetime | None = None,
    ) -> str | None:
        if taker_fee_rate < 0:
            raise ValueError("Taker fee rate cannot be negative.")
        with self._account_lock():
            payload = self._load_payload()
            current = self._position_from_payload(payload, symbol)
            # Strategy orders are transitions. Repeated Long/Short signals must not
            # resize a position merely because the live sizing price has changed.
            if current.side == target.side:
                return None
            positions = payload.setdefault("positions", {})
            history = payload.setdefault("history", [])
            execution_time = executed_at or datetime.now(timezone.utc)
            if execution_time.tzinfo is None:
                raise ValueError("Execution time must be timezone-aware.")
            now = execution_time.astimezone(timezone.utc).isoformat()
            if current.side is not None and current.entry_price is not None:
                direction = Decimal("1") if current.side == "Long" else Decimal("-1")
                exit_notional = current.quantity * Decimal(str(target.price))
                exit_fee = exit_notional * taker_fee_rate
                gross_pnl = direction * (Decimal(str(target.price)) - Decimal(str(current.entry_price))) * current.quantity
                net_pnl = gross_pnl - current.entry_fee - exit_fee
                deposit_size = current.quantity * Decimal(str(current.entry_price))
                history.append(
                    {
                        "symbol": symbol,
                        "side": current.side,
                        "quantity": str(current.quantity),
                        "deposit_size": str(deposit_size),
                        "entry_time": current.updated_at,
                        "entry_price": current.entry_price,
                        "exit_time": now,
                        "exit_price": target.price,
                        "entry_fee": str(current.entry_fee),
                        "entry_fee_rate": str(current.entry_fee_rate),
                        "exit_fee": str(exit_fee),
                        "exit_fee_rate": str(taker_fee_rate),
                        "pnl": str(net_pnl),
                        "return_pct": str(net_pnl / deposit_size * Decimal("100")) if deposit_size else "0",
                        "exit_reason": exit_reason or (
                            "Strategy reversed" if target.side is not None else "Strategy target flat"
                        ),
                    }
                )
            if target.side is None:
                positions.pop(symbol, None)
                action = f"Closed local {current.side or 'flat'} position"
            else:
                entry_fee = target.quantity * Decimal(str(target.price)) * taker_fee_rate
                position = LocalPosition(symbol, target.side, target.quantity, target.price, now, taker_fee_rate, entry_fee)
                positions[symbol] = {
                    **asdict(position),
                    "quantity": str(position.quantity),
                    "entry_fee_rate": str(position.entry_fee_rate),
                    "entry_fee": str(position.entry_fee),
                }
                action = f"Set local {target.side} position to {target.quantity} {symbol}"
            self._write_payload(payload)
            return action
