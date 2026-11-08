# Copyright 2026 anchored-vwam-backtest contributors
"""Validate hedge state before resuming a persisted account."""

from __future__ import annotations

from shared.hedge_types import (
    HedgeOrder,
    HedgePlan,
    HedgePosition,
    HedgeSnapshot,
)
from shared.margin_types import TradeRecord
from shared.validation import (
    boolean,
    integer,
    number,
    object_list,
    object_mapping,
    string,
)


def optional_str(value: object) -> str | None:
    """Handle optional str.

    Returns:
        The resulting value.

    """
    return None if value is None else string(value)


def optional_float(value: object) -> float | None:
    """Handle optional float.

    Returns:
        The resulting value.

    """
    return None if value is None else number(value)


def parse_trade_record(value: object) -> TradeRecord:
    """Handle parse TradeRecord.

    Returns:
        The resulting value.

    """
    row = object_mapping(value)
    return TradeRecord(
        deposit_size=number(row["deposit_size"]),
        side=string(row["side"]),
        entry_time=string(row["entry_time"]),
        exit_time=string(row["exit_time"]),
        entry_price=number(row["entry_price"]),
        exit_price=number(row["exit_price"]),
        quantity=number(row["quantity"]),
        pnl=number(row["pnl"]),
        return_pct=number(row["return_pct"]),
        exit_reason=string(row["exit_reason"]),
        entry_fee=number(row["entry_fee"]),
        exit_fee=number(row["exit_fee"]),
    )


def parse_hedge_order(value: object) -> HedgeOrder:
    """Handle parse HedgeOrder.

    Returns:
        The resulting value.

    """
    row = object_mapping(value)
    return HedgeOrder(
        entry_price=number(row["entry_price"]),
        quantity=number(row["quantity"]),
        allocation=number(row["allocation"]),
        take_profit=number(row["take_profit"]),
        stop_price=optional_float(row["stop_price"]),
    )


def parse_hedge_position(value: object) -> HedgePosition:
    """Handle parse HedgePosition.

    Returns:
        The resulting value.

    """
    row = object_mapping(value)
    return HedgePosition(
        entry_price=number(row["entry_price"]),
        quantity=number(row["quantity"]),
        allocation=number(row["allocation"]),
        take_profit=number(row["take_profit"]),
        stop_price=optional_float(row["stop_price"]),
        entry_fee=number(row["entry_fee"]),
        entry_time=string(row["entry_time"]),
        take_profit_reason=string(
            row.get("take_profit_reason", "Reached hedge TP band")
        ),
    )


def parse_hedge_plan(value: object) -> HedgePlan:
    """Handle parse HedgePlan.

    Returns:
        The resulting value.

    """
    row = object_mapping(value)
    return HedgePlan(
        id=string(row["id"]),
        created_at=string(row["created_at"]),
        deposit=number(row["deposit"]),
        legs={
            key: parse_hedge_order(item)
            for key, item in object_mapping(row["legs"]).items()
        },
        entry_timeout_bars=integer(row["entry_timeout_bars"]),
        fee_pct=number(row["fee_pct"]),
    )


def parse_hedge_snapshot(value: object) -> HedgeSnapshot:
    """Handle parse HedgeSnapshot.

    Returns:
        The resulting value.

    """
    row = object_mapping(value)
    return HedgeSnapshot(
        cash=number(row["cash"]),
        fee_pct=number(row["fee_pct"]),
        target_sigma=number(row["target_sigma"]),
        stop_loss_pct=number(row["stop_loss_pct"]),
        stop_loss_basis=string(row["stop_loss_basis"]),
        entry_timeout_bars=integer(row["entry_timeout_bars"]),
        minimum_order_size=number(row["minimum_order_size"]),
        positions={
            key: parse_hedge_position(item)
            for key, item in object_mapping(row["positions"]).items()
        },
        pending={
            key: parse_hedge_order(item)
            for key, item in object_mapping(row["pending"]).items()
        },
        history=[
            parse_trade_record(item) for item in object_list(row["history"])
        ],
        plan=(None if row["plan"] is None else parse_hedge_plan(row["plan"])),
        touching=boolean(row["touching"]),
        pending_bars=integer(row["pending_bars"]),
        last_time=optional_str(row["last_time"]),
        take_profit_pct=number(row.get("take_profit_pct", 0.0)),
    )
