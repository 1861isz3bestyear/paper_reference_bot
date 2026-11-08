# Copyright 2026 anchored-vwam-backtest contributors
"""Verify authoritative backtest behavior in the deployed shared engine."""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
import pytest

from shared.open_interest import confirm_open_interest

DAY = pd.Timestamp("2026-01-01", tz="UTC")


def test_confirmation_direction_staleness_and_no_lookahead() -> None:
    """Verify confirmation direction staleness and no lookahead."""
    oi = pd.DataFrame({
        "time": pd.date_range(DAY, periods=5, freq="5min"),
        "open_interest": [100, 101, 102, 103, 500],
    })
    times = pd.date_range(DAY, periods=35, freq="min")
    prices = pd.DataFrame({"time": times, "close": np.arange(100, 135)})
    bars = pd.DataFrame({"time": times, "zone": "Longs"})
    result = confirm_open_interest(bars, oi, prices)
    if result.oi_confirmation.iloc[0] != "Unavailable":
        pytest.fail("Expected result.oi_confirmation.iloc[0] == 'Unavailable'")
    if result.oi_confirmation.iloc[16] != "Longs":
        pytest.fail("Expected result.oi_confirmation.iloc[16] == 'Longs'")
    expected_value = 103
    if result.open_interest.iloc[20] != expected_value:
        pytest.fail("Expected result.open_interest.iloc[20] == expected_value")
    expected_value = 500
    if result.open_interest.iloc[21] != expected_value:
        pytest.fail("Expected result.open_interest.iloc[21] == expected_value")
    if result.oi_confirmation.iloc[-1] != "Unavailable":
        pytest.fail("Expected result.oi_confirmation.iloc[-1] == 'Unavailable'")
    earlier = confirm_open_interest(
        bars.iloc[:21], oi.iloc[:4], prices.iloc[:21]
    )
    pd.testing.assert_frame_equal(result.iloc[:21], earlier)
    prices["close"] = np.arange(135, 100, -1)
    bars["zone"] = "Shorts"
    if (
        confirm_open_interest(bars, oi, prices).oi_confirmation.iloc[16]
        != "Shorts"
    ):
        pytest.fail(
            "Expected confirm_open_interest(bars, oi,"
            " prices).oi_confirmation.iloc[16] == 'Shorts'"
        )
    oi["open_interest"] = 100
    if (
        confirm_open_interest(bars, oi, prices).oi_confirmation.iloc[16]
        != "Neutral"
    ):
        pytest.fail(
            "Expected confirm_open_interest(bars, oi,"
            " prices).oi_confirmation.iloc[16] == 'Neutral'"
        )


@pytest.mark.parametrize("bar_unit", ["s", "us", "ns"])
@pytest.mark.parametrize("oi_unit", ["s", "us", "ns"])
@pytest.mark.parametrize("price_unit", ["s", "us", "ns"])
def test_confirmation_accepts_mixed_timestamp_resolutions(
    bar_unit: Literal["s", "us", "ns"],
    oi_unit: Literal["s", "us", "ns"],
    price_unit: Literal["s", "us", "ns"],
) -> None:
    """Causal confirmation must survive different pandas timestamp units."""
    bars = pd.DataFrame(
        {"time": [DAY + pd.Timedelta(minutes=16)], "zone": ["Longs"]}
    )
    oi = pd.DataFrame({
        "time": [DAY, DAY + pd.Timedelta(minutes=15)],
        "open_interest": [100.0, 110.0],
    })
    prices = pd.DataFrame({
        "time": [DAY + pd.Timedelta(minutes=1), DAY + pd.Timedelta(minutes=16)],
        "close": [100.0, 105.0],
    })
    units: tuple[Literal["s", "us", "ns"], ...] = (
        bar_unit,
        oi_unit,
        price_unit,
    )
    for frame, unit in zip((bars, oi, prices), units, strict=True):
        frame["time"] = frame["time"].astype(
            pd.DatetimeTZDtype(unit=unit, tz="UTC")
        )
    originals = [frame.copy(deep=True) for frame in (bars, oi, prices)]
    result = confirm_open_interest(bars, oi, prices)
    if result["oi_confirmation"].iloc[0] != "Longs":
        pytest.fail("Expected result['oi_confirmation'].iloc[0] == 'Longs'")
    if result["oi_change_pct"].iloc[0] != pytest.approx(10):
        pytest.fail(
            "Expected result['oi_change_pct'].iloc[0] == pytest.approx(10)"
        )
    if result["oi_price_change_pct"].iloc[0] != pytest.approx(5):
        pytest.fail(
            "Expected result['oi_price_change_pct'].iloc[0] == pytest.approx(5)"
        )
    for frame, original in zip((bars, oi, prices), originals, strict=True):
        pd.testing.assert_frame_equal(frame, original)
