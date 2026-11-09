# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of reference bot market."""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, Self, cast, runtime_checkable

from shared.candles import candle_rows
from shared.validation import (
    boolean,
    database_rows,
    json_object,
    numeric_rows,
    numeric_string,
    object_list,
    object_mapping,
    string,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

import pandas as pd
import requests
from websockets.sync.client import connect as websocket_connect

LOGGER = logging.getLogger(__name__)

BYBIT_KLINES_URL = "https://api.bybit.com/v5/market/kline"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
BYBIT_WEBSOCKET_URL = "wss://stream.bybit.com/v5/public/linear"
MEXC_KLINES_URL = "https://api.mexc.com/api/v3/klines"
MEXC_TICKER_URL = "https://api.mexc.com/api/v3/ticker/price"
MEXC_EXCHANGE_INFO_URL = "https://api.mexc.com/api/v3/exchangeInfo"
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
BYBIT_INTERVALS = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}
CANDLE_COLUMNS = [
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_asset_volume",
    "number_of_trades",
]


@runtime_checkable
class KlineConnection(Protocol):
    """Transport needed by the confirmed-candle subscription."""

    def __enter__(self) -> Self:
        """Enter the stream context."""
        ...

    def __exit__(self, *args: object) -> None:
        """Release the stream connection."""
        ...

    def send(self, message: str) -> None:
        """Send a subscription request."""
        ...

    def recv(self, *, timeout: float) -> object:
        """Receive the next stream message."""
        ...


