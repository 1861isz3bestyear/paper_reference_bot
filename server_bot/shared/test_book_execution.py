# Copyright 2026 anchored-vwam-backtest contributors
"""Exercise historical depth validation, spread costs and account execution."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from shared.book_execution import BookEngine
from shared.candles import Candle
from shared.gex_rules import GexHypothesis
from shared.order_book import (
    BookHistory,
    BookLevel,
    BookSnapshot,
    parse_book_history,
)
from shared.spot_execution import DailyGexSignals, SpotAccount, SpotRisk
from shared.test_strategy import run

NOW = datetime(2026, 9, 21, 9, tzinfo=UTC)
HEADER = "time,available_at,side,price,quantity\n"


def book_at(seconds: int = 0) -> BookSnapshot:
    """Build a two-level book received after its source observation.

    Returns:
        A book with a visible spread and finite depth.

    """
    return BookSnapshot(
        observed=NOW + timedelta(seconds=seconds),
        available=NOW + timedelta(seconds=seconds, milliseconds=100),
        bids=(
            BookLevel(price=99, quantity=1),
            BookLevel(price=98, quantity=10),
        ),
        asks=(
            BookLevel(price=101, quantity=1),
            BookLevel(price=102, quantity=10),
        ),
    )


def book_csv() -> str:
    """Serialize a small complete snapshot fixture.

    Returns:
        Explicit source and receipt timestamps for each depth level.

    """
    return HEADER + "".join(
        f"{NOW.isoformat()},{NOW.isoformat()},{side},{price},2\n"
        for side, price in (
            ("bid", 99),
            ("bid", 98),
            ("ask", 101),
            ("ask", 102),
        )
    )


def test_depth_prices_and_insufficient_liquidity() -> None:
    """Walk levels on both sides without manufacturing additional liquidity."""
    book = book_at()
    assert book.fill(2, buy=True) == pytest.approx(101.5)
    assert book.fill(2, buy=False) == pytest.approx(98.5)
    assert book.allocation(203, buy=True) == pytest.approx(101.5)
    assert book.allocation(197, buy=False) == pytest.approx(98.5)
    assert book.fill(12, buy=True) is None
    assert book.allocation(2000, buy=False) is None
    assert book.fill(float("nan"), buy=True) is None
    assert book.allocation(-1, buy=True) is None


@pytest.mark.parametrize(
    "fault",
    [
        "crossed",
        "missing",
        "duplicate",
        "negative",
        "future",
        "naive",
        "side",
        "columns",
        "empty",
        "order",
    ],
)
def test_invalid_external_books(fault: str) -> None:
    """Reject malformed or ambiguous snapshots at the CSV boundary."""
    content = book_csv()
    changes = {
        "crossed": content.replace(",bid,99,", ",bid,103,"),
        "missing": "\n".join(
            line for line in content.splitlines() if ",ask," not in line
        ),
        "duplicate": content + content.splitlines()[1] + "\n",
        "negative": content.replace(",2\n", ",-2\n"),
        "future": content.replace(
            f",{NOW.isoformat()},",
            f",{(NOW - timedelta(seconds=1)).isoformat()},",
        ),
        "naive": content.replace("+00:00", ""),
        "side": content.replace(",bid,", ",buy,"),
        "columns": content.replace("quantity", "size"),
        "empty": HEADER,
        "order": (
            content
            + content.replace(
                NOW.isoformat(), (NOW - timedelta(seconds=1)).isoformat()
            ).removeprefix(HEADER)
        ),
    }
    with pytest.raises(ValueError, match=r"Book|book"):
        _ = parse_book_history(
            changes[fault], maximum_age=timedelta(seconds=10)
        )


def test_valid_book_csv_and_receipt_freshness() -> None:
    """Require a complete snapshot while preserving its causal timestamps."""
    history = parse_book_history(book_csv(), maximum_age=timedelta(seconds=10))
    assert history.snapshots[0].midpoint == pytest.approx(100)
    assert book_at().fresh(timedelta(seconds=1))
    assert not book_at().fresh(timedelta(milliseconds=50))


def execution_frame(
    books: tuple[BookSnapshot, ...], *, direction: int
) -> pd.DataFrame:
    """Adapt receipt events to the public backtest boundary.

    Returns:
        A daily signal frame with independently retained depth.

    """
    data = pd.DataFrame({
        "time": [book.available for book in books],
        "open": 100.0,
        "high": 100.0,
        "low": 100.0,
        "close": 100.0,
        "anchored_vwap": 100.0,
        "anchored_std": 0.0,
        "options_direction": [direction, *([0] * (len(books) - 1))],
        "options_entry": 100.0,
    })
    data.attrs["execution_books"] = BookHistory(snapshots=books)
    return data


@pytest.mark.parametrize("direction", [1, -1])
def test_public_backtest_uses_depth_and_fees(direction: int) -> None:
    """Ignore candle prices and synthetic slippage for book-backed fills."""
    books = (book_at(), book_at(1))
    _, trades = run(
        execution_frame(books, direction=direction),
        strategy_mode="GEX",
        gex_hypothesis=GexHypothesis.DAILY,
        initial_capital=205,
        fee_pct=1,
        slippage_bps=500,
    )
    assert len(trades) == 1
    trade = trades[0]
    expected = books[0].allocation(202.95, buy=direction > 0)
    assert trade.entry_price == pytest.approx(expected)
    assert trade.exit_price == pytest.approx(
        books[1].fill(trade.quantity, buy=direction < 0)
    )
    assert trade.entry_time == books[0].available
    assert trade.exit_time == books[1].available
    assert trade.pnl < 0
    assert trade.quantity * trade.entry_price == pytest.approx(202.95)


def test_insufficient_entry_depth_and_terminal_exit() -> None:
    """Reject oversized entries and fail visibly if liquidation cannot fill."""
    books = (book_at(), book_at(1))
    equity, trades = run(
        execution_frame(books, direction=1),
        strategy_mode="GEX",
        gex_hypothesis=GexHypothesis.DAILY,
        initial_capital=10000,
    )
    assert not trades
    assert (
        equity.loc[0, "execution_status"]
        == "Entry rejected: insufficient depth"
    )
    thin = replace(books[1], bids=(BookLevel(price=99, quantity=0.01),))
    with pytest.raises(ValueError, match="Cannot liquidate"):
        _ = run(
            execution_frame((books[0], thin), direction=1),
            strategy_mode="GEX",
            gex_hypothesis=GexHypothesis.DAILY,
            initial_capital=200,
        )


def test_stop_retries_after_liquidity_returns() -> None:
    """A triggered stop remains pending even if the next quote recovers."""
    engine = BookEngine(
        account=SpotAccount(cash=200, fee_pct=0),
        risk=SpotRisk(
            fee_pct=0, stop_loss_pct=5, take_profit_pct=10, minimum_order_size=0
        ),
        signals=DailyGexSignals(),
    )
    row = Candle(
        time=pd.Timestamp(NOW),
        open=100,
        high=100,
        low=100,
        close=100,
        options_direction=1,
        options_entry=100,
    )
    assert engine.process(row, book_at()) == "Entry filled"
    thin = replace(book_at(1), bids=(BookLevel(price=90, quantity=0.01),))
    assert (
        engine.process(replace(row, time=pd.Timestamp(thin.available)), thin)
        == "Exit rejected: insufficient depth"
    )
    recovered = book_at(2)
    assert (
        engine.process(
            replace(row, time=pd.Timestamp(recovered.available)), recovered
        )
        == "Stop loss"
    )
    assert engine.account.position is None
    assert len(engine.account.trades) == 1
