# Copyright (c) 2026 Margin Loan Oscillator contributors.
"""Public Bitget lending data and causal growth momentum calculations."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import requests

Period = Literal["24h", "30d"]
WINDOW = 14


@dataclass(frozen=True, kw_only=True)
class Growth:
    """One published loan growth observation."""

    timestamp: datetime
    growth: float


@dataclass(frozen=True, kw_only=True)
class LoanRate:
    """Current VIP0 lending terms, observed at the exchange request time."""

    timestamp: datetime
    daily: float
    annual: float


def mapping(value: object) -> dict[str, object]:
    """Validate a JSON object.

    Returns:
        An object with validated string keys.

    Raises:
        TypeError: If the object is malformed.

    """
    if not isinstance(value, dict):
        msg = "Expected a Bitget JSON object."
        raise TypeError(msg)
    entries = cast("dict[object, object]", value)
    result: dict[str, object] = {}
    for key, item in entries.items():
        if not isinstance(key, str):
            msg = "Expected string JSON keys."
            raise TypeError(msg)
        result[key] = item
    return result


def finite(value: object) -> float:
    """Validate exchange numeric strings.

    Returns:
        A finite number.

    Raises:
        TypeError: If the value is not numeric.
        ValueError: If the value is not finite.

    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        msg = "Expected a finite Bitget number."
        raise TypeError(msg)
    result = float(value)
    if not math.isfinite(result):
        msg = "Expected a finite Bitget number."
        raise ValueError(msg)
    return result


def response_body(text: str) -> dict[str, object]:
    """Validate an exchange response envelope.

    Returns:
        The successful response object.

    Raises:
        ValueError: If the API reports a failure.

    """
    body = mapping(cast("object", json.loads(text)))
    if body.get("code") != "00000":
        msg = f"Bitget request failed: {body.get('msg', 'unknown error')}"
        raise ValueError(msg)
    return body


def parse_growth(text: str) -> tuple[Growth, ...]:
    """Validate, deduplicate and sort exchange growth records.

    Returns:
        Chronological observations.

    Raises:
        TypeError: If records are malformed.
        ValueError: If records or duplicate timestamps are invalid.

    """
    data = response_body(text).get("data")
    if not isinstance(data, list):
        msg = "Expected Bitget growth records."
        raise TypeError(msg)
    result: dict[datetime, Growth] = {}
    for item in cast("list[object]", data):
        row = mapping(item)
        stamp = datetime.fromtimestamp(finite(row.get("ts")) / 1000, UTC)
        point = Growth(timestamp=stamp, growth=finite(row.get("growthRate")))
        if stamp in result and result[stamp] != point:
            msg = "Conflicting Bitget growth records."
            raise ValueError(msg)
        result[stamp] = point
    return tuple(result[stamp] for stamp in sorted(result))


def parse_rate(text: str) -> LoanRate:
    """Validate current borrowing rates separately from growth history.

    Returns:
        Nonnegative decimal interest rates.

    Raises:
        ValueError: If interest rates are negative.

    """
    body = response_body(text)
    data = mapping(body.get("data"))
    daily = finite(data.get("dailyInterest"))
    annual = finite(data.get("annualInterest"))
    if min(daily, annual) < 0:
        msg = "Bitget interest rates must be nonnegative."
        raise ValueError(msg)
    return LoanRate(
        timestamp=datetime.fromtimestamp(
            finite(body.get("requestTime")) / 1000, UTC
        ),
        daily=daily,
        annual=annual,
    )


def growth_momentum(
    points: tuple[Growth, ...], *, period: Period
) -> tuple[float | None, ...]:
    """Normalize trailing positive growth against total absolute growth.

    Returns:
        100 * sum(max(growth, 0)) / sum(abs(growth)) over 14 observations.
        Flat windows read 50; warmup and gaps are never filled.

    Raises:
        ValueError: If observations are not strictly chronological or finite.

    """
    step = timedelta(hours=1) if period == "24h" else timedelta(days=1)
    window: list[float] = []
    values: list[float | None] = []
    previous: datetime | None = None
    for point in points:
        if not math.isfinite(point.growth) or (
            previous is not None and point.timestamp <= previous
        ):
            msg = "Growth must be finite and strictly chronological."
            raise ValueError(msg)
        if previous is not None and point.timestamp - previous != step:
            window = []
        previous = point.timestamp
        window = [*window[-(WINDOW - 1) :], point.growth]
        scale = max(abs(value) for value in window)
        total = sum(abs(value) / scale for value in window) if scale else 0
        positive = (
            sum(max(value / scale, 0) for value in window) if scale else 0
        )
        rate = 100 * positive / total if total else 50
        values.append(rate if len(window) == WINDOW else None)
    return tuple(values)


def fetch_growth(asset: str, *, period: Period) -> tuple[Growth, ...]:
    """Fetch public base-coin loan growth without credentials.

    Returns:
        Validated growth history using Bitget's supported period parameter.

    """
    response = requests.get(
        "https://api.bitget.com/api/v3/market/margin-loan-growth",
        params={"symbol": f"{asset}USDT", "coin": asset, "period": period},
        timeout=15,
    )
    response.raise_for_status()
    return parse_growth(response.text)


def fetch_rate(asset: str) -> LoanRate:
    """Fetch current public VIP0 loan interest rates.

    Returns:
        The current lending quote; this is not historical data.

    """
    response = requests.get(
        "https://api.bitget.com/api/v3/market/margin-loans",
        params={"coin": asset, "level": "vip0"},
        timeout=15,
    )
    response.raise_for_status()
    return parse_rate(response.text)


def parse_period(value: str) -> Period:
    """Validate the public API history selector.

    Returns:
        A supported exchange period.

    Raises:
        ValueError: If the requested period is unsupported.

    """
    if value == "24h":
        return "24h"
    if value == "30d":
        return "30d"
    msg = "Unsupported Bitget period."
    raise ValueError(msg)