class CandleCache:
    """Persistent, duplicate-safe storage for completed market candles."""

    def __init__(self, path: Path) -> None:
        """Initialize the instance."""
        self.path: Path = path
        self._initialize()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """Commit or roll back each operation and always close its database.

        Yields:
            An open transaction on the candle cache.

        """
        with (
            closing(sqlite3.connect(self.path, timeout=20)) as connection,
            connection,
        ):
            _ = connection.execute("PRAGMA journal_mode=WAL")
            _ = connection.execute("PRAGMA busy_timeout=20000")
            yield connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            _ = connection.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    quote_asset_volume REAL NOT NULL,
                    number_of_trades INTEGER NOT NULL,
                    PRIMARY KEY (symbol, interval, open_time_ms)
                )
                """)

    def upsert(self, symbol: str, interval: str, candles: pd.DataFrame) -> None:
        """Upsert."""
        if candles.empty:
            return
        rows = [
            (
                symbol,
                interval,
                int(row.time.timestamp() * 1000),
                row.open,
                row.high,
                row.low,
                row.close,
                volume,
                quote_volume,
                trades,
            )
            for row, volume, quote_volume, trades in zip(
                candle_rows(candles),
                pd.to_numeric(candles["volume"]).astype(float).tolist(),
                pd.to_numeric(candles["quote_asset_volume"])
                .astype(float)
                .tolist(),
                pd.to_numeric(candles["number_of_trades"]).astype(int).tolist(),
                strict=True,
            )
        ]
        with self._connect() as connection:
            _ = connection.executemany(
                """
                INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, open_time_ms) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    quote_asset_volume=excluded.quote_asset_volume,
                    number_of_trades=excluded.number_of_trades
                """,
                rows,
            )

    def load(
        self, symbol: str, interval: str, *, start_ms: int, end_ms: int
    ) -> pd.DataFrame:
        """Load cached candles in chronological order.

        Returns:
            Candles in the requested half-open time range.

        """
        with self._connect() as connection:
            rows = database_rows(
                connection.execute(
                    """
                SELECT open_time_ms, open, high, low, close, volume,
                    quote_asset_volume, number_of_trades
                FROM candles
                WHERE symbol = ? AND interval = ?
                    AND open_time_ms >= ? AND open_time_ms < ?
                ORDER BY open_time_ms
                """,
                    (symbol, interval, start_ms, end_ms),
                ).fetchall()
            )
        if not rows:
            return pd.DataFrame(columns=CANDLE_COLUMNS)
        frame = pd.DataFrame(
            rows,
            columns=[
                "open_time_ms",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "quote_asset_volume",
                "number_of_trades",
            ],
        )
        frame.insert(
            0,
            "time",
            pd.to_datetime(frame.pop("open_time_ms"), unit="ms", utc=True),
        )
        return frame[CANDLE_COLUMNS]


@dataclass(frozen=True)
class WebSocketHealth:
    """Represent web socket health."""

    connected: bool
    last_message: str | None
    last_completed_candle: str | None
    reconnect_count: int
    connection_generation: int
    last_error: str | None


class RestMarketFeed:
    """Poll completed candles exclusively through REST."""

    def start(self) -> None:
        """Start."""

    def stop(self) -> None:
        """Stop."""

    @staticmethod
    def health() -> WebSocketHealth:
        """Return the inactive stream status.

        Returns:
            A disconnected feed with no stream observations.

        """
        return WebSocketHealth(
            connected=False,
            last_message=None,
            last_completed_candle=None,
            reconnect_count=0,
            connection_generation=0,
            last_error=None,
        )


class BybitKlineWebSocket:
    """Persist confirmed Bybit stream candles through a callback."""

    def __init__(
        self,
        symbol: str,
        interval: str,
        on_candle: Callable[[pd.DataFrame], None],
        *,
        connect: Callable[..., object] = websocket_connect,
    ) -> None:
        """Initialize a confirmed-candle stream.

        Raises:
            ValueError: If the interval is not supported by Bybit.

        """
        if interval not in BYBIT_INTERVALS:
            message = f"Bybit does not support interval {interval!r}."
            raise ValueError(message)
        self.symbol: str = symbol
        self.interval: str = interval
        self.on_candle: Callable[[pd.DataFrame], None] = on_candle
        self._connect: Callable[..., object] = connect
        self._stop: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock: threading.Lock = threading.Lock()
        self._connected: bool = False
        self._last_message: str | None = None
        self._last_completed_candle: str | None = None
        self._reconnect_count: int = 0
        self._connection_generation: int = 0
        self._last_error: str | None = None

    def start(self) -> None:
        """Start."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="bybit-kline-websocket", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop."""
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def health(self) -> WebSocketHealth:
        """Return an atomic snapshot of stream health.

        Returns:
            Connection status and the most recent stream observations.

        """
        with self._lock:
            return WebSocketHealth(
                self._connected,
                self._last_message,
                self._last_completed_candle,
                self._reconnect_count,
                self._connection_generation,
                self._last_error,
            )

    def _set_connection(
        self, *, connected: bool, error: str | None = None
    ) -> None:
        with self._lock:
            self._connected = connected
            self._last_error = error
            if connected:
                self._connection_generation += 1

    def handle_message(self, raw: str) -> None:
        """Validate and publish a confirmed websocket candle."""
        payload = json_object(raw)
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._last_message = now
        if (
            payload.get("topic")
            != f"kline.{BYBIT_INTERVALS[self.interval]}.{self.symbol}"
        ):
            return
        for item in object_list(payload.get("data", [])):
            candle = object_mapping(item)
            if not boolean(candle.get("confirm", False)):
                continue
            frame = pd.DataFrame({
                "time": [
                    pd.to_datetime(
                        int(numeric_string(candle["start"])),
                        unit="ms",
                        utc=True,
                    )
                ],
                "open": [numeric_string(candle["open"])],
                "high": [numeric_string(candle["high"])],
                "low": [numeric_string(candle["low"])],
                "close": [numeric_string(candle["close"])],
                "volume": [numeric_string(candle["volume"])],
                "quote_asset_volume": [
                    numeric_string(candle.get("turnover", 0))
                ],
                "number_of_trades": [0],
            })
            self.on_candle(frame)
            with self._lock:
                self._last_completed_candle = pd.to_datetime(
                    int(numeric_string(candle["start"])), unit="ms", utc=True
                ).isoformat()

    def _consume_connection(self) -> None:
        connection = self._connect(
            BYBIT_WEBSOCKET_URL,
            open_timeout=20,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        )
        if not isinstance(connection, KlineConnection):
            message = "Expected a WebSocket connection"
            raise TypeError(message)
        with connection as socket:
            socket.send(
                json.dumps({
                    "op": "subscribe",
                    "args": [
                        f"kline.{BYBIT_INTERVALS[self.interval]}.{self.symbol}"
                    ],
                })
            )
            self._set_connection(connected=True)
            while not self._stop.is_set():
                try:
                    raw = socket.recv(timeout=1)
                except TimeoutError:
                    continue
                self.handle_message(
                    raw.decode() if isinstance(raw, bytes) else string(raw)
                )

    def _run(self) -> None:
        retry_seconds = 1.0
        while not self._stop.is_set():
            generation = self.health().connection_generation
            try:
                self._consume_connection()
            except Exception as exc:
                LOGGER.exception("Market websocket disconnected; retrying")
                self._set_connection(
                    connected=False, error=f"{type(exc).__name__}: {exc}"
                )
                with self._lock:
                    self._reconnect_count += 1
                if self.health().connection_generation != generation:
                    retry_seconds = 1.0
                if self._stop.wait(
                    retry_seconds
                    + random.SystemRandom().uniform(0, retry_seconds * 0.25)
                ):
                    break
                retry_seconds = min(retry_seconds * 2, 60)
        self._set_connection(connected=False, error=self.health().last_error)


