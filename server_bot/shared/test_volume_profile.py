# Copyright 2026 anchored-vwam-backtest contributors
"""Test volume profile."""

from typing import cast

import numpy as np
import pandas as pd
import pytest

from shared.volume_profile import (
    ProfileOptions,
    add_volume_profile,
    levels,
    price_minutes,
)


def test_value_area_contiguous_edges_and_ties() -> None:
    """Verify value area contiguous edges and ties."""
    assert levels({10: 5, 11: 10, 12: 4, 13: 1}, 10, 0.7) == (120, 115, 100)
    assert levels({10: 5, 12: 5}, 10, 1) == (130, 105, 100)
    assert levels({10: 1, 11: 10, 12: 5}, 10, 0.9) == (130, 115, 110)
    assert np.isnan(levels({}, 10, 0.7)).all()


def test_causal_daily_reset_previous_day_and_rolling() -> None:
    """Verify causal daily reset previous day and rolling."""
    times = pd.to_datetime([
        "2026-01-01T23:59Z",
        "2026-01-02T00:00Z",
        "2026-01-02T00:01Z",
        "2026-01-02T01:01Z",
    ])
    prices = pd.DataFrame({
        "time": times,
        "price": [100.0, 110.0, 200.0, 300.0],
        "size": [10.0, 5.0, 50.0, 100.0],
    })
    bars = pd.DataFrame({"time": times})
    daily = add_volume_profile(bars, prices)
    assert daily.poc.tolist() == [105, 105, 205, 305]
    expected_value = 105
    assert daily.previous_poc.iloc[2] == expected_value
    expected_value = 100
    assert daily.bar_trade_vwap.iloc[0] == expected_value
    truncated = add_volume_profile(bars.iloc[:2], prices.iloc[:2])
    pd.testing.assert_frame_equal(daily.iloc[:2], truncated)
    rolling = add_volume_profile(
        bars, prices, {"period": "Rolling", "rolling_hours": 1}
    )
    expected_value = 300
    assert rolling.val.iloc[-1] == expected_value
    anchored = add_volume_profile(
        bars, prices, {"period": "Anchored", "anchor": times[1]}
    )
    assert np.isnan(cast("float", anchored.poc.iloc[1]))
    expected_value = 205
    assert anchored.poc.iloc[2] == expected_value


@pytest.mark.parametrize(
    ("period", "boundary"),
    [
        ("Weekly UTC", "2026-01-05T00:00Z"),
        ("Monthly UTC", "2026-02-01T00:00Z"),
    ],
)
def test_calendar_profiles_reset_and_expose_previous_period(
    period: str, boundary: str
) -> None:
    """Verify weekly and monthly profiles are causal at boundaries."""
    boundary_time = pd.Timestamp(boundary)
    times = pd.to_datetime([
        boundary_time - pd.Timedelta(minutes=1),
        boundary_time,
        boundary_time + pd.Timedelta(minutes=1),
    ])
    prices = pd.DataFrame({
        "time": times,
        "price": [100.0, 110.0, 200.0],
        "size": [10.0, 5.0, 50.0],
    })
    result = add_volume_profile(
        pd.DataFrame({"time": times}), prices, {"period": period}
    )

    expected_previous_poc = 105
    assert result.poc.tolist() == [105, 105, 205]
    assert np.isnan(cast("float", result.previous_poc.iloc[1]))
    assert result.previous_poc.iloc[2] == expected_previous_poc


def test_empty_and_gap_sessions() -> None:
    """Verify empty and gap sessions."""
    prices = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-01T00:01Z"]),
        "price": [100.0],
        "size": [1.0],
    })
    bars = pd.DataFrame({
        "time": pd.to_datetime(
            ["2026-01-01T00:01Z", "2026-01-02T00:01Z", "2026-01-03T00:01Z"]
        )
    })
    result = add_volume_profile(bars, prices)
    assert np.isnan(cast("float", result.poc.iloc[1]))
    expected_value = 105
    assert result.previous_poc.iloc[1] == expected_value
    assert np.isnan(cast("float", result.previous_poc.iloc[2]))
    assert add_volume_profile(bars.iloc[:0], prices).empty


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bucket": 0.15},
        {"bucket": 0},
        {"value_area": 0},
        {"value_area": 101},
        {"period": "bad"},
        {"period": "Rolling", "rolling_hours": 0},
        {"period": "Anchored"},
    ],
)
def test_validation(kwargs: ProfileOptions) -> None:
    """Verify validation."""
    with pytest.raises(ValueError, match=r".+"):
        _ = add_volume_profile(pd.DataFrame(), pd.DataFrame(), kwargs)


@pytest.mark.parametrize(
    "stamp", [1767225600, 1767225600000, "2026-01-01T00:00:00Z"]
)
def test_trade_price_aggregation(stamp: int | str) -> None:
    """Verify trade price aggregation."""
    frame = pd.DataFrame({
        "timestamp": [stamp, stamp],
        "price": [100, 100],
        "size": [2, 2],
        "side": ["Buy", "Buy"],
        "trdMatchID": ["a", "a"],
    })
    result = price_minutes(frame)
    expected_value = 2
    assert result.iloc[0]["size"] == expected_value
    assert pd.to_datetime(result["time"], utc=True).iloc[0] == pd.Timestamp(
        "2026-01-01T00:01Z"
    )
    with pytest.raises(ValueError, match="require trade price"):
        _ = price_minutes(frame.drop(columns="price"))
    frame.loc[1, "price"] = 101
    with pytest.raises(ValueError, match="Conflicting"):
        _ = price_minutes(frame)
    frame["price"] = -1
    with pytest.raises(ValueError, match="positive"):
        _ = price_minutes(frame)
