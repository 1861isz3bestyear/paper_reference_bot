# Copyright 2026 anchored-vwam-backtest contributors
"""Persisted exchange order intents and hedge execution cycles."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from shared.hedge_types import HedgePlan
from shared.hedge_validation import optional_str, parse_hedge_plan
from shared.validation import boolean, object_mapping, string


class ReplicaOrder(TypedDict):
    """An order intent saved before submitting to the exchange."""

    link: str
    submitted_at: NotRequired[str]
    expected_side: NotRequired[str | None]
    expected_quantity: NotRequired[str]
    reference_id: NotRequired[str | None]


class ReplicaHedgePlan(HedgePlan):
    """A validated reference plan identified across producer restarts."""

    key: str


class HedgeExecutionOrder(TypedDict):
    """A rounded exchange hedge order and its reconciliation progress."""

    link: str
    submitted_at: str | None
    quantity: str
    price: str
    tp: str
    sl: str
    seen_position: NotRequired[bool]


class HedgeCycle(TypedDict):
    """One tracked hedge cycle with independently reconciled leg orders."""

    key: str
    plan: ReplicaHedgePlan
    orders: dict[str, HedgeExecutionOrder]
    complete: bool


def parse_replica_order(value: object) -> ReplicaOrder:
    """Validate a saved single-position exchange intent.

    Returns:
        The saved intent with validated field types.

    """
    row = object_mapping(value)
    order = ReplicaOrder(link=string(row["link"]))
    if "submitted_at" in row:
        order["submitted_at"] = string(row["submitted_at"])
    if "expected_side" in row:
        order["expected_side"] = optional_str(row["expected_side"])
    if "expected_quantity" in row:
        order["expected_quantity"] = string(row["expected_quantity"])
    if "reference_id" in row:
        order["reference_id"] = optional_str(row["reference_id"])
    return order


def parse_replica_plan(value: object) -> ReplicaHedgePlan:
    """Validate a hedge plan saved as part of an exchange execution cycle.

    Returns:
        The reference plan and its cross-restart identity.

    """
    row = object_mapping(value)
    return ReplicaHedgePlan(**parse_hedge_plan(row), key=string(row["key"]))


def parse_execution_order(value: object) -> HedgeExecutionOrder:
    """Validate a saved hedge leg order.

    Returns:
        The rounded order and its persisted progress.

    """
    row = object_mapping(value)
    order = HedgeExecutionOrder(
        link=string(row["link"]),
        submitted_at=optional_str(row["submitted_at"]),
        quantity=string(row["quantity"]),
        price=string(row["price"]),
        tp=string(row["tp"]),
        sl=string(row["sl"]),
    )
    if "seen_position" in row:
        order["seen_position"] = boolean(row["seen_position"])
    return order


def parse_hedge_cycle(value: object) -> HedgeCycle:
    """Validate persisted hedge execution state before resuming orders.

    Returns:
        A complete, typed execution cycle.

    """
    row = object_mapping(value)
    return HedgeCycle(
        key=string(row["key"]),
        plan=parse_replica_plan(row["plan"]),
        orders={
            side: parse_execution_order(order)
            for side, order in object_mapping(row["orders"]).items()
        },
        complete=boolean(row["complete"]),
    )
