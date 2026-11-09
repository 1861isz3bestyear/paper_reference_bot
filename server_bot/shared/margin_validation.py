# Copyright 2026 anchored-vwam-backtest contributors
"""Validate persisted isolated-margin account snapshots before resuming."""

from shared.hedge_validation import optional_float, optional_str
from shared.margin_types import (
    Contract,
    MarginOrder,
    MarginPosition,
    MarginSettings,
    MarginSnapshot,
    MarginTradeRecord,
    RiskTier,
    TradeRecord,
)
from shared.validation import (
    boolean,
    integer,
    number,
    object_list,
    object_mapping,
    string,
)


def parse_risk_tier(value: object) -> RiskTier:
    """Validate a saved risk tier.

    Returns:
        The account data with every field validated.

    """
    row = object_mapping(value)
    return RiskTier(
        limit=number(row["limit"]),
        mmr=number(row["mmr"]),
        deduction=number(row["deduction"]),
        max_leverage=number(row["max_leverage"]),
    )


def parse_contract(value: object) -> Contract:
    """Validate a saved contract.

    Returns:
        The account data with every field validated.

    """
    row = object_mapping(value)
    return Contract(
        symbol=string(row["symbol"]),
        fetched_at=string(row["fetched_at"]),
        max_leverage=number(row["max_leverage"]),
        leverage_step=number(row["leverage_step"]),
        quantity_step=number(row["quantity_step"]),
        min_quantity=number(row["min_quantity"]),
        min_notional=number(row["min_notional"]),
        max_quantity=number(row["max_quantity"]),
        price_tick=number(row["price_tick"]),
        funding_interval_minutes=integer(row["funding_interval_minutes"]),
        tiers=[parse_risk_tier(item) for item in object_list(row["tiers"])],
    )


def parse_margin_settings(value: object) -> MarginSettings:
    """Validate a saved margin settings.

    Returns:
        The account data with every field validated.

    """
    row = object_mapping(value)
    return MarginSettings(
        config_json=string(row["config_json"]),
        leverage=integer(row["leverage"]),
        slippage_bps=number(row["slippage_bps"]),
        fee_pct=number(row["fee_pct"]),
        strategy_mode=string(row["strategy_mode"]),
        band_sigma=number(row["band_sigma"]),
        exit_band_sigma=number(row["exit_band_sigma"]),
        trend_mode=boolean(row["trend_mode"]),
        open_position_side=string(row["open_position_side"]),
        stop_loss_pct=number(row["stop_loss_pct"]),
        stop_loss_basis=string(row["stop_loss_basis"]),
        minimum_order_size=number(row["minimum_order_size"]),
        take_profit_pct=number(row["take_profit_pct"]),
        allow_reentry=boolean(row["allow_reentry"]),
        hedge_entry_timeout_bars=integer(row["hedge_entry_timeout_bars"]),
    )


def parse_margin_position(value: object) -> MarginPosition:
    """Validate a saved margin position.

    Returns:
        The account data with every field validated.

    """
    row = object_mapping(value)
    return MarginPosition(
        quantity=number(row["quantity"]),
        entry_price=number(row["entry_price"]),
        entry_time=string(row["entry_time"]),
        margin=number(row["margin"]),
        entry_fee=number(row["entry_fee"]),
        reserve=number(row["reserve"]),
        allocation=number(row["allocation"]),
        funding_paid=number(row["funding_paid"]),
        target=optional_float(row["target"]),
        liquidation_price=number(row["liquidation_price"]),
        stop=optional_float(row["stop"]),
    )


def parse_margin_order(value: object) -> MarginOrder:
    """Validate a saved margin order.

    Returns:
        The account data with every field validated.

    """
    row = object_mapping(value)
    return MarginOrder(
        price=number(row["price"]),
        allocation=number(row["allocation"]),
        target=optional_float(row["target"]),
    )


def parse_trade_record(value: object) -> TradeRecord:
    """Validate a saved trade record.

    Returns:
        The account data with every field validated.

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


def parse_margin_trade_record(value: object) -> MarginTradeRecord:
    """Validate a saved margin trade record.

    Returns:
        The account data with every field validated.

    """
    row = object_mapping(value)
    return MarginTradeRecord(
        **parse_trade_record(row),
        leverage=integer(row["leverage"]),
        margin=number(row["margin"]),
        entry_notional=number(row["entry_notional"]),
        funding_paid=number(row["funding_paid"]),
        liquidation_price=number(row["liquidation_price"]),
        gross_pnl=number(row["gross_pnl"]),
        liquidation_adjustment=number(row["liquidation_adjustment"]),
    )


def parse_margin_snapshot(value: object) -> MarginSnapshot:
    """Validate a saved margin snapshot.

    Returns:
        The account data with every field validated.

    """
    row = object_mapping(value)
    return MarginSnapshot(
        cash=number(row["cash"]),
        settings=parse_margin_settings(row["settings"]),
        contract=parse_contract(row["contract"]),
        positions={
            key: parse_margin_position(item)
            for key, item in object_mapping(row["positions"]).items()
        },
        pending={
            key: parse_margin_order(item)
            for key, item in object_mapping(row["pending"]).items()
        },
        history=[
            parse_margin_trade_record(item)
            for item in object_list(row["history"])
        ],
        last_time=optional_str(row["last_time"]),
        touching=boolean(row["touching"]),
        pending_bars=integer(row["pending_bars"]),
        previous_close=optional_float(row["previous_close"]),
        previous_vwap=optional_float(row["previous_vwap"]),
        reentry_ready=boolean(row["reentry_ready"]),
        last_mark=optional_float(row["last_mark"]),
    )
