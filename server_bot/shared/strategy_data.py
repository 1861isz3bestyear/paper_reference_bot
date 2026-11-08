# Copyright 2026 anchored-vwam-backtest contributors
"""Validate backtest inputs and select chronological trading sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, time, tzinfo
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from shared.gex import OPTIONS_GEX_MODE
from shared.hedge_types import HEDGE_MODE
from shared.margin_types import Contract, MarginSettings, validate_leverage

if TYPE_CHECKING:
    from shared.spot_execution import AllowedSide

MAXIMUM_PERCENT = 100


@dataclass(frozen=True, kw_only=True)
class BacktestWindow:
    """Select a timeline and optional recurring local trading sessions."""

    start: pd.Timestamp
    end: pd.Timestamp
    weekdays: frozenset[int] | None = None
    timezone: tzinfo = UTC
    session_start: time = time.min
    session_end: time = time.max


@dataclass(frozen=True, kw_only=True)
class PreparedBacktest:
    """Keep selected candles aligned with their originating session dates."""

    data: pd.DataFrame
    scheduled_days: pd.Series[date] | None
    window: BacktestWindow


def validate_side(value: str) -> AllowedSide:
    """Validate an external side selection before exposing a domain value.

    Returns:
        The recognized side selection.

    Raises:
        ValueError: If the selection is not supported.

    """
    if value == "Long":
        return value
    if value == "Short":
        return value
    if value == "Both":
        return value
    msg = "Open position side must be Long, Short, or Both."
    raise ValueError(msg)


def validate_parameters(capital: float, settings: MarginSettings) -> None:
    """Check the common numerical and mode inputs before selecting a runner.

    Raises:
        ValueError: If a parameter is outside its supported domain.

    """
    validate_leverage(settings["leverage"], settings["slippage_bps"])
    if not np.isfinite(capital) or capital <= 0:
        msg = "Initial capital must be finite and positive."
        raise ValueError(msg)
    if (
        not np.isfinite(settings["fee_pct"])
        or not 0 <= settings["fee_pct"] < MAXIMUM_PERCENT
    ):
        msg = "Fee percentage must be finite and between 0 and 100."
        raise ValueError(msg)
    for value in (
        settings["stop_loss_pct"],
        settings["take_profit_pct"],
        settings["minimum_order_size"],
        settings["band_sigma"],
    ):
        if not np.isfinite(value) or value < 0:
            msg = (
                "Risk, band, and minimum order parameters must be finite and"
                " nonnegative."
            )
            raise ValueError(msg)
    if not np.isfinite(settings["exit_band_sigma"]):
        msg = "Close-order VWAP sigma must be finite."
        raise ValueError(msg)
    if (
        settings["exit_band_sigma"] < 0
        and settings["strategy_mode"] != "VWAP band mean reversion"
    ):
        msg = (
            "Negative close-order VWAP sigma is only valid for mean reversion."
        )
        raise ValueError(msg)
    _ = validate_side(settings["open_position_side"])
    if settings["strategy_mode"] not in {
        "AVWAP crossover",
        "VWAP band mean reversion",
        HEDGE_MODE,
        OPTIONS_GEX_MODE,
    }:
        msg = "Unsupported strategy mode."
        raise ValueError(msg)


def validate_prices(data: pd.DataFrame) -> None:
    """Reject invalid OHLC ranges and nonchronological candle times.

    Raises:
        ValueError: If prices or timestamps cannot represent valid candles.

    """
    prices = data[["open", "high", "low", "close"]]
    if (
        not np.isfinite(prices.to_numpy(dtype=float)).all()
        or (prices <= 0).any().any()
        or (data["low"] > prices.min(axis=1)).any()
        or (data["high"] < prices.max(axis=1)).any()
    ):
        msg = (
            "Candles must contain finite positive prices with valid OHLC"
            " ranges."
        )
        raise ValueError(msg)
    if (
        data["time"].isna().any()
        or not data["time"].is_monotonic_increasing
        or data["time"].duplicated().any()
    ):
        msg = "Candle times must be unique, valid, and increasing."
        raise ValueError(msg)


def prepare_backtest(
    data: pd.DataFrame, *, window: BacktestWindow
) -> PreparedBacktest:
    """Shift indicators before filtering to retain warmup order levels.

    Returns:
        Selected candles and their aligned session dates.

    Raises:
        ValueError: If the timeline is empty or reversed.

    """
    validate_prices(data)
    if window.start >= window.end:
        msg = "Strategy start must be before strategy end."
        raise ValueError(msg)
    data = data.copy()
    data["order_vwap"] = data["anchored_vwap"].shift(1)
    data["order_std"] = data["anchored_std"].shift(1)
    data = data[
        (data["time"] >= window.start) & (data["time"] <= window.end)
    ].copy()
    if window.weekdays is not None:
        return select_sessions(data, window=window)
    return PreparedBacktest(
        data=data.reset_index(drop=True), scheduled_days=None, window=window
    )


def select_sessions(
    data: pd.DataFrame, *, window: BacktestWindow
) -> PreparedBacktest:
    """Associate overnight sessions with the weekday on which they start.

    Returns:
        Session candles and aligned local start dates.

    Raises:
        ValueError: If weekdays or session boundaries are invalid.

    """
    if not window.weekdays:
        return PreparedBacktest(
            data=data.iloc[:0], scheduled_days=None, window=window
        )
    if not window.weekdays.issubset(set(range(7))):
        msg = (
            "Active weekdays must use values from 0 (Monday) through 6"
            " (Sunday)."
        )
        raise ValueError(msg)
    if window.session_start == window.session_end:
        msg = "Trade start and end times must be different."
        raise ValueError(msg)
    local_times = pd.to_datetime(data["time"], utc=True).dt.tz_convert(
        window.timezone
    )
    if window.session_start < window.session_end:
        mask = (
            local_times.dt.weekday.isin(window.weekdays)
            & (local_times.dt.time >= window.session_start)
            & (local_times.dt.time <= window.session_end)
        )
        days = local_times.dt.date
    else:
        starts_today = local_times.dt.weekday.isin(window.weekdays) & (
            local_times.dt.time >= window.session_start
        )
        started_yesterday = ((local_times.dt.weekday - 1) % 7).isin(
            window.weekdays
        ) & (local_times.dt.time <= window.session_end)
        mask = starts_today | started_yesterday
        days = local_times.dt.date.where(
            starts_today, (local_times - pd.Timedelta(days=1)).dt.date
        )
    return PreparedBacktest(
        data=data.loc[mask].reset_index(drop=True),
        scheduled_days=days.loc[mask].reset_index(drop=True),
        window=window,
    )


def validate_margin_data(
    data: pd.DataFrame, contract: Contract | None
) -> Contract:
    """Require mark prices, funding history, and a contract for leveraged runs.

    Returns:
        The supplied contract once required market data is present.

    Raises:
        ValueError: If required market data is missing or invalid.

    """
    required = [
        "mark_open",
        "mark_high",
        "mark_low",
        "mark_close",
        "funding_rate",
    ]
    if contract is None or any(key not in data for key in required):
        msg = (
            "Leverage requires contract risk tiers, mark prices and historical"
            " funding."
        )
        raise ValueError(msg)
    if (
        not np.isfinite(data[required].to_numpy(dtype=float)).all()
        or (data[required[:4]] <= 0).any().any()
    ):
        msg = "Invalid mark prices or funding rates."
        raise ValueError(msg)
    return contract
