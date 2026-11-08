# Copyright 2026 anchored-vwam-backtest contributors
"""Pure order-book composition and warmed-up margin-rate momentum."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from shared.order_book import BookSnapshot


@dataclass(frozen=True, kw_only=True)
class BorrowRate:
    """One hourly margin interest rate annualized without compounding."""

    timestamp: datetime
    annual_rate: float


def limit_order_oscillator_rate(book: BookSnapshot) -> float:
    """Calculate bid share of resting quote-currency notional.

    Returns:
        A 0-100 bid/ask liquidity rate, independent of price changes.

    Raises:
        ValueError: If total quote notional is nonfinite or nonpositive.

    """
    bid = math.fsum(level.price * level.quantity for level in book.bids)
    ask = math.fsum(level.price * level.quantity for level in book.asks)
    if not math.isfinite(bid + ask) or bid + ask <= 0:
        msg = "Recorded order-book notional must be finite and positive."
        raise ValueError(msg)
    return 100 * bid / (bid + ask)


def borrow_rate_rsi(rates: tuple[BorrowRate, ...]) -> tuple[float | None, ...]:
    """Calculate Wilder RSI(14), retaining warmup and flat-series neutrality.

    Returns:
        One value per rate; the first fourteen values are unavailable.

    """
    period = 14
    values: list[float | None] = [None] * min(period, len(rates))
    gain = loss = 0.0
    for index in range(1, len(rates)):
        delta = rates[index].annual_rate - rates[index - 1].annual_rate
        up, down = max(delta, 0), max(-delta, 0)
        if index <= period:
            gain += up / period
            loss += down / period
        else:
            gain = (gain * (period - 1) + up) / period
            loss = (loss * (period - 1) + down) / period
        if index < period:
            continue
        value = 50.0
        if gain + loss > 0:
            value = 100 * gain / (gain + loss)
        values.append(value)
    return tuple(values)
