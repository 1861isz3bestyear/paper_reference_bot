# Copyright 2026 anchored-vwam-backtest contributors
"""Collect public completed candles without creating a trading account."""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import sleep, time_ns
from typing import TYPE_CHECKING, cast

import requests

from reference_bot.market import (
    INTERVAL_MS,
    CandleCache,
    fetch_completed_linear_klines,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SaverConfig:
    """Configure a public Bybit linear candle archive."""

    database: Path = Path("data/candles.sqlite")
    symbol: str = "BTCUSDT"
    interval: str = "1m"
    poll_seconds: float = 30
    history_days: int = 7

    def __post_init__(self) -> None:
        """Reject invalid settings before touching persistence.

        Raises:
            ValueError: If settings cannot describe a valid collection.

        """
        if self.interval not in INTERVAL_MS:
            msg = "Unsupported candle interval."
            raise ValueError(msg)
        if not self.symbol.isascii() or not self.symbol.isalnum():
            msg = "Symbol must contain ASCII letters and digits."
            raise ValueError(msg)
        if (
            not math.isfinite(self.poll_seconds)
            or self.poll_seconds <= 0
            or self.history_days <= 0
        ):
            msg = "Polling seconds and history days must be positive."
            raise ValueError(msg)


def collect(config: SaverConfig, *, now_ms: int | None = None) -> int:
    """Backfill on first run and resume with an overlapping candle thereafter.

    Returns:
        Number of completed candles written, including overlap updates.

    Raises:
        ValueError: If the stored timestamp is invalid.

    """
    end_ms = now_ms if now_ms is not None else time_ns() // 1_000_000
    config.database.parent.mkdir(parents=True, exist_ok=True)
    cache = CandleCache(config.database)
    with closing(sqlite3.connect(config.database)) as connection:
        row = cast(
            "tuple[object]",
            connection.execute(
                "SELECT MAX(open_time_ms) FROM candles "
                "WHERE symbol = ? AND interval = ?",
                (config.symbol, config.interval),
            ).fetchone(),
        )
    latest = row[0]
    if latest is not None and (
        not isinstance(latest, int) or isinstance(latest, bool)
    ):
        msg = "Stored candle timestamp must be an integer."
        raise ValueError(msg)
    start_ms = (
        latest
        if latest is not None
        else end_ms - config.history_days * 86_400_000
    )
    frame = fetch_completed_linear_klines(
        config.symbol, config.interval, start_ms, end_ms
    )
    cache.upsert(config.symbol, config.interval, frame)
    return len(frame)


class Arguments(argparse.Namespace):
    """Typed command-line settings populated by argparse."""

    database: Path = Path("data/candles.sqlite")
    symbol: str = "BTCUSDT"
    interval: str = "1m"
    poll_seconds: float = 30
    history_days: int = 7
    once: bool = False


def main(argv: Sequence[str] | None = None) -> int:
    """Run a standalone saver once or continuously, retrying transient errors.

    Returns:
        Zero on graceful shutdown; one when a single collection fails.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--database", type=Path, default=Path("data/candles.sqlite")
    )
    _ = parser.add_argument("--symbol", default="BTCUSDT")
    _ = parser.add_argument(
        "--interval", choices=tuple(INTERVAL_MS), default="1m"
    )
    _ = parser.add_argument("--poll-seconds", type=float, default=30)
    _ = parser.add_argument("--history-days", type=int, default=7)
    _ = parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        config = SaverConfig(
            database=Path(str(args.database)),
            symbol=str(args.symbol).upper(),
            interval=str(args.interval),
            poll_seconds=float(args.poll_seconds),
            history_days=int(args.history_days),
        )
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO)
    try:
        return run(config, once=bool(args.once))
    except KeyboardInterrupt:
        return 0


def run(config: SaverConfig, *, once: bool) -> int:
    """Collect repeatedly with retries, or return the single collection status.

    Returns:
        One for a failed single collection, otherwise zero.

    """
    while True:
        status = 0
        try:
            LOGGER.info("Saved %s completed candles", collect(config))
        except requests.RequestException, ValueError, sqlite3.Error, OSError:
            LOGGER.exception("Candle collection failed")
            status = 1
        if once:
            return status
        sleep(config.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
