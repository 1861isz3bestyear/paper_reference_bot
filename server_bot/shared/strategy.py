# Copyright 2026 anchored-vwam-backtest contributors
"""Validate and dispatch historical strategy execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, time, tzinfo
from typing import TYPE_CHECKING, TypedDict, Unpack

import pandas as pd

from shared.gex import OPTIONS_GEX_MODE
from shared.gex_rules import GexHypothesis
from shared.hedge_strategy import (
    HEDGE_MODE,
    HedgeBacktestOptions,
    hedge_backtest,
)
from shared.leverage import leveraged_backtest
from shared.margin_types import Contract, MarginSettings
from shared.spot_execution import (
    BandSignals,
    CrossoverSignals,
    DailyGexSignals,
    GexRegimeSignals,
    OptionsGexSignals,
    SignedGexSignals,
    SpotRisk,
)
from shared.strategy_data import (
    BacktestWindow,
    prepare_backtest,
    validate_margin_data,
    validate_parameters,
    validate_side,
)
from shared.strategy_runner import run_spot_backtest
from shared.tunnel_execution import TunnelExecutionSettings, run_tunnel_backtest

if TYPE_CHECKING:
    from shared.models import Trade


@dataclass(frozen=True, kw_only=True)
class BacktestOptions:
    """Configure execution after the backtest timeline is selected."""

    initial_capital: float
    fee_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    allow_reentry: bool
    strategy_mode: str
    band_sigma: float
    active_weekdays: set[int] | None = None
    strategy_timezone: tzinfo = UTC
    active_session_start: time = time.min
    active_session_end: time = time.max
    exit_band_sigma: float = 0.0
    open_position_side: str = "Both"
    trend_mode: bool = False
    minimum_order_size: float = 0.0
    stop_loss_basis: str = "Position"
    hedge_entry_timeout_bars: int = 3
    leverage: int = 1
    slippage_bps: float = 0.0
    gex_hypothesis: GexHypothesis = GexHypothesis.REGIME
    contract: Contract | None = None


class RequiredBacktestKeywordOptions(TypedDict):
    """Required legacy backtest keyword names."""

    initial_capital: float
    fee_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    allow_reentry: bool
    strategy_mode: str
    band_sigma: float


class BacktestKeywordOptions(RequiredBacktestKeywordOptions, total=False):
    """Legacy keyword names accepted while callers migrate to options."""

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
    contract: Contract | None
    gex_hypothesis: GexHypothesis


def backtest(
    df: pd.DataFrame,
    strategy_start_at: pd.Timestamp,
    *,
    strategy_end_at: pd.Timestamp,
    options: BacktestOptions | None = None,
    **legacy: Unpack[BacktestKeywordOptions],
) -> tuple[pd.DataFrame, list[Trade]]:
    """Run a historical strategy through the existing public keyword API.

    Returns:
        The equity curve and completed trades.

    Raises:
        TypeError: If required options are missing or options are duplicated.
        ValueError: If the strategy or execution configuration is unsupported.

    """
    if options is None:
        required = {
            "initial_capital",
            "fee_pct",
            "stop_loss_pct",
            "take_profit_pct",
            "allow_reentry",
            "strategy_mode",
            "band_sigma",
        }
        missing = required - legacy.keys()
        if missing:
            msg = f"Missing backtest options: {', '.join(sorted(missing))}."
            raise TypeError(msg)
        options = BacktestOptions(
            initial_capital=legacy["initial_capital"],
            fee_pct=legacy["fee_pct"],
            stop_loss_pct=legacy["stop_loss_pct"],
            take_profit_pct=legacy["take_profit_pct"],
            allow_reentry=legacy["allow_reentry"],
            strategy_mode=legacy["strategy_mode"],
            band_sigma=legacy["band_sigma"],
            active_weekdays=legacy.get("active_weekdays"),
            strategy_timezone=legacy.get("strategy_timezone", UTC),
            active_session_start=legacy.get("active_session_start", time.min),
            active_session_end=legacy.get("active_session_end", time.max),
            exit_band_sigma=legacy.get("exit_band_sigma", 0.0),
            open_position_side=legacy.get("open_position_side", "Both"),
            trend_mode=legacy.get("trend_mode", False),
            minimum_order_size=legacy.get("minimum_order_size", 0.0),
            stop_loss_basis=legacy.get("stop_loss_basis", "Position"),
            hedge_entry_timeout_bars=legacy.get("hedge_entry_timeout_bars", 3),
            leverage=legacy.get("leverage", 1),
            slippage_bps=legacy.get("slippage_bps", 0.0),
            contract=legacy.get("contract"),
            gex_hypothesis=legacy.get("gex_hypothesis", GexHypothesis.REGIME),
        )
    elif legacy:
        msg = "Pass either options or legacy keyword options, not both."
        raise TypeError(msg)

    settings = MarginSettings(
        config_json="",
        leverage=options.leverage,
        slippage_bps=options.slippage_bps,
        fee_pct=options.fee_pct,
        strategy_mode=options.strategy_mode,
        band_sigma=options.band_sigma,
        exit_band_sigma=options.exit_band_sigma,
        trend_mode=options.trend_mode,
        open_position_side=options.open_position_side,
        stop_loss_pct=options.stop_loss_pct,
        stop_loss_basis=options.stop_loss_basis,
        minimum_order_size=options.minimum_order_size,
        take_profit_pct=options.take_profit_pct,
        allow_reentry=options.allow_reentry,
        hedge_entry_timeout_bars=options.hedge_entry_timeout_bars,
    )
    if options.strategy_mode == OPTIONS_GEX_MODE and options.leverage != 1:
        msg = "Options GEX supports unleveraged backtests only."
        raise ValueError(msg)
    validate_parameters(options.initial_capital, settings)
    prepared = prepare_backtest(
        df,
        window=BacktestWindow(
            start=pd.Timestamp(strategy_start_at),
            end=pd.Timestamp(strategy_end_at),
            weekdays=(
                None
                if options.active_weekdays is None
                else frozenset(options.active_weekdays)
            ),
            timezone=options.strategy_timezone,
            session_start=options.active_session_start,
            session_end=options.active_session_end,
        ),
    )
    if prepared.data.empty:
        return (
            pd.DataFrame(
                columns=["time", "equity", "position", "close", "anchored_vwap"]
            ),
            [],
        )
    if (
        options.strategy_mode == OPTIONS_GEX_MODE
        and options.gex_hypothesis == GexHypothesis.TUNNEL
    ):
        return run_tunnel_backtest(
            prepared,
            options.initial_capital,
            settings=TunnelExecutionSettings(
                risk=SpotRisk(
                    fee_pct=options.fee_pct,
                    stop_loss_pct=options.stop_loss_pct,
                    take_profit_pct=options.take_profit_pct,
                    minimum_order_size=options.minimum_order_size,
                    slippage_bps=options.slippage_bps,
                ),
                side=validate_side(options.open_position_side),
                allow_reentry=options.allow_reentry,
            ),
        )
    if options.leverage > 1:
        return leveraged_backtest(
            prepared.data,
            options.initial_capital,
            settings,
            contract=validate_margin_data(prepared.data, options.contract),
            scheduled_days=prepared.scheduled_days,
        )
    if options.strategy_mode == HEDGE_MODE:
        return hedge_backtest(
            prepared.data,
            options.initial_capital,
            options.fee_pct,
            options=HedgeBacktestOptions(
                target_sigma=options.exit_band_sigma,
                stop_loss_pct=options.stop_loss_pct,
                stop_loss_basis=options.stop_loss_basis,
                entry_timeout_bars=options.hedge_entry_timeout_bars,
                minimum_order_size=options.minimum_order_size,
                scheduled_days=prepared.scheduled_days,
                take_profit_pct=options.take_profit_pct,
            ),
        )
    signals = (
        {
            GexHypothesis.DAILY: DailyGexSignals,
            GexHypothesis.REGIME: GexRegimeSignals,
            GexHypothesis.FLIP: OptionsGexSignals,
            GexHypothesis.SIGN: SignedGexSignals,
        }[options.gex_hypothesis](
            side=validate_side(options.open_position_side)
        )
        if options.strategy_mode == OPTIONS_GEX_MODE
        else (
            CrossoverSignals(allow_reentry=options.allow_reentry)
            if options.strategy_mode == "AVWAP crossover"
            else BandSignals(
                entry_sigma=options.band_sigma,
                exit_sigma=options.exit_band_sigma,
                side=validate_side(options.open_position_side),
                trend=options.trend_mode,
            )
        )
    )
    return run_spot_backtest(
        prepared,
        options.initial_capital,
        risk=SpotRisk(
            fee_pct=options.fee_pct,
            stop_loss_pct=options.stop_loss_pct,
            take_profit_pct=options.take_profit_pct,
            minimum_order_size=options.minimum_order_size,
            slippage_bps=(
                options.slippage_bps
                if options.strategy_mode == OPTIONS_GEX_MODE
                else 0.0
            ),
        ),
        signals=signals,
    )
