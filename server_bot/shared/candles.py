# Copyright 2026 anchored-vwam-backtest contributors
"""Typed candle rows for the sequential strategy engines."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class Candle:
    """Represent candle."""

    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    anchored_vwap: float = math.nan
    order_vwap: float = math.nan
    order_std: float = math.nan
    # Mark is for leverage engine only
    mark_open: float = math.nan
    mark_high: float = math.nan
    mark_low: float = math.nan
    mark_close: float = math.nan
    funding_rate: float = 0.0
    options_gex: float = math.nan
    options_gamma_flip: float = math.nan
    options_direction: float = math.nan
    options_entry: float = math.nan
    options_stop: float = math.nan
    options_intrabar: float = 0.0
    options_insurance: float = math.nan


def candle_rows(data: pd.DataFrame) -> Iterator[Candle]:
    """Convert frame columns before exposing scalar values to trading logic.

    Yields:
        Validated candle rows in chronological input order.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    times = pd.to_datetime(data["time"], utc=True)
    if times.isna().any():
        msg = "Candle timestamps must not be missing."
        raise ValueError(msg)
    required = ("open", "high", "low", "close")
    columns = {
        key: pd.to_numeric(data[key]).astype(float).tolist() for key in required
    }
    if any(
        not math.isfinite(value)
        for values in columns.values()
        for value in values
    ):
        msg = "Candle prices must be finite."
        raise ValueError(msg)
    optional = (
        "anchored_vwap",
        "order_vwap",
        "order_std",
        "mark_open",
        "mark_high",
        "mark_low",
        "mark_close",
        "funding_rate",
        "options_gex",
        "options_gamma_flip",
        "options_direction",
        "options_entry",
        "options_stop",
        "options_intrabar",
        "options_insurance",
    )
    for key in optional:
        columns[key] = (
            pd.to_numeric(data[key]).astype(float).tolist()
            if key in data
            else [0.0 if key == "funding_rate" else math.nan] * len(data)
        )
    for i, timestamp in enumerate(times):
        yield Candle(
            timestamp, *(columns[key][i] for key in (*required, *optional))
        )
