# Copyright 2026 anchored-vwam-backtest contributors
"""Validated perpetual-contract and margin-account data structures."""

from __future__ import annotations

import math
from typing import TypedDict

MAX_LEVERAGE = 100
MAX_SLIPPAGE_BPS = 1_000


class RiskTier(TypedDict):
    """Represent risktier."""

    limit: float
    mmr: float
    deduction: float
    max_leverage: float


class Contract(TypedDict):
    """Represent contract."""

    symbol: str
    fetched_at: str
    max_leverage: float
    leverage_step: float
    quantity_step: float
    min_quantity: float
    min_notional: float
    max_quantity: float
    price_tick: float
    funding_interval_minutes: int
    tiers: list[RiskTier]


class MarginSettings(TypedDict):
    """Represent marginsettings."""

    config_json: str
    leverage: int
    slippage_bps: float
    fee_pct: float
    strategy_mode: str
    band_sigma: float
    exit_band_sigma: float
    trend_mode: bool
    open_position_side: str
    stop_loss_pct: float
    stop_loss_basis: str
    minimum_order_size: float
    take_profit_pct: float
    allow_reentry: bool
    hedge_entry_timeout_bars: int


class MarginPosition(TypedDict):
    """Represent marginposition."""

    quantity: float
    entry_price: float
    entry_time: str
    margin: float
    entry_fee: float
    reserve: float
    allocation: float
    funding_paid: float
    target: float | None
    liquidation_price: float
    stop: float | None


class MarginOrder(TypedDict):
    """Represent marginorder."""

    price: float
    allocation: float
    target: float | None


class TradeRecord(TypedDict):
    """Represent traderecord."""

    deposit_size: float
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    return_pct: float
    exit_reason: str
    entry_fee: float
    exit_fee: float


class MarginTradeRecord(TradeRecord):
    """Represent margintraderecord."""

    leverage: int
    margin: float
    entry_notional: float
    funding_paid: float
    liquidation_price: float
    gross_pnl: float
    liquidation_adjustment: float


class MarginSnapshot(TypedDict):
    """Represent marginsnapshot."""

    cash: float
    settings: MarginSettings
    contract: Contract
    positions: dict[str, MarginPosition]
    pending: dict[str, MarginOrder]
    history: list[MarginTradeRecord]
    last_time: str | None
    touching: bool
    pending_bars: int
    previous_close: float | None
    previous_vwap: float | None
    reentry_ready: bool
    last_mark: float | None


def validate_leverage(leverage: object, slippage_bps: float) -> None:
    """Handle validate leverage.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    if (
        isinstance(leverage, bool)
        or not isinstance(leverage, int)
        or not 1 <= leverage <= MAX_LEVERAGE
    ):
        msg = "Leverage must be an integer: 1 (off), or 2\N{EN DASH}100."
        raise ValueError(msg)
    if (
        not math.isfinite(slippage_bps)
        or not 0 <= slippage_bps <= MAX_SLIPPAGE_BPS
    ):
        msg = "Slippage must be finite and between 0 and 1000 basis points."
        raise ValueError(msg)
