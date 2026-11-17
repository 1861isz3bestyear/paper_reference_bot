# Copyright 2026 anchored-vwam-backtest contributors
"""Verify the reusable child replay in both deployable shared packages."""

from dataclasses import replace
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from shared.child_assets import (
    ChildFunding,
    ChildOrder,
    ChildPrice,
    replay_child,
)

if TYPE_CHECKING:
    from shared.spot_execution import Side

TIMES = pd.date_range("2026-11-01", periods=3, freq="min", tz="UTC")
FUNDING = ChildFunding(capital=100, fee_pct=0, slippage_bps=0, leverage=1)
PRICES = tuple(
    ChildPrice(time=timestamp, opening=opening, close=close)
    for timestamp, opening, close in zip(
        TIMES, (1, 0.1, 1.2), (1.1, 0.05, 1.3), strict=True
    )
)


def orders(*, side: Side = "Long") -> tuple[ChildOrder, ...]:
    """Provide explicit father actions independent of any signal calculation.

    Returns:
        Entry and stop-loss actions two candles apart.

    """
    return (
        ChildOrder(time=TIMES[0], action="open", side=side),
        ChildOrder(
            time=TIMES[2], action="close", side=side, reason="Stop loss"
        ),
    )


@pytest.mark.parametrize("side", ["Long", "Short"])
def test_mirrored_position_ignores_child_extremes(side: Side) -> None:
    """Close solely on the father action even after an adverse child move."""
    result = replay_child(PRICES, orders(side=side), funding=FUNDING)
    trade = result.trades[0]
    expected = 20 if side == "Long" else -20
    assert trade.pnl == pytest.approx(expected)
    assert trade.entry_time == TIMES[0]
    assert trade.exit_time == TIMES[2]
    assert trade.exit_reason == "Stop loss"
    assert result.marks[-1].equity == pytest.approx(FUNDING.capital + expected)
    intermediate = 5 if side == "Long" else 195
    assert result.marks[1].equity == pytest.approx(intermediate)


@pytest.mark.parametrize("side", ["Long", "Short"])
def test_leveraged_fees_and_same_candle_execution(side: Side) -> None:
    """Charge both fees and adverse slippage without using the future close."""
    entry, close = orders(side=side)
    funding = replace(FUNDING, fee_pct=0.1, slippage_bps=10, leverage=2)
    result = replay_child(
        PRICES, (entry, replace(close, time=entry.time)), funding=funding
    )
    trade = result.trades[0]
    entry_price = 1.001 if side == "Long" else 0.999
    exit_price = 0.999 if side == "Long" else 1.001
    quantity = FUNDING.capital / (0.5 + 0.002) / entry_price
    assert trade.quantity == pytest.approx(quantity)
    assert trade.entry_price == pytest.approx(entry_price)
    assert trade.exit_price == pytest.approx(exit_price)
    assert trade.pnl == pytest.approx(
        -quantity * 0.002 - quantity * (entry_price + exit_price) * 0.001
    )


def test_empty_orders_and_independent_funding() -> None:
    """Price moves alone never open a trade or share account capital."""
    idle = replay_child(PRICES, (), funding=FUNDING)
    first = replay_child(PRICES, orders(), funding=FUNDING)
    second = replay_child(PRICES, orders(), funding=FUNDING)
    assert idle.trades == ()
    assert all(mark.equity == FUNDING.capital for mark in idle.marks)
    assert first == second


def test_reject_invalid_execution_ledgers() -> None:
    """Reject missing prices, overlapping entries and unmatched exits."""
    entry, close = orders()
    invalid = (
        (close, entry),
        (entry, entry, close),
        (close,),
        (entry, replace(close, side="Short")),
        (entry,),
        (entry, replace(close, time=TIMES[-1] + pd.Timedelta(minutes=1))),
    )
    for sequence in invalid:
        with pytest.raises(
            ValueError, match=r"chronological|overlapping|match|Missing"
        ):
            _ = replay_child(PRICES, sequence, funding=FUNDING)


def test_reject_depleted_account_and_price_timeline() -> None:
    """Do not fabricate capital or execute against duplicated timestamps."""
    entry, close = orders(side="Short")
    prices = tuple(replace(price, opening=3) for price in PRICES[1:])
    with pytest.raises(ValueError, match="depleted"):
        _ = replay_child(
            (PRICES[0], *prices),
            (entry, close, replace(entry, time=close.time)),
            funding=FUNDING,
        )
    with pytest.raises(ValueError, match="unique chronological"):
        _ = replay_child((PRICES[0], PRICES[0]), (), funding=FUNDING)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_reject_invalid_prices(value: float) -> None:
    """Reject invalid external price values before account mutation."""
    for price in (
        replace(PRICES[0], opening=value),
        replace(PRICES[0], close=value),
    ):
        with pytest.raises(ValueError, match="finite and positive"):
            _ = replay_child((price,), (), funding=FUNDING)


@pytest.mark.parametrize(
    "changes",
    [
        {"capital": 0},
        {"capital": float("nan")},
        {"fee_pct": -1},
        {"fee_pct": 100},
        {"fee_pct": float("inf")},
        {"slippage_bps": -1},
        {"slippage_bps": float("nan")},
        {"leverage": 0},
    ],
)
def test_reject_invalid_funding(changes: dict[str, float]) -> None:
    """Reject invalid account settings before running orders."""
    with pytest.raises(ValueError, match=r"capital|fee|leverage"):
        _ = replace(FUNDING, **changes)
