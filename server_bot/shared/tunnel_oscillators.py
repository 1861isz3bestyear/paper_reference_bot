# Copyright 2026 anchored-vwam-backtest contributors
"""Pure order-book liquidity composition."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:

    from shared.order_book import BookSnapshot


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