def fetch_live_price(symbol: str) -> float:
    """Fetch the latest Bybit linear price.

    Returns:
        A finite positive quote price.

    Raises:
        ValueError: The exchange rejects the query or returns no positive price.

    """
    response = requests.get(
        BYBIT_TICKERS_URL,
        params={"category": "linear", "symbol": symbol},
        timeout=20,
    )
    response.raise_for_status()
    payload = object_mapping(cast("object", response.json()))
    if payload.get("retCode") != 0:
        message = f"Bybit API error: {payload.get('retMsg', 'unknown error')}"
        raise ValueError(message)
    rows = object_list(
        object_mapping(payload.get("result", {})).get("list", [])
    )
    if not rows or numeric_string(object_mapping(rows[0])["lastPrice"]) <= 0:
        message = f"Bybit returned no valid live price for {symbol}."
        raise ValueError(message)
    return numeric_string(object_mapping(rows[0])["lastPrice"])


def fetch_mexc_live_price(symbol: str) -> float:
    """Fetch the latest MEXC spot price.

    Returns:
        A finite positive quote price.

    Raises:
        ValueError: The exchange returns no positive price.

    """
    response = requests.get(
        MEXC_TICKER_URL, params={"symbol": symbol}, timeout=20
    )
    response.raise_for_status()
    payload = object_mapping(cast("object", response.json()))
    if "price" not in payload or numeric_string(payload["price"]) <= 0:
        message = f"MEXC returned no valid live price for {symbol}."
        raise ValueError(message)
    return numeric_string(payload["price"])


def fetch_mexc_taker_fee_rate(symbol: str) -> Decimal:
    """Return MEXC's published spot taker commission for a symbol.

    Returns:
        A finite nonnegative commission rate.

    Raises:
        ValueError: The exchange returns no valid commission rate.

    """
    response = requests.get(
        MEXC_EXCHANGE_INFO_URL, params={"symbol": symbol}, timeout=20
    )
    response.raise_for_status()
    payload = object_mapping(cast("object", response.json()))
    symbols = [
        object_mapping(item) for item in object_list(payload.get("symbols", []))
    ]
    if not symbols or "takerCommission" not in symbols[0]:
        message = f"MEXC returned no taker commission for {symbol}."
        raise ValueError(message)
    rate = Decimal(str(symbols[0]["takerCommission"]))
    if not rate.is_finite() or rate < 0:
        message = f"MEXC returned an invalid taker commission for {symbol}."
        raise ValueError(message)
    return rate


