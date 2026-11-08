# Copyright 2026 anchored-vwam-backtest contributors
"""Execute complete market orders against historical displayed book depth."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pandas as pd

from shared.candles import candle_rows
from shared.spot_execution import (
    DailyGexSignals,
    ExitDecision,
    SignalHistory,
    SpotAccount,
    protective_exit,
)

if TYPE_CHECKING:
    from shared.candles import Candle
    from shared.models import Trade
    from shared.order_book import BookHistory, BookSnapshot
    from shared.spot_execution import ExitReason, SpotRisk
    from shared.strategy_data import PreparedBacktest


@dataclass(kw_only=True)
class BookEngine:
    """Own account mutation separately from book pricing."""

    account: SpotAccount
    risk: SpotRisk
    signals: DailyGexSignals
    pending_exit: ExitReason | None = None

    def process(self, row: Candle, book: BookSnapshot) -> str:
        """Apply protection before entries, using the executable spread side.

        Returns:
            An auditable action or rejection reason for this receipt.

        """
        position = self.account.position
        if position is None:
            return self.enter(row, book)
        if self.pending_exit is not None:
            return self.close(row, book, reason=self.pending_exit)
        price = (
            book.bids[0].price
            if position.side == "Long"
            else book.asks[0].price
        )
        quote = replace(row, open=price, high=price, low=price, close=price)
        decision = protective_exit(quote, position, self.risk)
        if decision is None:
            return "Holding"
        return self.close(row, book, reason=decision.reason)

    def enter(self, row: Candle, book: BookSnapshot) -> str:
        """Size an entry against depth before committing account changes.

        Returns:
            The entry result, including insufficient-liquidity rejections.

        """
        if (
            self.account.cash <= 0
            or self.account.cash < self.risk.minimum_order_size
        ):
            return "Below minimum capital"
        decision = self.signals.entry(row, SignalHistory())
        if decision is None:
            return "No entry"
        price = book.allocation(
            self.account.cash * (1 - self.risk.fee_pct / 100),
            buy=decision.side == "Long",
        )
        if price is None:
            return "Entry rejected: insufficient depth"
        direction = 1 if decision.side == "Long" else -1
        if (
            math.isfinite(decision.stop)
            and direction * (price - decision.stop) <= 0
        ):
            return "Entry rejected: stop beyond fill"
        self.account.open(replace(decision, price=price), row.time)
        return "Entry filled"

    def close(
        self, row: Candle, book: BookSnapshot, *, reason: ExitReason
    ) -> str:
        """Settle only when the entire held quantity is executable.

        Returns:
            The exit result; a rejected exit leaves the position open.

        """
        position = self.account.position
        if position is None:
            return "Flat"
        reason = self.pending_exit or reason
        price = book.fill(position.quantity, buy=position.side == "Short")
        if price is None:
            self.pending_exit = reason
            return "Exit rejected: insufficient depth"
        self.account.close(ExitDecision(price=price, reason=reason), row.time)
        self.pending_exit = None
        return reason


def run_book_backtest(
    prepared: PreparedBacktest,
    books: BookHistory,
    *,
    engine: BookEngine,
) -> tuple[pd.DataFrame, list[Trade]]:
    """Replay receipt events without candle fills or additional slippage.

    Returns:
        Marked equity with execution status and fully settled trade history.

    Raises:
        ValueError: If books are missing or final liquidation is impossible.

    """
    lookup = {pd.Timestamp(book.available): book for book in books.snapshots}
    candles = tuple(candle_rows(prepared.data))
    days = (
        prepared.scheduled_days.tolist()
        if prepared.scheduled_days is not None
        else [None] * len(candles)
    )
    records: list[dict[str, object]] = []
    for index, row in enumerate(candles):
        book = lookup.get(row.time)
        if book is None:
            msg = "Execution row has no matching historical book receipt."
            raise ValueError(msg)
        last = index == len(candles) - 1
        session_end = days[index] is not None and (
            last or days[index] != days[index + 1]
        )
        status = "Stale book"
        if book.fresh(books.maximum_age):
            if last or session_end:
                status = engine.close(
                    row,
                    book,
                    reason=(
                        "End of strategy"
                        if last
                        else "End of scheduled session"
                    ),
                )
            else:
                status = engine.process(row, book)
        if (last or session_end) and engine.account.position is not None:
            msg = (
                "Cannot liquidate at session/end boundary: stale book or"
                " insufficient depth."
            )
            raise ValueError(msg)
        mark = engine.account.mark(book.midpoint)
        records.append({
            "time": row.time,
            "equity": mark.equity,
            "position": mark.quantity,
            "close": book.midpoint,
            "anchored_vwap": row.anchored_vwap,
            "execution_status": status,
        })
    return pd.DataFrame(records), engine.account.trades
