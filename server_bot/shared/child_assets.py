# Copyright 2026 anchored-vwam-backtest contributors
"""Replay a father's executed orders using only child-asset prices."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from shared.models import Trade

if TYPE_CHECKING:
    import pandas as pd

    from shared.spot_execution import Side

type ChildSymbol = Literal["DOGEUSDT", "XRPUSDT"]
CHILD_SYMBOLS: tuple[ChildSymbol, ...] = ("DOGEUSDT", "XRPUSDT")
PERCENT = 100
BASIS_POINTS = 10000


@dataclass(frozen=True, kw_only=True)
class ChildPrice:
    """Hold a validated candle's opening fill proxy and closing valuation."""

    time: pd.Timestamp
    opening: float
    close: float


@dataclass(frozen=True, kw_only=True)
class ChildOrder:
    """Mirror one already executed father action, without evaluating signals."""

    time: pd.Timestamp
    action: Literal["open", "close"]
    side: Side
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class ChildFunding:
    """Configure a separate price-only account and transaction costs."""

    capital: float
    fee_pct: float
    slippage_bps: float
    leverage: int

    def __post_init__(self) -> None:
        """Reject invalid account parameters before execution.

        Raises:
            ValueError: If funding or costs are invalid.

        """
        if not math.isfinite(self.capital) or self.capital <= 0:
            msg = "Child starting capital must be finite and positive."
            raise ValueError(msg)
        if not math.isfinite(self.fee_pct) or not 0 <= self.fee_pct < PERCENT:
            msg = "Child fee must be finite and between 0 and 100 percent."
            raise ValueError(msg)
        if (
            not math.isfinite(self.slippage_bps)
            or not 0 <= self.slippage_bps < BASIS_POINTS
            or self.leverage < 1
        ):
            msg = "Invalid child leverage or slippage."
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class ChildPosition:
    """Own the entry state required to settle a mirrored position."""

    order: ChildOrder
    capital: float
    price: float
    quantity: float
    entry_fee: float


@dataclass(frozen=True, kw_only=True)
class ChildMark:
    """Record independently funded child equity at a candle close."""

    time: pd.Timestamp
    equity: float


@dataclass(kw_only=True)
class ChildAccount:
    """Apply father orders chronologically without child protection rules."""

    funding: ChildFunding
    cash: float = field(init=False)
    position: ChildPosition | None = None
    trades: list[Trade] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Fund this account independently of the father and other children."""
        self.cash = self.funding.capital

    def execute(self, order: ChildOrder, price: float) -> None:
        """Apply a mirrored entry or close using an adverse price adjustment."""
        direction = 1 if order.side == "Long" else -1
        if order.action == "close":
            direction = -direction
        fill = price * (1 + direction * self.funding.slippage_bps / 10000)
        if order.action == "open":
            self._open(order, fill)
        else:
            self._close(order, fill)

    def _open(self, order: ChildOrder, price: float) -> None:
        """Reserve margin and both estimated fees for the mirrored entry.

        Raises:
            ValueError: If the ledger overlaps or the child is depleted.

        """
        if self.position is not None or self.cash <= 0:
            msg = (
                "Cannot mirror an overlapping order or depleted child account."
            )
            raise ValueError(msg)
        notional = self.cash / (
            1 / self.funding.leverage + 2 * self.funding.fee_pct / 100
        )
        self.position = ChildPosition(
            order=order,
            capital=self.cash,
            price=price,
            quantity=notional / price,
            entry_fee=notional * self.funding.fee_pct / 100,
        )

    def _close(self, order: ChildOrder, price: float) -> None:
        """Settle using child prices while retaining the father's exit reason.

        Raises:
            ValueError: If no corresponding child position exists.

        """
        position = self.position
        if position is None or position.order.side != order.side:
            msg = "Child close does not match an open father position."
            raise ValueError(msg)
        direction = 1 if order.side == "Long" else -1
        pnl = (
            direction * position.quantity * (price - position.price)
            - position.entry_fee
            - position.quantity * price * self.funding.fee_pct / 100
        )
        self.cash = position.capital + pnl
        self.trades.append(
            Trade(
                deposit_size=position.capital,
                side=order.side,
                entry_time=position.order.time,
                exit_time=order.time,
                entry_price=position.price,
                exit_price=price,
                quantity=position.quantity,
                pnl=pnl,
                return_pct=pnl / position.capital * 100,
                exit_reason=order.reason,
            )
        )
        self.position = None

    def mark(self, price: float) -> float:
        """Value the child without applying independent exits.

        Returns:
            Cash plus unrealized P&L, net of paid entry fees.

        """
        position = self.position
        if position is None:
            return self.cash
        direction = 1 if position.order.side == "Long" else -1
        return (
            position.capital
            + direction * position.quantity * (price - position.price)
            - position.entry_fee
        )


@dataclass(frozen=True, kw_only=True)
class ChildResult:
    """Expose a child's settled trades and marked equity separately."""

    trades: tuple[Trade, ...]
    marks: tuple[ChildMark, ...]


def replay_child(
    prices: tuple[ChildPrice, ...],
    orders: tuple[ChildOrder, ...],
    *,
    funding: ChildFunding,
) -> ChildResult:
    """Replay father actions at matching child opens without filling gaps.

    Returns:
        The independent child account history.

    Raises:
        ValueError: If prices or action chronology cannot support the replay.

    """
    events: dict[pd.Timestamp, list[ChildOrder]] = {}
    if any(left.time > right.time for left, right in pairwise(orders)):
        msg = "Father orders must be chronological."
        raise ValueError(msg)
    for order in orders:
        events.setdefault(order.time, []).append(order)
    account = ChildAccount(funding=funding)
    marks: list[ChildMark] = []
    previous = None
    for price in prices:
        if previous is not None and price.time <= previous:
            msg = "Child prices must have unique chronological timestamps."
            raise ValueError(msg)
        if any(
            not math.isfinite(value) or value <= 0
            for value in (price.opening, price.close)
        ):
            msg = "Child prices must be finite and positive."
            raise ValueError(msg)
        for order in events.pop(price.time, []):
            account.execute(order, price.opening)
        marks.append(
            ChildMark(time=price.time, equity=account.mark(price.close))
        )
        previous = price.time
    if events or account.position is not None:
        msg = (
            "Missing child prices for father orders or an unclosed father"
            " order."
        )
        raise ValueError(msg)
    return ChildResult(trades=tuple(account.trades), marks=tuple(marks))
