# Copyright 2026 anchored-vwam-backtest contributors
"""Adapt prepared historical data to the sequential spot engine."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pandas as pd

from shared.book_execution import BookEngine, run_book_backtest
from shared.candles import candle_rows
from shared.order_book import BookHistory
from shared.spot_execution import (
    DailyGexSignals,
    ExitDecision,
    SpotAccount,
    SpotEngine,
)

if TYPE_CHECKING:
    from shared.models import Trade
    from shared.spot_execution import (
        BandSignals,
        CrossoverSignals,
        OptionsGexSignals,
        SpotRisk,
    )
    from shared.strategy_data import BacktestWindow, PreparedBacktest


def run_spot_backtest(
    prepared: PreparedBacktest,
    capital: float,
    *,
    risk: SpotRisk,
    signals: CrossoverSignals | BandSignals | OptionsGexSignals,
) -> tuple[pd.DataFrame, list[Trade]]:
    """Execute chronological candles and serialize marked account values.

    Returns:
        The equity curve and settled trade history.

    Raises:
        ValueError: If execution-book metadata or the strategy is incompatible.

    """
    books = cast("object", prepared.data.attrs.get("execution_books"))
    if books is not None:
        if not isinstance(books, BookHistory) or not isinstance(
            signals, DailyGexSignals
        ):
            msg = (
                "Historical book execution requires daily GEX and validated"
                " books."
            )
            raise ValueError(msg)
        result, trades = run_book_backtest(
            prepared,
            books,
            engine=BookEngine(
                account=SpotAccount(cash=capital, fee_pct=risk.fee_pct),
                risk=risk,
                signals=signals,
            ),
        )
        validate_trade_timeline(trades, prepared.window)
        return result, trades
    account = SpotAccount(
        cash=capital, fee_pct=risk.fee_pct, slippage_bps=risk.slippage_bps
    )
    engine = SpotEngine(account=account, risk=risk, signals=signals)
    candles = list(candle_rows(prepared.data))
    days = (
        prepared.scheduled_days.tolist()
        if prepared.scheduled_days is not None
        else [None] * len(candles)
    )
    rows: list[dict[str, object]] = []
    for index, row in enumerate(candles):
        engine.process(
            row,
            session_start=days[index] is not None
            and (index == 0 or days[index] != days[index - 1]),
        )
        if days[index] is not None and (
            index == len(candles) - 1 or days[index] != days[index + 1]
        ):
            account.close(
                ExitDecision(
                    price=row.close, reason="End of scheduled session"
                ),
                row.time,
            )
        if index == len(candles) - 1:
            account.close(
                ExitDecision(price=row.close, reason="End of strategy"),
                row.time,
            )
        mark = account.mark(row.close)
        rows.append({
            "time": row.time,
            "equity": mark.equity,
            "position": mark.quantity,
            "close": row.close,
            "anchored_vwap": row.anchored_vwap,
        })
    validate_trade_timeline(account.trades, prepared.window)
    return pd.DataFrame(rows), account.trades


def validate_trade_timeline(
    trades: list[Trade], window: BacktestWindow
) -> None:
    """Enforce the execution timeline before exposing completed trades.

    Raises:
        RuntimeError: If an engine generated an out-of-window trade.

    """
    if any(
        trade.entry_time < window.start
        or trade.entry_time > window.end
        or trade.exit_time < window.start
        or trade.exit_time > window.end
        for trade in trades
    ):
        msg = "Backtest generated a trade outside the strategy timeline."
        raise RuntimeError(msg)
