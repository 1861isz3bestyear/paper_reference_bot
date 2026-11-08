# Copyright 2026 anchored-vwam-backtest contributors
"""GEX indicator foundation. Zones are directional filters, not trade orders."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

ORDER_FLOW_MODE = "Trade/Book imbalance"
GEX_MODE = "GEX"
OPTIONS_GEX_MODE = GEX_MODE
MILLISECOND_TIMESTAMP_THRESHOLD = 10_000_000_000


@dataclass(frozen=True, kw_only=True)
class GexOptions:
    """Configure GEX interpretation and zone thresholds."""

    resting: bool = False
    invert: bool = False
    short: float = -0.2
    long: float = 0.2


def _validated_gex_frame(
    source: pd.DataFrame, *, resting: bool
) -> pd.DataFrame:
    """Copy and validate the externally supplied GEX records.

    Returns:
        A validated copy of the records.

    Raises:
        ValueError: If required fields or supported symbols are invalid.

    """
    frame = source.copy()
    required = (
        {"time", "bid_size", "ask_size"}
        if resting
        else {"timestamp", "side", "size"}
    )
    if not required.issubset(frame):
        raise ValueError("Required CSV columns: " + ", ".join(sorted(required)))
    if frame.empty:
        msg = "GEX input is empty."
        raise ValueError(msg)
    if "symbol" in frame and not frame["symbol"].eq("BTCUSDT").all():
        msg = "GEX currently accepts Bybit BTCUSDT only."
        raise ValueError(msg)
    return frame


def _normalize_gex_values(frame: pd.DataFrame, *, resting: bool) -> list[str]:
    """Normalize timestamps and quantities at the dataframe boundary.

    Returns:
        The normalized quantity-column names.

    Raises:
        ValueError: If timestamps or quantities are invalid.

    """
    timestamps = frame["time" if resting else "timestamp"]
    if pd.api.types.is_numeric_dtype(timestamps):
        numeric_timestamps = pd.to_numeric(timestamps).astype(float)
        unit = (
            "ms"
            if numeric_timestamps.median() > MILLISECOND_TIMESTAMP_THRESHOLD
            else "s"
        )
        frame["time"] = pd.to_datetime(
            timestamps, unit=unit, utc=True, errors="coerce"
        )
    else:
        frame["time"] = pd.to_datetime(
            timestamps, utc=True, errors="coerce", format="mixed"
        )
    columns = ["bid_size", "ask_size"] if resting else ["size"]
    frame[columns] = frame[columns].apply(pd.to_numeric, errors="coerce")
    invalid_quantity = (
        not np.isfinite(frame[columns].to_numpy(dtype=float)).all()
        or (frame[columns] < 0).any().any()
    )
    if frame["time"].isna().any() or invalid_quantity:
        msg = "GEX requires valid timestamps and finite nonnegative quantities."
        raise ValueError(msg)
    return columns


def _executed_trade_sizes(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate executed trades and derive bid/ask aggressor sizes.

    Returns:
        Records with duplicate IDs removed and aggressor sizes added.

    Raises:
        ValueError: If trade sides or IDs are invalid.

    """
    if not frame["side"].isin(["Buy", "Sell"]).all():
        msg = "Bybit trade side must be Buy or Sell (taker side)."
        raise ValueError(msg)
    if "trdMatchID" in frame:
        if frame["trdMatchID"].isna().any():
            msg = "Trade IDs must not be missing."
            raise ValueError(msg)
        duplicates = frame[frame.duplicated("trdMatchID", keep=False)]
        values = duplicates[["time", "side", "size"]]
        first_values = values.groupby(duplicates["trdMatchID"]).transform(
            "first"
        )
        if not duplicates.empty and values.ne(first_values).any().any():
            msg = "Conflicting duplicate trade IDs."
            raise ValueError(msg)
        frame = frame.drop_duplicates("trdMatchID").copy()
    frame["bid_size"] = frame["size"].where(frame["side"].eq("Sell"), 0)
    frame["ask_size"] = frame["size"].where(frame["side"].eq("Buy"), 0)
    return frame


def aggregate_gex(
    source: pd.DataFrame,
    interval: str,
    *,
    options: GexOptions | None = None,
) -> pd.DataFrame:
    """Aggregate executed volumes, or take each bar's last recorded book.

    snapshot.

    Output timestamps are bar closes: observations cannot be used before then.
    Book quantities need consistent depth and BTC units throughout the input.

    Returns:
        The computed result.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    config = options or GexOptions()
    if not -1 <= config.short < config.long <= 1:
        msg = "Thresholds must satisfy -1 <= short < long <= 1."
        raise ValueError(msg)
    frame = _validated_gex_frame(source, resting=config.resting)
    _ = _normalize_gex_values(frame, resting=config.resting)
    if not config.resting:
        frame = _executed_trade_sizes(frame)
    grouped = (
        frame.sort_values("time", kind="stable")
        .set_index("time")[["bid_size", "ask_size"]]
        .resample(interval, closed="left", label="right")
    )
    result = grouped.last() if config.resting else grouped.sum(min_count=1)
    total = result["bid_size"].astype(float) + result["ask_size"].astype(float)
    if np.isinf(total.to_numpy(dtype=float)).any():
        msg = "Aggregated quantities exceed the supported numeric range."
        raise ValueError(msg)
    result["obi"] = (result["bid_size"] - result["ask_size"]) / total.where(
        total > 0
    )
    result["oscillator"] = (
        result["obi"]
        * (1 if config.resting else -1)
        * (-1 if config.invert else 1)
    )
    result["zone"] = "None"
    result.loc[result["oscillator"] <= config.short, "zone"] = "Shorts"
    result.loc[result["oscillator"] >= config.long, "zone"] = "Longs"
    result.loc[result["oscillator"].isna(), "zone"] = "Unavailable"
    return result.reset_index()
