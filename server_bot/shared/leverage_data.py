# Copyright 2026 anchored-vwam-backtest contributors
"""Public Bybit inputs that never replace missing risk data with zero."""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn, cast

import pandas as pd
import requests

from shared.margin_types import Contract, RiskTier
from shared.validation import (
    numeric_string,
    object_list,
    object_mapping,
    string,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

BASE_URL = "https://api.bybit.com/v5/market/"
MARK_CANDLE_FIELD_COUNT = 5
INTERVAL_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


def request_rows(endpoint: str, **params: str | int) -> dict[str, object]:
    """Handle request rows.

    Returns:
        The resulting value.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    response = requests.get(
        BASE_URL + endpoint, params={"category": "linear", **params}, timeout=30
    )
    response.raise_for_status()
    payload = object_mapping(cast("object", response.json()))
    if payload.get("retCode") != 0:
        msg = f"Bybit {endpoint}: {payload.get('retMsg', 'request failed')}"
        raise ValueError(msg)
    return object_mapping(payload["result"])


def _raise_invalid_number(field: str, value: object, context: str) -> NoReturn:
    msg = f"Bybit {context}: missing or invalid {field} ({value!r})."
    raise ValueError(msg)


def _number(row: Mapping[str, object], field: str, context: str) -> float:
    """Identify invalid exchange fields instead of leaking float conversion.

    errors.

    Returns:
        The computed result.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    value = row.get(field)
    if isinstance(value, bool):
        _raise_invalid_number(field, value, context)
    try:
        result = numeric_string(value)
    except TypeError, ValueError:
        msg = f"Bybit {context}: missing or invalid {field} ({value!r})."
        raise ValueError(msg) from None
    if not math.isfinite(result):
        msg = f"Bybit {context}: missing or invalid {field} ({value!r})."
        raise ValueError(msg)
    return result


def parse_risk_tiers(
    rows: Sequence[Mapping[str, object]], symbol: str
) -> list[RiskTier]:
    """Derive omitted maintenance deductions from the contiguous tier schedule.

    Returns:
        The computed result.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    context = f"risk-limit for {symbol}"
    ordered = sorted(
        rows, key=lambda row: _number(row, "riskLimitValue", context)
    )
    tiers: list[RiskTier] = []
    previous_limit = previous_rate = previous_deduction = 0.0
    for row in ordered:
        limit = _number(row, "riskLimitValue", context)
        rate = _number(row, "maintenanceMargin", context)
        if limit <= previous_limit or rate < previous_rate:
            msg = (
                f"Bybit {context}: invalid risk-tier order or maintenance"
                " rates."
            )
            raise ValueError(msg)
        raw_deduction = row.get("mmDeduction")
        if raw_deduction is None or (
            isinstance(raw_deduction, str) and not raw_deduction.strip()
        ):
            # Bybit can omit this field or return an empty string. Continuity of
            # maintenance margin gives D[n] = D[n-1] +
            # limit[n-1]*(MMR[n]-MMR[n-1]).
            deduction = previous_deduction + previous_limit * (
                rate - previous_rate
            )
        else:
            deduction = _number(row, "mmDeduction", context)
        tiers.append(
            RiskTier(
                limit=limit,
                mmr=rate,
                deduction=deduction,
                max_leverage=_number(row, "maxLeverage", context),
            )
        )
        previous_limit, previous_rate, previous_deduction = (
            limit,
            rate,
            deduction,
        )
    return tiers


def fetch_contract(symbol: str) -> Contract:
    """Handle fetch contract.

    Returns:
        The resulting value.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    instruments = object_list(
        request_rows("instruments-info", symbol=symbol)["list"]
    )
    if not instruments:
        msg = f"No Bybit instrument found for {symbol}."
        raise ValueError(msg)
    info = object_mapping(instruments[0])
    if (
        info.get("contractType") != "LinearPerpetual"
        or info.get("settleCoin") != "USDT"
    ):
        msg = "Leverage requires a USDT-settled perpetual contract."
        raise ValueError(msg)
    tiers: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        page = request_rows(
            "risk-limit",
            symbol=symbol,
            **({"cursor": cursor} if cursor else {}),
        )
        tiers.extend(object_mapping(row) for row in object_list(page["list"]))
        next_cursor = page.get("nextPageCursor")
        if not next_cursor:
            break
        if next_cursor == cursor:
            msg = "Bybit risk-limit pagination did not advance."
            raise ValueError(msg)
        cursor = string(next_cursor)
    lots = object_mapping(info["lotSizeFilter"])
    context = f"instruments-info for {symbol}"
    funding_interval = _number(info, "fundingInterval", context)
    if not funding_interval.is_integer():
        msg = f"Bybit {context}: fundingInterval must be whole minutes."
        raise ValueError(msg)
    contract = Contract(
        symbol=symbol,
        fetched_at=datetime.now(UTC).isoformat(),
        max_leverage=_number(
            object_mapping(info["leverageFilter"]), "maxLeverage", context
        ),
        leverage_step=_number(
            object_mapping(info["leverageFilter"]), "leverageStep", context
        ),
        quantity_step=_number(lots, "qtyStep", context),
        min_quantity=_number(lots, "minOrderQty", context),
        min_notional=_number(lots, "minNotionalValue", context),
        max_quantity=_number(lots, "maxMktOrderQty", context),
        price_tick=_number(
            object_mapping(info["priceFilter"]), "tickSize", context
        ),
        funding_interval_minutes=int(funding_interval),
        tiers=parse_risk_tiers(tiers, symbol),
    )
    validate_contract(contract)
    return contract


def validate_contract(value: object) -> None:
    """Handle validate contract.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    contract = object_mapping(value)
    for key in (
        "max_leverage",
        "leverage_step",
        "quantity_step",
        "min_quantity",
        "max_quantity",
        "price_tick",
        "funding_interval_minutes",
    ):
        number = numeric_string(contract.get(key))
        if not math.isfinite(number) or number <= 0:
            msg = f"Invalid contract {key}."
            raise ValueError(msg)
    min_notional = numeric_string(contract.get("min_notional"))
    if not math.isfinite(min_notional) or min_notional < 0:
        msg = "Invalid contract minimum notional."
        raise ValueError(msg)
    tiers = object_list(contract.get("tiers"))
    if not tiers:
        msg = "Maintenance margin tiers are required."
        raise ValueError(msg)
    previous = 0.0
    for raw_tier in tiers:
        tier = object_mapping(raw_tier)
        limit = numeric_string(tier.get("limit"))
        mmr = numeric_string(tier.get("mmr"))
        deduction = numeric_string(tier.get("deduction"))
        max_leverage = numeric_string(tier.get("max_leverage"))
        if (
            not all(
                math.isfinite(item)
                for item in (limit, mmr, deduction, max_leverage)
            )
            or limit <= previous
            or not 0 < mmr < 1
            or deduction < 0
            or max_leverage < 1
        ):
            msg = "Invalid maintenance margin tier."
            raise ValueError(msg)
        previous = limit


def fetch_mark_prices(
    symbol: str, interval: str, start_ms: int, *, end_ms: int
) -> pd.DataFrame:
    """Handle fetch mark prices.

    Returns:
        The resulting value.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    rows: list[list[float]] = []
    cursor = end_ms
    while cursor >= start_ms:
        page = request_rows(
            "mark-price-kline",
            symbol=symbol,
            interval=(
                "D" if interval == "1d" else str(INTERVAL_MINUTES[interval])
            ),
            start=start_ms,
            end=cursor,
            limit=1000,
        )["list"]
        if not page:
            break
        validated = [
            [numeric_string(item) for item in object_list(row)]
            for row in object_list(page)
        ]
        if any(len(row) != MARK_CANDLE_FIELD_COUNT for row in validated):
            msg = "Invalid Bybit mark-price candle."
            raise ValueError(msg)
        rows.extend(validated)
        next_cursor = min(int(row[0]) for row in validated) - 1
        if next_cursor >= cursor:
            msg = "Bybit mark-price pagination did not advance."
            raise ValueError(msg)
        cursor = next_cursor
    frame = pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "mark_open",
            "mark_high",
            "mark_low",
            "mark_close",
        ],
    )
    frame["time"] = pd.to_datetime(
        pd.to_numeric(frame.pop("timestamp")), unit="ms", utc=True
    )
    for key in ("mark_open", "mark_high", "mark_low", "mark_close"):
        frame[key] = pd.to_numeric(frame[key])
    return frame.drop_duplicates("time").sort_values("time")


