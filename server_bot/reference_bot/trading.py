# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of reference bot trading."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import pandas as pd
import requests

from shared.validation import (
    json_object,
    numeric_string,
    object_list,
    object_mapping,
    string,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from shared.models import Trade

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPER_ACCOUNT_FILE = PROJECT_ROOT / "reference_account.json"
BYBIT_ENV_FILES = (PROJECT_ROOT / "bybitapi.env",)
BYBIT_FEE_RATE_URL = "https://api.bybit.com/v5/account/fee-rate"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
BTC_QUANTITY_STEP = Decimal("0.001")


@dataclass(frozen=True, kw_only=True)
class OrderLimits:
    """Exchange quantity grid and minimum accepted order sizes."""

    quantity_step: Decimal = BTC_QUANTITY_STEP
    minimum_quantity: Decimal = BTC_QUANTITY_STEP
    exchange_minimum_notional: Decimal = Decimal(0)


DEFAULT_LIMITS = OrderLimits()
ZERO_FEE = Decimal(0)


@dataclass(frozen=True)
class PaperTradeTarget:
    """Represent paper trade target."""

    side: str | None
    price: float
    quantity: Decimal


@dataclass(frozen=True)
class LocalPosition:
    """Represent local position."""

    symbol: str
    side: str | None
    quantity: Decimal
    entry_price: float | None
    updated_at: str | None
    entry_fee_rate: Decimal = Decimal(0)
    entry_fee: Decimal = Decimal(0)

    def unrealized_pnl(self, current_price: float) -> float:
        """Unrealized pnl.

        Returns:
            The unrealized pnl result.

        """
        if self.side is None or self.entry_price is None:
            return 0.0
        direction = 1 if self.side == "Long" else -1
        gross = (
            direction
            * (current_price - self.entry_price)
            * float(self.quantity)
        )
        return gross - float(self.entry_fee)


def _credential_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name, value = stripped.split("=", 1)
            values[name.strip()] = value.strip().strip("\"'")
    return values


def load_bybit_credentials() -> tuple[str, str]:
    """Load Mainnet credentials used only for the read-only fee-rate request.

    Returns:
        The load bybit credentials result.

    """
    values: dict[str, str] = {}
    for path in BYBIT_ENV_FILES:
        if path.is_file():
            values = _credential_values(path)
            break
    key = values.get("BYBIT_API_KEY") or os.getenv("BYBIT_API_KEY", "")
    secret = values.get("BYBIT_API_SECRET") or os.getenv("BYBIT_API_SECRET", "")
    return key.strip(), secret.strip()


class FeeResponse(Protocol):
    """Represent feeresponse."""

    def raise_for_status(self) -> None:
        """Handle raise for status."""
        ...

    def json(self) -> object:
        """Handle json."""
        ...


class FeeRequest(Protocol):
    """Represent feerequest."""

    def __call__(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: int,
    ) -> FeeResponse:
        """Handle call."""
        ...


def fetch_bybit_taker_fee_rate(
    api_key: str,
    api_secret: str,
    symbol: str,
    *,
    timestamp_ms: int | None = None,
    request_get: FeeRequest = requests.get,
) -> Decimal:
    """Read the account-specific mainnet taker fee with a signed request.

    Returns:
        The calculated decimal amount.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.
        ValueError: If the requested operation cannot safely proceed.

    """
    if not api_key or not api_secret:
        message = (
            "BYBIT_API_KEY and BYBIT_API_SECRET are required to read the live"
            " Bybit fee rate."
        )
        raise ValueError(message)
    timestamp = str(
        timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    )
    recv_window = "5000"
    query = f"category=linear&symbol={symbol}"
    signature_payload = f"{timestamp}{api_key}{recv_window}{query}"
    signature = hmac.new(
        api_secret.encode(), signature_payload.encode(), hashlib.sha256
    ).hexdigest()
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
    payload = object_mapping(response.json())
    if payload.get("retCode") != 0:
        message = (
            "Bybit fee-rate request failed:"
            f" {payload.get('retMsg', 'unknown error')}"
        )
        raise RuntimeError(message)
    rates = object_list(
        object_mapping(payload.get("result", {})).get("list", [])
    )
    if not rates:
        message = f"Bybit returned no linear fee rate for {symbol}."
        raise RuntimeError(message)
    rate = Decimal(str(object_mapping(rates[0])["takerFeeRate"]))
    if rate < 0:
        message = "Bybit returned an invalid taker fee rate."
        raise RuntimeError(message)
    return rate


@lru_cache(maxsize=32)
def fetch_bybit_order_limits(symbol: str) -> tuple[Decimal, Decimal, Decimal]:
    """Read the linear contract quantity grid and minimum notional.

    Returns:
        The fetch bybit order limits result.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.

    """
    response = requests.get(
        BYBIT_INSTRUMENTS_URL,
        params={"category": "linear", "symbol": symbol},
        timeout=20,
    )
    response.raise_for_status()
    payload = object_mapping(cast("object", response.json()))
    if payload.get("retCode") != 0:
        message = (
            "Bybit instrument request failed:"
            f" {payload.get('retMsg', 'unknown error')}"
        )
        raise RuntimeError(message)
    rows = object_list(
        object_mapping(payload.get("result", {})).get("list", [])
    )
    if not rows:
        message = f"Bybit returned no instrument limits for {symbol}."
        raise RuntimeError(message)
    limits = object_mapping(object_mapping(rows[0]).get("lotSizeFilter", {}))
    try:
        step = Decimal(str(limits["qtyStep"]))
        minimum = Decimal(str(limits["minOrderQty"]))
        minimum_notional = Decimal(str(limits.get("minNotionalValue", "0")))
    except (KeyError, ValueError) as exc:
        message = f"Bybit returned invalid instrument limits for {symbol}."
        raise RuntimeError(message) from exc
    if step <= 0 or minimum <= 0 or minimum_notional < 0:
        message = f"Bybit returned invalid instrument limits for {symbol}."
        raise RuntimeError(message)
    return step, minimum, minimum_notional


def latest_strategy_side(
    trades: list[Trade], final_candle_time: pd.Timestamp
) -> str | None:
    """Read the position before the artificial end-of-range close.

    Returns:
        The action description, or None when no action is needed.

    """
    if not trades:
        return None
    last_trade = trades[-1]
    if last_trade.exit_reason == "End of strategy" and pd.Timestamp(
        last_trade.exit_time
    ) == pd.Timestamp(final_candle_time):
        return last_trade.side
    return None


class LocalPaperTrader:
    """Persist simulated positions locally; never submit an exchange order."""

    def __init__(self, account_path: Path = PAPER_ACCOUNT_FILE) -> None:
        """Initialize the instance."""
        self.account_path: Path = account_path
        self.lock_path: Path = account_path.with_suffix(
            account_path.suffix + ".lock"
        )

    @contextmanager
    def _account_lock(self) -> Generator[None]:
        """Serialize ledger changes across Streamlit sessions and processes."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_payload(self) -> dict[str, object]:
        if not self.account_path.is_file():
            return {"positions": {}, "history": []}
        try:
            return json_object(self.account_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            message = (
                f"Could not read local paper account {self.account_path.name}:"
                f" {exc}"
            )
            raise RuntimeError(message) from exc

    @staticmethod
    def _position_from_payload(
        payload: dict[str, object], symbol: str
    ) -> LocalPosition:
        raw_position = object_mapping(payload.get("positions", {})).get(symbol)
        position = object_mapping(raw_position) if raw_position else {}
        if not position or position.get("side") not in {"Long", "Short"}:
            return LocalPosition(symbol, None, Decimal(0), None, None)
        try:
            return LocalPosition(
                symbol=symbol,
                side=string(position["side"]),
                quantity=Decimal(str(position["quantity"])),
                entry_price=numeric_string(position["entry_price"]),
                updated_at=(
                    string(position["updated_at"])
                    if position.get("updated_at") is not None
                    else None
                ),
                entry_fee_rate=Decimal(
                    str(position.get("entry_fee_rate", "0"))
                ),
                entry_fee=Decimal(str(position.get("entry_fee", "0"))),
            )
        except (ValueError, TypeError, KeyError) as exc:
            message = f"Invalid local paper position for {symbol}: {exc}"
            raise RuntimeError(message) from exc

    def _write_payload(self, payload: dict[str, object]) -> None:
        """Replace the ledger atomically to prevent partial JSON.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        temporary_path = self.account_path.with_suffix(
            self.account_path.suffix + ".tmp"
        )
        try:
            _ = temporary_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            _ = Path(temporary_path).replace(self.account_path)
        except OSError as exc:
            message = (
                f"Could not write local paper account {self.account_path.name}:"
                f" {exc}"
            )
            raise RuntimeError(message) from exc

    @staticmethod
    def build_target(
        symbol: str,
        side: str | None,
        price: float,
        *,
        notional: float,
        limits: OrderLimits = DEFAULT_LIMITS,
    ) -> PaperTradeTarget:
        """Build target.

        Returns:
            The quantity rounded to the exchange grid.

        Raises:
            ValueError: If the requested operation cannot safely proceed.

        """
        if side not in {None, "Long", "Short"}:
            message = "Target side must be Long, Short, or flat."
            raise ValueError(message)
        if price <= 0 or notional <= 0:
            message = "Price and paper allocation must be positive."
            raise ValueError(message)
        raw_quantity = Decimal(str(notional)) / Decimal(str(price))
        if (
            limits.quantity_step <= 0
            or limits.minimum_quantity <= 0
            or limits.exchange_minimum_notional < 0
        ):
            message = "Exchange order limits must be valid."
            raise ValueError(message)
        quantity = (raw_quantity / limits.quantity_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * limits.quantity_step
        if side is not None and (
            quantity < limits.minimum_quantity
            or quantity * Decimal(str(price)) < limits.exchange_minimum_notional
        ):
            required = max(
                limits.exchange_minimum_notional,
                limits.minimum_quantity * Decimal(str(price)),
            )
            message = (
                f"Paper allocation is too small; {symbol} requires at least"
                f" {limits.minimum_quantity} units and approximately {required}"
                " quote notional."
            )
            raise ValueError(message)
        return PaperTradeTarget(side=side, price=price, quantity=quantity)

    def current_position(self, symbol: str) -> LocalPosition:
        """Read the current position.

        Returns:
            The persisted position, or a flat position when absent.

        """
        return self._position_from_payload(self._load_payload(), symbol)

    def trade_history(self, symbol: str) -> list[dict[str, object]]:
        """Return completed local paper trades, newest first.

        Returns:
            The trade history result.

        """
        history = self._history(self._load_payload())
        return [
            item for item in reversed(history) if item.get("symbol") == symbol
        ]

    def _history(self, payload: dict[str, object]) -> list[dict[str, object]]:
        try:
            return [
                object_mapping(item)
                for item in object_list(payload.get("history", []))
            ]
        except (TypeError, ValueError) as exc:
            message = f"Invalid trade history in {self.account_path.name}."
            raise RuntimeError(message) from exc

    def available_capital(
        self,
        symbol: str,
        initial_capital: float,
        *,
        closing_price: float | None = None,
        taker_fee_rate: Decimal = ZERO_FEE,
    ) -> Decimal:
        """Calculate equity after realized trades and an optional final close.

        Returns:
            The calculated decimal amount.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.
            ValueError: If the requested operation cannot safely proceed.

        """
        if initial_capital <= 0:
            message = "Initial paper capital must be positive."
            raise ValueError(message)
        if taker_fee_rate < 0:
            message = "Taker fee rate cannot be negative."
            raise ValueError(message)
        payload = self._load_payload()
        history = self._history(payload)
        capital = Decimal(str(initial_capital))
        try:
            capital += sum(
                (
                    Decimal(str(trade["pnl"]))
                    for trade in history
                    if trade.get("symbol") == symbol
                ),
                Decimal(0),
            )
        except (KeyError, ValueError, TypeError) as exc:
            message = (
                f"Invalid trade history in {self.account_path.name}: {exc}"
            )
            raise RuntimeError(message) from exc

        current = self._position_from_payload(payload, symbol)
        if (
            closing_price is not None
            and current.side is not None
            and current.entry_price is not None
        ):
            if closing_price <= 0:
                message = "Closing price must be positive."
                raise ValueError(message)
            direction = Decimal(1) if current.side == "Long" else Decimal(-1)
            price = Decimal(str(closing_price))
            gross_pnl = (
                direction
                * (price - Decimal(str(current.entry_price)))
                * current.quantity
            )
            exit_fee = current.quantity * price * taker_fee_rate
            capital += gross_pnl - current.entry_fee - exit_fee
        return capital

    def strategy_started_at(self, default_start: datetime) -> datetime:
        """Persist the independent paper strategy start and anchor time.

        Returns:
            The timezone-aware strategy timestamp.

        """
        with self._account_lock():
            payload = self._load_payload()
            stored = payload.get("strategy_started_at")
            if stored:
                return datetime.fromisoformat(str(stored)).astimezone(UTC)
            started_at = default_start.astimezone(UTC)
            payload["strategy_started_at"] = started_at.isoformat()
            self._write_payload(payload)
            return started_at

    def reset_strategy_start(self, started_at: datetime) -> datetime:
        """Change the paper VWAP anchor while preserving trading history.

        Returns:
            The timezone-aware strategy timestamp.

        """
        with self._account_lock():
            payload = self._load_payload()
            normalized = started_at.astimezone(UTC)
            payload["strategy_started_at"] = normalized.isoformat()
            self._write_payload(payload)
            return normalized

    @staticmethod
    def _closing_trade(
        current: LocalPosition,
        target: PaperTradeTarget,
        *,
        taker_fee_rate: Decimal,
        now: str,
        exit_reason: str | None,
    ) -> dict[str, object]:
        if current.entry_price is None:
            message = "An open position must have an entry price."
            raise ValueError(message)
        direction = Decimal(1) if current.side == "Long" else Decimal(-1)
        exit_notional = current.quantity * Decimal(str(target.price))
        exit_fee = exit_notional * taker_fee_rate
        gross_pnl = (
            direction
            * (Decimal(str(target.price)) - Decimal(str(current.entry_price)))
            * current.quantity
        )
        net_pnl = gross_pnl - current.entry_fee - exit_fee
        deposit_size = current.quantity * Decimal(str(current.entry_price))
        return {
            "symbol": current.symbol,
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
            "return_pct": (
                str(net_pnl / deposit_size * Decimal(100))
                if deposit_size
                else "0"
            ),
            "exit_reason": (
                exit_reason
                or (
                    "Strategy reversed"
                    if target.side is not None
                    else "Strategy target flat"
                )
            ),
        }

    def reconcile(
        self,
        symbol: str,
        target: PaperTradeTarget,
        *,
        taker_fee_rate: Decimal,
        exit_reason: str | None = None,
        executed_at: datetime | None = None,
    ) -> str | None:
        """Reconcile.

        Returns:
            The action description, or None when no action is needed.

        Raises:
            ValueError: If the requested operation cannot safely proceed.

        """
        if taker_fee_rate < 0:
            message = "Taker fee rate cannot be negative."
            raise ValueError(message)
        with self._account_lock():
            payload = self._load_payload()
            current = self._position_from_payload(payload, symbol)
            # Strategy orders are transitions. Repeated Long/Short signals must
            # not
            # resize a position merely because the live sizing price has
            # changed.
            if current.side == target.side:
                return None
            positions = object_mapping(payload.get("positions", {}))
            history = self._history(payload)
            payload["positions"] = positions
            payload["history"] = history
            execution_time = executed_at or datetime.now(UTC)
            if execution_time.tzinfo is None:
                message = "Execution time must be timezone-aware."
                raise ValueError(message)
            now = execution_time.astimezone(UTC).isoformat()
            if current.side is not None and current.entry_price is not None:
                history.append(
                    self._closing_trade(
                        current,
                        target,
                        taker_fee_rate=taker_fee_rate,
                        now=now,
                        exit_reason=exit_reason,
                    )
                )
            if target.side is None:
                _ = positions.pop(symbol, None)
                action = f"Closed local {current.side or 'flat'} position"
            else:
                entry_fee = (
                    target.quantity
                    * Decimal(str(target.price))
                    * taker_fee_rate
                )
                position = LocalPosition(
                    symbol,
                    target.side,
                    target.quantity,
                    target.price,
                    now,
                    taker_fee_rate,
                    entry_fee,
                )
                positions[symbol] = {
                    **asdict(position),
                    "quantity": str(position.quantity),
                    "entry_fee_rate": str(position.entry_fee_rate),
                    "entry_fee": str(position.entry_fee),
                }
                action = (
                    f"Set local {target.side} position to {target.quantity}"
                    f" {symbol}"
                )
            self._write_payload(payload)
            return action
