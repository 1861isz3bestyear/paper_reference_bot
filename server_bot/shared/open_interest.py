# Copyright 2026 anchored-vwam-backtest contributors
"""Provide open interest behavior."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

"""Causal OI confirmation: expanding positions do not imply direction alone."""

MINIMUM_LOOKBACK_MINUTES = 5


@dataclass(frozen=True, kw_only=True)
class OpenInterestOptions:
    """Configure causal open-interest confirmation."""

    lookback: int = 15
    minimum_growth: float = 0.1
    max_age: int = 10


def confirm_open_interest(
    bars: pd.DataFrame,
    snapshots: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    options: OpenInterestOptions | None = None,
) -> pd.DataFrame:
    """Handle confirm open interest.

    Returns:
        The resulting value.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    config = options or OpenInterestOptions()
    if (
        config.lookback < MINIMUM_LOOKBACK_MINUTES
        or config.max_age <= 0
        or not np.isfinite(
            [config.lookback, config.minimum_growth, config.max_age]
        ).all()
        or config.minimum_growth < 0
    ):
        msg = (
            "OI requires lookback >= 5 minutes, positive maximum age, and"
            " nonnegative growth threshold."
        )
        raise ValueError(msg)
    result = bars.sort_values("time").reset_index(drop=True).copy()
    oi = snapshots.sort_values("time").copy()
    price = prices.sort_values("time").copy()
    for frame in (result, oi, price):
        frame["time"] = pd.to_datetime(frame["time"], utc=True).astype(
            "datetime64[ns, UTC]"
        )
    # Historical sample timestamps are not publication timestamps. Allow a
    # minute.
    oi["available"] = oi["time"] + pd.Timedelta(minutes=1)
    current = pd.merge_asof(
        result[["time"]],
        oi.rename(columns={"time": "oi_time"}),
        left_on="time",
        right_on="available",
        direction="backward",
    )
    reference_times = pd.to_datetime(
        current["oi_time"], utc=True
    ) - pd.Timedelta(minutes=config.lookback)
    valid = reference_times.notna()
    reference = pd.Series(np.nan, index=result.index)
    if valid.any():
        matched = pd.merge_asof(
            pd.DataFrame({"time": reference_times[valid]}),
            oi[["time", "open_interest"]],
            on="time",
            direction="backward",
            tolerance=pd.Timedelta(minutes=5),
        )
        reference.loc[valid] = matched["open_interest"].to_numpy()
    result["open_interest"] = current["open_interest"]
    result["oi_age_minutes"] = (
        pd.to_datetime(result["time"], utc=True)
        - pd.to_datetime(current["oi_time"], utc=True)
    ).dt.total_seconds() / 60
    result["oi_change_pct"] = (
        current["open_interest"] / reference.where(reference > 0) - 1
    ) * 100

    def price_at(times: pd.Series[pd.Timestamp]) -> pd.Series[float]:
        return pd.merge_asof(
            pd.DataFrame({"time": times}),
            price,
            on="time",
            direction="backward",
            tolerance=pd.Timedelta(minutes=1),
        )["close"].astype(float)

    current_price = price_at(pd.to_datetime(result["time"], utc=True))
    old_price = price_at(
        pd.to_datetime(result["time"], utc=True)
        - pd.Timedelta(minutes=config.lookback)
    )
    result["oi_price_change_pct"] = (
        current_price / old_price.where(old_price > 0) - 1
    ) * 100
    usable = (
        (result["oi_age_minutes"] <= config.max_age)
        & result["oi_change_pct"].notna()
        & result["oi_price_change_pct"].notna()
    )
    growth = (result["oi_change_pct"] > 0) & (
        result["oi_change_pct"] >= config.minimum_growth
    )
    result["oi_confirmation"] = "Neutral"
    result.loc[
        usable
        & growth
        & result["zone"].eq("Longs")
        & (result["oi_price_change_pct"] > 0),
        "oi_confirmation",
    ] = "Longs"
    result.loc[
        usable
        & growth
        & result["zone"].eq("Shorts")
        & (result["oi_price_change_pct"] < 0),
        "oi_confirmation",
    ] = "Shorts"
    result.loc[
        ~usable | result["zone"].eq("Unavailable"), "oi_confirmation"
    ] = "Unavailable"
    return result
