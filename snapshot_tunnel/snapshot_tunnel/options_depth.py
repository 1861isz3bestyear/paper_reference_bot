# Copyright 2026 anchored-vwam-backtest contributors
"""Collect and serialize complete Bybit books alongside options snapshots."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from time import sleep
from typing import TYPE_CHECKING

from snapshot_tunnel.order_book import BookSnapshot, parse_book_history
from snapshot_tunnel.validation import (
    integer,
    json_object,
    numeric_string,
    object_list,
    object_mapping,
)

if TYPE_CHECKING:
    import requests

DEPTH_LIMIT = 50
LEVEL_WIDTH = 2
MAXIMUM_DEPTH_AGE = timedelta(seconds=10)
MAXIMUM_CLOCK_LEAD = timedelta(seconds=5)


def depth_csv(book: BookSnapshot) -> str:
    """Serialize a complete book with exchange and local receipt timestamps.

    Returns:
        Validatable depth CSV, with one row per price level.

    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(("time", "available_at", "side", "price", "quantity"))
    for side, levels in (("bid", book.bids), ("ask", book.asks)):
        writer.writerows(
            (
                book.observed.isoformat(),
                book.available.isoformat(),
                side,
                level.price,
                level.quantity,
            )
            for level in levels
        )
    return output.getvalue()


def parse_depth(
    payload: str, *, asset: str, received: datetime
) -> BookSnapshot:
    """Validate Bybit's full top-50 linear book and its receipt freshness.

    Returns:
        A two-sided, uncrossed, timely full book.

    Raises:
        ValueError: If the exchange, symbol, levels or timestamps are invalid.

    """
    body = json_object(payload)
    result = object_mapping(body.get("result"))
    if integer(body["retCode"]) != 0 or result.get("s") != f"{asset}USDT":
        msg = "Bybit depth request failed or returned the wrong market."
        raise ValueError(msg)
    observed = datetime.fromtimestamp(numeric_string(result["ts"]) / 1000, UTC)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(("time", "available_at", "side", "price", "quantity"))
    for side, key in (("bid", "b"), ("ask", "a")):
        levels = object_list(result[key])
        if not 1 <= len(levels) <= DEPTH_LIMIT:
            msg = "Bybit depth must contain 1-50 levels per side."
            raise ValueError(msg)
        for item in levels:
            level = object_list(item)
            if len(level) != LEVEL_WIDTH:
                msg = "Bybit depth levels require price and quantity."
                raise ValueError(msg)
            writer.writerow((
                observed.isoformat(),
                received.isoformat(),
                side,
                numeric_string(level[0]),
                numeric_string(level[1]),
            ))
    book = parse_book_history(
        output.getvalue(), maximum_age=MAXIMUM_DEPTH_AGE
    ).snapshots[0]
    if not book.fresh(MAXIMUM_DEPTH_AGE):
        msg = "Bybit depth is stale at receipt."
        raise ValueError(msg)
    return book


def fetch_depth(session: requests.Session, *, asset: str) -> BookSnapshot:
    """Download a full current book and record its actual local receipt time.

    Returns:
        Validated top-50 Bybit linear-contract depth.

    Raises:
        ValueError: If the request fails or exchange clocks disagree.

    """
    response = session.get(
        "https://api.bybit.com/v5/market/orderbook",
        params={
            "category": "linear",
            "symbol": f"{asset}USDT",
            "limit": DEPTH_LIMIT,
        },
        timeout=(3, 5),
    )
    response.raise_for_status()
    received = datetime.now(UTC)
    body = json_object(response.text)
    result = object_mapping(body.get("result"))
    if integer(body["retCode"]) != 0:
        msg = "Bybit depth request failed."
        raise ValueError(msg)
    observed = datetime.fromtimestamp(numeric_string(result["ts"]) / 1000, UTC)
    lead = observed - received
    if lead > MAXIMUM_CLOCK_LEAD:
        msg = (
            "Bybit book timestamp is more than 5 seconds ahead of the local "
            "clock. Synchronize the system clock and retry."
        )
        raise ValueError(msg)
    if lead > timedelta(0):
        # Wait for actual availability instead of rewriting exchange timestamps.
        sleep(lead.total_seconds())
        received = datetime.now(UTC)
    return parse_depth(response.text, asset=asset, received=received)
