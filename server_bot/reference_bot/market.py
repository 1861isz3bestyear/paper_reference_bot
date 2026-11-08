from __future__ import annotations

import sqlite3
import json
import random
import threading
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import requests
from websockets.sync.client import connect as websocket_connect


BYBIT_KLINES_URL = "https://api.bybit.com/v5/market/kline"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
BYBIT_WEBSOCKET_URL = "wss://stream.bybit.com/v5/public/linear"
MEXC_KLINES_URL = "https://api.mexc.com/api/v3/klines"
MEXC_TICKER_URL = "https://api.mexc.com/api/v3/ticker/price"
MEXC_EXCHANGE_INFO_URL = "https://api.mexc.com/api/v3/exchangeInfo"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
BYBIT_INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
CANDLE_COLUMNS = [
    "time", "open", "high", "low", "close", "volume", "quote_asset_volume", "number_of_trades"
]


class CandleCache:
    """Persistent, duplicate-safe storage for completed market candles."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=20000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
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
                """
            )

    def upsert(self, symbol: str, interval: str, candles: pd.DataFrame) -> None:
        if candles.empty:
            return
        rows = [
            (
                symbol,
                interval,
                int(pd.Timestamp(row.time).timestamp() * 1000),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume),
                float(row.quote_asset_volume),
                int(row.number_of_trades),
            )
            for row in candles.itertuples(index=False)
        ]
        with self._connect() as connection:
            connection.executemany(
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

    def load(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT open_time_ms, open, high, low, close, volume, quote_asset_volume, number_of_trades
                FROM candles
                WHERE symbol = ? AND interval = ? AND open_time_ms >= ? AND open_time_ms < ?
                ORDER BY open_time_ms
                """,
                (symbol, interval, start_ms, end_ms),
            ).fetchall()
        if not rows:
            return pd.DataFrame(columns=CANDLE_COLUMNS)
        frame = pd.DataFrame(
            rows,
            columns=[
                "open_time_ms", "open", "high", "low", "close", "volume", "quote_asset_volume",
                "number_of_trades",
            ],
        )
        frame.insert(0, "time", pd.to_datetime(frame.pop("open_time_ms"), unit="ms", utc=True))
        return frame[CANDLE_COLUMNS]


@dataclass(frozen=True)
class WebSocketHealth:
    connected: bool
    last_message: str | None
    last_completed_candle: str | None
    reconnect_count: int
    connection_generation: int
    last_error: str | None


class RestMarketFeed:
    """No-op feed used when completed candles are polled exclusively through REST."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def health(self) -> WebSocketHealth:
        return WebSocketHealth(False, None, None, 0, 0, None)


class BybitKlineWebSocket:
    """Background Bybit kline stream that persists confirmed candles through a callback."""

    def __init__(
        self,
        symbol: str,
        interval: str,
        on_candle: Callable[[pd.DataFrame], None],
        connect: Callable[..., object] = websocket_connect,
    ) -> None:
        if interval not in BYBIT_INTERVALS:
            raise ValueError(f"Bybit does not support interval {interval!r}.")
        self.symbol = symbol
        self.interval = interval
        self.on_candle = on_candle
        self._connect = connect
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._connected = False
        self._last_message: str | None = None
        self._last_completed_candle: str | None = None
        self._reconnect_count = 0
        self._connection_generation = 0
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bybit-kline-websocket", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def health(self) -> WebSocketHealth:
        with self._lock:
            return WebSocketHealth(
                self._connected,
                self._last_message,
                self._last_completed_candle,
                self._reconnect_count,
                self._connection_generation,
                self._last_error,
            )

    def _set_connection(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            self._connected = connected
            self._last_error = error
            if connected:
                self._connection_generation += 1

    def _handle_message(self, raw: str) -> None:
        payload = json.loads(raw)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._last_message = now
        if payload.get("topic") != f"kline.{BYBIT_INTERVALS[self.interval]}.{self.symbol}":
            return
        for candle in payload.get("data", []):
            if not candle.get("confirm"):
                continue
            frame = pd.DataFrame(
                {
                    "time": [pd.to_datetime(int(candle["start"]), unit="ms", utc=True)],
                    "open": [float(candle["open"])],
                    "high": [float(candle["high"])],
                    "low": [float(candle["low"])],
                    "close": [float(candle["close"])],
                    "volume": [float(candle["volume"])],
                    "quote_asset_volume": [float(candle.get("turnover", 0))],
                    "number_of_trades": [0],
                }
            )
            self.on_candle(frame)
            with self._lock:
                self._last_completed_candle = pd.Timestamp(frame.iloc[0]["time"]).isoformat()

    def _run(self) -> None:
        retry_seconds = 1.0
        while not self._stop.is_set():
            try:
                with self._connect(
                    BYBIT_WEBSOCKET_URL,
                    open_timeout=20,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as socket:
                    socket.send(json.dumps({
                        "op": "subscribe",
                        "args": [f"kline.{BYBIT_INTERVALS[self.interval]}.{self.symbol}"],
                    }))
                    self._set_connection(True)
                    retry_seconds = 1.0
                    while not self._stop.is_set():
                        try:
                            self._handle_message(socket.recv(timeout=1))
                        except TimeoutError:
                            continue
            except Exception as exc:  # The worker must survive all transport/protocol failures.
                self._set_connection(False, f"{type(exc).__name__}: {exc}")
                with self._lock:
                    self._reconnect_count += 1
                if self._stop.wait(retry_seconds + random.uniform(0, retry_seconds * 0.25)):
                    break
                retry_seconds = min(retry_seconds * 2, 60)
        self._set_connection(False, self.health().last_error)


def fetch_live_price(symbol: str) -> float:
    response = requests.get(BYBIT_TICKERS_URL, params={"category": "linear", "symbol": symbol}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") != 0:
        raise ValueError(f"Bybit API error: {payload.get('retMsg', 'unknown error')}")
    rows = payload.get("result", {}).get("list", [])
    if not rows or float(rows[0]["lastPrice"]) <= 0:
        raise ValueError(f"Bybit returned no valid live price for {symbol}.")
    return float(rows[0]["lastPrice"])


def fetch_mexc_live_price(symbol: str) -> float:
    response = requests.get(MEXC_TICKER_URL, params={"symbol": symbol}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if "price" not in payload or float(payload["price"]) <= 0:
        raise ValueError(f"MEXC returned no valid live price for {symbol}.")
    return float(payload["price"])


def fetch_mexc_taker_fee_rate(symbol: str) -> Decimal:
    """Return MEXC's published spot taker commission for a symbol."""
    response = requests.get(MEXC_EXCHANGE_INFO_URL, params={"symbol": symbol}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    symbols = payload.get("symbols", [])
    if not symbols or "takerCommission" not in symbols[0]:
        raise ValueError(f"MEXC returned no taker commission for {symbol}.")
    rate = Decimal(str(symbols[0]["takerCommission"]))
    if rate < 0:
        raise ValueError(f"MEXC returned an invalid taker commission for {symbol}.")
    return rate


def reverse_candles(candles: pd.DataFrame) -> pd.DataFrame:
    result = candles.copy()
    if result.empty:
        return result
    original_high = result["high"].copy()
    original_low = result["low"].copy()
    original_volume = result["volume"].copy()
    if (result[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Cannot reverse candles containing zero or negative prices.")
    result["open"] = 1 / result["open"]
    result["high"] = 1 / original_low
    result["low"] = 1 / original_high
    result["close"] = 1 / result["close"]
    result["volume"] = result["quote_asset_volume"]
    result["quote_asset_volume"] = original_volume
    return result


def fetch_completed_mexc_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    mexc_intervals = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m", "4h": "4h", "1d": "1d"}
    if interval not in mexc_intervals:
        raise ValueError(f"MEXC does not support interval {interval!r}.")
    interval_ms = INTERVAL_MS[interval]
    frames: list[pd.DataFrame] = []
    cursor = start_ms
    while cursor < end_ms:
        page_end = min(end_ms, cursor + interval_ms * 1000 - 1)
        response = requests.get(
            MEXC_KLINES_URL,
            params={"symbol": symbol, "interval": mexc_intervals[interval], "startTime": cursor, "endTime": page_end, "limit": 1000},
            timeout=20,
        )
        response.raise_for_status()
        rows = response.json()
        if isinstance(rows, dict):
            raise ValueError(f"MEXC API error: {rows.get('msg', 'unknown error')}")
        if not rows:
            break
        frame = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_asset_volume"])
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
    result = result[result["open_time"] + interval_ms <= end_ms]
    result["time"] = pd.to_datetime(result["open_time"], unit="ms", utc=True)
    result["number_of_trades"] = 0
    result = result.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    return result[CANDLE_COLUMNS]


def fetch_completed_linear_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    if interval not in BYBIT_INTERVALS:
        raise ValueError(f"Bybit does not support interval {interval!r}.")
    rows: list[list[str]] = []
    cursor_end = end_ms
    while cursor_end > start_ms:
        response = requests.get(
            BYBIT_KLINES_URL,
            params={"category": "linear", "symbol": symbol, "interval": BYBIT_INTERVALS[interval], "start": start_ms, "end": cursor_end, "limit": 1000},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise ValueError(f"Bybit API error: {payload.get('retMsg', 'unknown error')}")
        page = payload.get("result", {}).get("list", [])
        if not page:
            break
        rows.extend(page)
        next_end = min(int(row[0]) for row in page) - 1
        if next_end >= cursor_end:
            break
        cursor_end = next_end
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "quote_asset_volume"])
    numeric = ["open", "high", "low", "close", "volume", "quote_asset_volume"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame["open_time"] = pd.to_numeric(frame["open_time"])
    frame = frame[frame["open_time"] + INTERVAL_MS[interval] <= end_ms]
    frame["time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame["number_of_trades"] = 0
    frame = frame.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    return frame[["time", "open", "high", "low", "close", "volume", "quote_asset_volume", "number_of_trades"]]