def reverse_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Invert candle prices and swap base and quote volumes.

    Returns:
        Candles priced in the opposite quote currency.

    Raises:
        ValueError: A candle price is zero or negative.

    """
    result = candles.copy()
    if result.empty:
        return result
    original_high = result["high"].copy()
    original_low = result["low"].copy()
    original_volume = result["volume"].copy()
    if (result[["open", "high", "low", "close"]] <= 0).any().any():
        message = "Cannot reverse candles containing zero or negative prices."
        raise ValueError(message)
    result["open"] = 1 / result["open"]
    result["high"] = 1 / original_low
    result["low"] = 1 / original_high
    result["close"] = 1 / result["close"]
    result["volume"] = result["quote_asset_volume"]
    result["quote_asset_volume"] = original_volume
    return result


def fetch_completed_mexc_klines(
    symbol: str, interval: str, *, start_ms: int, end_ms: int
) -> pd.DataFrame:
    """Fetch completed MEXC candles in chronological order.

    Returns:
        Completed candles with numeric prices and volumes.

    Raises:
        ValueError: The interval is unsupported or response numbers are invalid.

    """
    mexc_intervals = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "1h": "60m",
        "4h": "4h",
        "1d": "1d",
    }
    if interval not in mexc_intervals:
        message = f"MEXC does not support interval {interval!r}."
        raise ValueError(message)
    interval_ms = INTERVAL_MS[interval]
    frames: list[pd.DataFrame] = []
    cursor = start_ms
    while cursor < end_ms:
        page_end = min(end_ms, cursor + interval_ms * 1000 - 1)
        response = requests.get(
            MEXC_KLINES_URL,
            params={
                "symbol": symbol,
                "interval": mexc_intervals[interval],
                "startTime": cursor,
                "endTime": page_end,
                "limit": 1000,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = cast("object", response.json())
        if isinstance(payload, dict):
            error = object_mapping(cast("object", payload))
            if "msg" in error:
                message = f"MEXC API error: {error['msg']}"
                raise ValueError(message)
        rows = numeric_rows(cast("object", payload), 8)
        if not rows:
            break
        frame = pd.DataFrame(
            rows,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_asset_volume",
            ],
        )
        frames.append(frame)
        next_cursor = int(rows[-1][0]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    if not frames:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    result = pd.concat(frames, ignore_index=True)
    numeric = ["open", "high", "low", "close", "volume", "quote_asset_volume"]
    result[numeric] = result[numeric].apply(pd.to_numeric, errors="coerce")
    result["open_time"] = pd.to_numeric(result["open_time"])
    completed = result["open_time"] <= end_ms - interval_ms
    result = result.loc[completed, :]
    result["time"] = pd.to_datetime(result["open_time"], unit="ms", utc=True)
    result["number_of_trades"] = 0
    result = (
        result.drop_duplicates(subset="time")
        .sort_values("time")
        .reset_index(drop=True)
    )
    return result[CANDLE_COLUMNS]


def fetch_completed_linear_klines(
    symbol: str, interval: str, *, start_ms: int, end_ms: int
) -> pd.DataFrame:
    """Fetch completed Bybit linear candles in chronological order.

    Returns:
        Completed candles with numeric prices and volumes.

    Raises:
        ValueError: The interval is unsupported or exchange data is invalid.

    """
    if interval not in BYBIT_INTERVALS:
        message = f"Bybit does not support interval {interval!r}."
        raise ValueError(message)
    rows: list[list[float]] = []
    cursor_end = end_ms
    while cursor_end > start_ms:
        response = requests.get(
            BYBIT_KLINES_URL,
            params={
                "category": "linear",
                "symbol": symbol,
                "interval": BYBIT_INTERVALS[interval],
                "start": start_ms,
                "end": cursor_end,
                "limit": 1000,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = object_mapping(cast("object", response.json()))
        if payload.get("retCode") != 0:
            message = (
                f"Bybit API error: {payload.get('retMsg', 'unknown error')}"
            )
            raise ValueError(message)
        page = numeric_rows(
            object_mapping(payload.get("result", {})).get("list", []), 7
        )
        if not page:
            break
        rows.extend(page)
        next_end = min(int(row[0]) for row in page) - 1
        if next_end >= cursor_end:
            break
        cursor_end = next_end
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(
        rows,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_asset_volume",
        ],
    )
    numeric = ["open", "high", "low", "close", "volume", "quote_asset_volume"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame["open_time"] = pd.to_numeric(frame["open_time"])
    completed = frame["open_time"] <= end_ms - INTERVAL_MS[interval]
    frame = frame.loc[completed, :]
    frame["time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame["number_of_trades"] = 0
    frame = (
        frame.drop_duplicates(subset="time")
        .sort_values("time")
        .reset_index(drop=True)
    )
    return frame[[
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_asset_volume",
        "number_of_trades",
    ]]
