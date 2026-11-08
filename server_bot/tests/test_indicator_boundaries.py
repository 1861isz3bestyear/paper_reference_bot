# Copyright 2026 anchored-vwam-backtest contributors
"""Check VWAP reset boundaries, display times and external row validation."""

from __future__ import annotations

from datetime import UTC, date, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from shared import indicators, time_utils, validation
from shared.order_book import BookLevel, BookSnapshot
from shared.tunnel_oscillators import (
    BorrowRate,
    borrow_rate_rsi,
    limit_order_oscillator_rate,
)


def candles() -> pd.DataFrame:
    """Return exact constant-volume candles across recurring reset dates.

    Returns:
        The configured test value.

    """
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=100, freq="D", tz="UTC"),
        "open": 100.0,
        "high": 100.0,
        "low": 100.0,
        "close": 100.0,
        "volume": 1.0,
    })


def test_fixed_vwap_and_launch_boundaries() -> None:
    """Leave pre-anchor prices unavailable and reset at the launch interval."""
    data = candles()
    result = indicators.add_anchored_vwap(
        data, pd.Timestamp("2026-01-02", tz="UTC")
    )
    if not (result.anchored_vwap.isna().iloc[0]):
        pytest.fail("Expected result.anchored_vwap.isna().iloc[0]")
    if not (result.anchored_vwap.iloc[1:].eq(100).all()):
        pytest.fail("Expected result.anchored_vwap.iloc[1:].eq(100).all()")
    if not (result.anchored_std.iloc[1:].eq(0).all()):
        pytest.fail("Expected result.anchored_std.iloc[1:].eq(0).all()")
    launch = indicators.add_launch_weekly_anchored_vwap(
        data, pd.Timestamp("2026-01-02", tz="UTC"), 2
    )
    if not (launch.anchored_vwap.iloc[1:].eq(100).all()):
        pytest.fail("Expected launch.anchored_vwap.iloc[1:].eq(100).all()")
    if not (
        indicators.add_launch_weekly_anchored_vwap(
            data, pd.Timestamp("2027-01-01", tz="UTC"), 1
        )
        .anchored_vwap.isna()
        .all()
    ):
        pytest.fail(
            "Expected indicators.add_launch_weekly_anchored_vwap(data,"
            " pd.Timestamp('2027-01-01', tz='UTC'),"
            " 1).anchored_vwap.isna().all()"
        )
    with pytest.raises(ValueError, match=r"reset weeks"):
        _ = indicators.add_launch_weekly_anchored_vwap(
            data, pd.Timestamp("2026-01-01", tz="UTC"), 0
        )


@pytest.mark.parametrize("frequency", ["Daily", "Weekly", "Monthly"])
def test_recurring_vwap(frequency: str) -> None:
    """Compute full periods and retain missing values for partial history."""
    data = candles()
    options = indicators.RecurringAnchor(selected_timezone=UTC)
    result = indicators.add_recurring_anchored_vwap(
        data, frequency, time(), options=options
    )
    valid = result.anchored_vwap.dropna()
    if valid.empty:
        pytest.fail("Expected not valid.empty")
    if not (valid.eq(100).all()):
        pytest.fail("Expected valid.eq(100).all()")
    if not (result.anchored_std.dropna().eq(0).all()):
        pytest.fail("Expected result.anchored_std.dropna().eq(0).all()")
    partial = data.iloc[:1]
    absent = indicators.add_recurring_anchored_vwap(
        partial, frequency, time(12), options=options
    )
    if not (absent.anchored_vwap.isna().all()):
        pytest.fail("Expected absent.anchored_vwap.isna().all()")


@pytest.mark.parametrize(
    ("frequency", "options"),
    [
        (
            "Weekly",
            indicators.RecurringAnchor(selected_timezone=UTC, interval_count=0),
        ),
        ("bad", indicators.RecurringAnchor(selected_timezone=UTC)),
        (
            "Weekly",
            indicators.RecurringAnchor(selected_timezone=UTC, anchor_weekday=7),
        ),
        (
            "Monthly",
            indicators.RecurringAnchor(
                selected_timezone=UTC, anchor_month_day=29
            ),
        ),
    ],
)
def test_invalid_recurring_schedule(
    frequency: str, options: indicators.RecurringAnchor
) -> None:
    """Reject invalid recurring reset parameters before constructing periods."""
    with pytest.raises(ValueError, match=r"anchor|frequency"):
        _ = indicators.add_recurring_anchored_vwap(
            candles(), frequency, time(), options=options
        )


