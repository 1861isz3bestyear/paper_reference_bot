# Copyright 2026 anchored-vwam-backtest contributors
"""Hedge-cycle orders and persisted account state."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from shared.margin_types import TradeRecord

HEDGE_MODE = "VWAP zero hedge"


class HedgeOrder(TypedDict):
    """Represent hedgeorder."""

    entry_price: float
    quantity: float
    allocation: float
    take_profit: float
    stop_price: float | None


class HedgePosition(HedgeOrder):
    """Represent hedgeposition."""

    entry_fee: float
    entry_time: str
    take_profit_reason: str


class HedgePlan(TypedDict):
    """Represent hedgeplan."""

    id: str
    created_at: str
    deposit: float
    legs: dict[str, HedgeOrder]
    entry_timeout_bars: int
    fee_pct: float


class HedgeSnapshot(TypedDict):
    """Represent hedgesnapshot."""

    cash: float
    fee_pct: float
    target_sigma: float
    stop_loss_pct: float
    stop_loss_basis: str
    entry_timeout_bars: int
    minimum_order_size: float
    positions: dict[str, HedgePosition]
    pending: dict[str, HedgeOrder]
    history: list[TradeRecord]
    plan: HedgePlan | None
    touching: bool
    pending_bars: int
    last_time: str | None
    take_profit_pct: float
