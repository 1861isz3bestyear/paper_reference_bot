# Copyright 2026 anchored-vwam-backtest contributors
"""Validate complete historical books and price marketable orders."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby, pairwise


@dataclass(frozen=True, kw_only=True)
class BookLevel:
    """One executable price and base-asset quantity."""

    price: float
    quantity: float


@dataclass(frozen=True, kw_only=True)
class BookSnapshot:
    """A complete depth snapshot with explicit source and receipt times."""

    observed: datetime
    available: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @property
    def midpoint(self) -> float:
        """The observed midpoint for marking and trigger detection."""
        return (self.bids[0].price + self.asks[0].price) / 2

    def fresh(self, maximum_age: timedelta) -> bool:
        """Check receipt latency.

        Returns:
            Whether the observed book is fresh when received.

        """
        return self.available - self.observed <= maximum_age

    def fill(self, quantity: float, *, buy: bool) -> float | None:
        """Price a complete base-quantity order against displayed depth.

        Returns:
            Volume-weighted price, or none when recorded depth is insufficient.

        """
        if not math.isfinite(quantity) or quantity <= 0:
            return None
        remaining = quantity
        cost = 0.0
        for level in self.asks if buy else self.bids:
            taken = min(remaining, level.quantity)
            cost += taken * level.price
            remaining -= taken
            if remaining <= 0:
                return cost / quantity
        return None

    def allocation(self, notional: float, *, buy: bool) -> float | None:
        """Price an entry that allocates a fixed quote-currency budget.

        Returns:
            Average price, or none if displayed liquidity cannot fill it.

        """
        if not math.isfinite(notional) or notional <= 0:
            return None
        remaining = notional
        quantity = 0.0
        for level in self.asks if buy else self.bids:
            spent = min(remaining, level.quantity * level.price)
            quantity += spent / level.price
            remaining -= spent
            if remaining <= 0:
                return notional / quantity
        return None


@dataclass(frozen=True, kw_only=True)
class BookHistory:
    """Keep independently complete books and their freshness policy together."""

    snapshots: tuple[BookSnapshot, ...]
    maximum_age: timedelta = timedelta(seconds=10)


def _time(value: str) -> datetime:
    """Parse an aware exchange or receipt timestamp.

    Returns:
        An aware datetime.

    Raises:
        ValueError: If timezone information is missing.

    """
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        msg = "Book timestamps must include a timezone."
        raise ValueError(msg)
    return result


def _level(row: dict[str, str]) -> BookLevel:
    """Validate one external price/quantity pair.

    Returns:
        Positive finite depth.

    Raises:
        ValueError: If prices or quantities are invalid.

    """
    price, quantity = float(row["price"]), float(row["quantity"])
    if not all(
        math.isfinite(value) and value > 0 for value in (price, quantity)
    ):
        msg = "Book prices and quantities must be finite and positive."
        raise ValueError(msg)
    return BookLevel(price=price, quantity=quantity)


def _snapshot(rows: tuple[dict[str, str], ...]) -> BookSnapshot:
    """Validate a complete snapshot without inferring missing depth.

    Returns:
        A sorted two-sided, noncrossed book.

    Raises:
        ValueError: If timestamps, sides or levels are inconsistent.

    """
    observed = _time(rows[0]["time"])
    available = _time(rows[0]["available_at"])
    if available < observed or any(
        _time(row["time"]) != observed or row["side"] not in {"bid", "ask"}
        for row in rows
    ):
        msg = "Book snapshots require causal timestamps and bid/ask sides."
        raise ValueError(msg)
    bids = tuple(
        sorted(
            (_level(row) for row in rows if row["side"] == "bid"),
            key=lambda level: -level.price,
        )
    )
    asks = tuple(
        sorted(
            (_level(row) for row in rows if row["side"] == "ask"),
            key=lambda level: level.price,
        )
    )
    if (
        not bids
        or not asks
        or bids[0].price >= asks[0].price
        or any(
            len({level.price for level in side}) != len(side)
            for side in (bids, asks)
        )
    ):
        msg = "Books require both sides, unique prices and a positive spread."
        raise ValueError(msg)
    return BookSnapshot(
        observed=observed, available=available, bids=bids, asks=asks
    )


def parse_book_history(content: str, *, maximum_age: timedelta) -> BookHistory:
    """Read full snapshots, one row per level, in receipt order.

    Returns:
        Validated historical depth; incremental updates are not accepted.

    Raises:
        ValueError: If schema, chronology or snapshot contents are invalid.

    """
    reader = csv.DictReader(io.StringIO(content))
    columns = {"time", "available_at", "side", "price", "quantity"}
    if (
        reader.fieldnames is None
        or set(reader.fieldnames) != columns
        or len(reader.fieldnames) != len(columns)
    ):
        msg = "Book CSV columns must be time,available_at,side,price,quantity."
        raise ValueError(msg)
    rows = tuple(reader)
    if (
        not rows
        or maximum_age <= timedelta(0)
        or any(
            None in row or any(value is None for value in row.values())
            for row in rows
        )
    ):
        msg = "Book history requires complete rows and a positive maximum age."
        raise ValueError(msg)
    snapshots = tuple(
        _snapshot(tuple(group))
        for _, group in groupby(
            rows, key=lambda row: _time(row["available_at"])
        )
    )
    if any(
        right.available <= left.available or right.observed <= left.observed
        for left, right in pairwise(snapshots)
    ):
        msg = (
            "Book snapshots must have increasing unique source and receipt"
            " times."
        )
        raise ValueError(msg)
    return BookHistory(snapshots=snapshots, maximum_age=maximum_age)
