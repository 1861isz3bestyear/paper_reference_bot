# Copyright 2026 anchored-vwam-backtest contributors
"""Keyword schemas for strategy test scenarios."""

from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from datetime import time, tzinfo

    import pandas as pd

    from shared.gex_rules import GexHypothesis
    from shared.margin_types import Contract


class BacktestOptions(TypedDict):
    """Complete options accepted by the backtest scenario builder."""

    strategy_start_at: pd.Timestamp
    strategy_end_at: pd.Timestamp
    initial_capital: float
    fee_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    allow_reentry: bool
    strategy_mode: str
    band_sigma: float
    active_weekdays: NotRequired[set[int] | None]
    strategy_timezone: NotRequired[tzinfo]
    active_session_start: NotRequired[time]
    active_session_end: NotRequired[time]
    exit_band_sigma: NotRequired[float]
    open_position_side: NotRequired[str]
    trend_mode: NotRequired[bool]
    minimum_order_size: NotRequired[float]
    stop_loss_basis: NotRequired[str]
    hedge_entry_timeout_bars: NotRequired[int]
    leverage: NotRequired[int]
    slippage_bps: NotRequired[float]
    gex_hypothesis: NotRequired[GexHypothesis]
    contract: NotRequired[Contract | None]


class BacktestChanges(TypedDict, total=False):
    """Optional overrides for a backtest scenario."""

    strategy_start_at: pd.Timestamp
    strategy_end_at: pd.Timestamp
    initial_capital: float
    fee_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    allow_reentry: bool
    strategy_mode: str
    band_sigma: float
    active_weekdays: set[int] | None
    strategy_timezone: tzinfo
    active_session_start: time
    active_session_end: time
    exit_band_sigma: float
    open_position_side: str
    trend_mode: bool
    minimum_order_size: float
    stop_loss_basis: str
    hedge_entry_timeout_bars: int
    leverage: int
    slippage_bps: float
    gex_hypothesis: GexHypothesis
    contract: Contract | None


class MarginChanges(TypedDict, total=False):
    """Optional overrides for a margin scenario."""

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


class CandleChanges(TypedDict, total=False):
    """Optional overrides for a generated candle."""

    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    anchored_vwap: float
    order_vwap: float
    order_std: float
    mark_open: float
    mark_high: float
    mark_low: float
    mark_close: float
    funding_rate: float