def fetch_funding(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    interval_minutes: int,
) -> list[tuple[pd.Timestamp, float]]:
    # Include boundary settlements on either side to detect truncated history.
    """Handle fetch funding.

    Returns:
        The resulting value.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    padding = max(interval_minutes, 480) * 60_000
    rows: list[dict[str, object]] = []
    cursor = end_ms
    while cursor >= start_ms - padding:
        page = request_rows(
            "funding/history",
            symbol=symbol,
            startTime=start_ms - padding,
            endTime=cursor,
            limit=200,
        )["list"]
        if not page:
            break
        validated = [object_mapping(row) for row in object_list(page)]
        rows.extend(validated)
        next_cursor = (
            min(
                int(numeric_string(row["fundingRateTimestamp"]))
                for row in validated
            )
            - 1
        )
        if next_cursor >= cursor:
            msg = "Bybit funding pagination did not advance."
            raise ValueError(msg)
        cursor = next_cursor
    rates = {
        int(numeric_string(row["fundingRateTimestamp"])): _number(
            row, "fundingRate", f"funding/history for {symbol}"
        )
        for row in rows
    }
    if not rates or not all(math.isfinite(rate) for rate in rates.values()):
        msg = "Historical funding is missing or invalid."
        raise ValueError(msg)
    # Historical interval changes cannot be inferred from the current
    # instrument.
    # Refuse unexpectedly large gaps instead of treating unknown settlements as
    # free.
    times = sorted(rates)
    max_gap = interval_minutes * 60_000
    if (
        times[0] > start_ms
        or end_ms - times[-1] > max_gap
        or any(b - a > max_gap for a, b in itertools.pairwise(times))
    ):
        msg = (
            "Incomplete funding coverage; choose a range with available"
            " settlements."
        )
        raise ValueError(msg)
    return [
        (pd.Timestamp(t, unit="ms", tz="UTC"), rates[t])
        for t in times
        if start_ms <= t <= end_ms
    ]


def load_leverage_data(
    candles: pd.DataFrame,
    symbol: str,
    interval: str,
    *,
    contract: Contract | None = None,
) -> tuple[pd.DataFrame, Contract]:
    """Join mark candles and exact funding boundaries to ordinary trade candles.

    Returns:
        The computed result.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    contract = contract or fetch_contract(symbol)
    start = int(
        pd.to_datetime(candles["time"], utc=True).iloc[0].timestamp() * 1000
    )
    end = int(
        pd.to_datetime(candles["time"], utc=True).iloc[-1].timestamp() * 1000
    )
    duration = INTERVAL_MINUTES[interval] * 60_000
    marks = fetch_mark_prices(symbol, interval, start, end_ms=end)
    result = candles.merge(marks, on="time", how="left", validate="one_to_one")
    keys = ["mark_open", "mark_high", "mark_low", "mark_close"]
    prices = result[keys]
    if (
        prices.isna().any().any()
        or not prices.map(math.isfinite).all().all()
        or (prices <= 0).any().any()
        or (result["mark_low"] > prices.min(axis=1)).any()
        or (result["mark_high"] < prices.max(axis=1)).any()
    ):
        msg = "Complete, valid mark-price candles are required for leverage."
        raise ValueError(msg)
    if (
        len(result) > 1
        and not (
            result["time"].diff().dropna()
            == pd.Timedelta(milliseconds=duration)
        ).all()
    ):
        msg = "Leverage requires continuous candles; market history has gaps."
        raise ValueError(msg)
    rates = fetch_funding(
        symbol,
        start,
        end + duration - 1,
        interval_minutes=contract["funding_interval_minutes"],
    )
    result["funding_rate"] = 0.0
    candle_times = set(pd.to_datetime(result["time"], utc=True))
    for timestamp, rate in rates:
        if timestamp not in candle_times:
            msg = (
                "Funding falls inside a candle. Select a smaller interval (1h"
                " or less)."
            )
            raise ValueError(msg)
        result.loc[result["time"] == timestamp, "funding_rate"] = rate
    return result, contract