def test_display_timezone_conversion() -> None:
    """Convert New York wall clocks and chart timestamps consistently."""
    day = date(2026, 1, 1)
    utc = time_utils.utc_datetime(day, at=time(12))
    expected_value = 12
    if utc.hour != expected_value:
        pytest.fail("Expected utc.hour == 12")
    expected_value = 3
    if time_utils.utc_datetime(day, 3).hour != expected_value:
        pytest.fail("Expected time_utils.utc_datetime(day, 3).hour == 3")
    zone, label = time_utils.chart_timezone("Eastern Time (New York)")
    if label != "Eastern Time (New York)":
        pytest.fail("Expected label == 'Eastern Time (New York)'")
    expected_value = 17
    if (
        time_utils.timeline_datetime(day, zone, at=time(12)).hour
        != expected_value
    ):
        pytest.fail(
            "Expected time_utils.timeline_datetime(day, zone, at=time(12)).hour"
            " == 17"
        )
    expected_value = 5
    if time_utils.timeline_datetime(day, zone, hour=0).hour != expected_value:
        pytest.fail(
            "Expected time_utils.timeline_datetime(day, zone, hour=0).hour == 5"
        )
    if time_utils.chart_timezone("UTC") != (UTC, "UTC"):
        pytest.fail("Expected time_utils.chart_timezone('UTC') == (UTC, 'UTC')")
    if not (
        time_utils.chart_timezone("Local time")[1].startswith("Local time (")
    ):
        pytest.fail(
            "Expected time_utils.chart_timezone('Local"
            " time')[1].startswith('Local time (')"
        )
    values = pd.Series([pd.Timestamp(utc)])
    if time_utils.chart_times(values, zone).iloc[0] != pd.Timestamp(
        "2026-01-01 07:00"
    ):
        pytest.fail(
            "Expected time_utils.chart_times(values, zone).iloc[0] =="
            " pd.Timestamp('2026-01-01 07:00')"
        )
    if time_utils.chart_timestamp(
        values.iloc[0], ZoneInfo("America/New_York")
    ) != pd.Timestamp("2026-01-01 07:00"):
        pytest.fail(
            "Expected time_utils.chart_timestamp(values.iloc[0],"
            " ZoneInfo('America/New_York')) == pd.Timestamp('2026-01-01 07:00')"
        )


def test_external_row_validation() -> None:
    """Validate heterogeneous JSON and SQL rows without unsafe coercion."""
    if validation.numeric_rows([["1", 2]], 2) != [[1.0, 2.0]]:
        pytest.fail(
            "Expected validation.numeric_rows([['1', 2]], 2) == [[1.0, 2.0]]"
        )
    if validation.database_rows([(1, "a")]) != [(1, "a")]:
        pytest.fail(
            "Expected validation.database_rows([(1, 'a')]) == [(1, 'a')]"
        )
    with pytest.raises(TypeError, match=r"string object keys"):
        _ = validation.object_mapping({1: "bad"})
    with pytest.raises(TypeError, match=r"array"):
        _ = validation.object_list({})
    with pytest.raises(ValueError, match=r"2 values"):
        _ = validation.numeric_rows([[1]], 2)
    with pytest.raises(TypeError, match=r"tuple"):
        _ = validation.database_rows([[1]])


@pytest.mark.parametrize(
    ("rates", "expected"),
    [([1.0] * 20, 50), (list(range(20)), 100), (list(range(20, 0, -1)), 0)],
)
def test_borrow_rate_warmup(rates: list[float], expected: float) -> None:
    """Retain RSI warmup and handle flat, rising and falling interest rates."""
    start = time_utils.utc_datetime(date(2026, 1, 1))
    observations = tuple(
        BorrowRate(timestamp=start + timedelta(hours=index), annual_rate=rate)
        for index, rate in enumerate(rates)
    )
    result = borrow_rate_rsi(observations)
    if result[:14] != (None,) * 14:
        pytest.fail("Expected result[:14] == (None,) * 14")
    if result[14:] != (expected,) * 6:
        pytest.fail("Expected result[14:] == (expected,) * 6")
    if borrow_rate_rsi(()) != ():
        pytest.fail("Expected borrow_rate_rsi(()) == ()")


def test_book_notional_oscillator() -> None:
    """Use quote notional instead of raw quantity and reject empty liquidity."""
    now = time_utils.utc_datetime(date(2026, 1, 1))
    book = BookSnapshot(
        observed=now,
        available=now,
        bids=(BookLevel(price=99, quantity=2),),
        asks=(BookLevel(price=101, quantity=1),),
    )
    if limit_order_oscillator_rate(book) != pytest.approx(19800 / 299):
        pytest.fail(
            "Expected limit_order_oscillator_rate(book) == pytest.approx(19800"
            " / 299)"
        )
    empty = BookSnapshot(observed=now, available=now, bids=(), asks=())
    with pytest.raises(ValueError, match=r"notional"):
        _ = limit_order_oscillator_rate(empty)
