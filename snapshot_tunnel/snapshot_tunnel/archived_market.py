# Copyright 2026 anchored-vwam-backtest contributors
"""Calculate GEX and oscillators exclusively from committed archive inputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from market_storage.archive import Archive
from market_storage.inputs import CalculationInputs, GexInputs, load_inputs
from market_storage.models import Source

from snapshot_tunnel.bitget import Growth, growth_momentum, parse_growth
from snapshot_tunnel.oi_divergence import (
    DivergenceSeries,
    DivergenceSettings,
    InterestSample,
    MarketSample,
    divergence_series,
)
from snapshot_tunnel.options_data import normalize_chain
from snapshot_tunnel.options_depth import parse_depth
from snapshot_tunnel.options_model import (
    Snapshot,
    positive_number,
    strike_exposures,
)
from snapshot_tunnel.tunnel_oscillators import limit_order_oscillator_rate
from snapshot_tunnel.validation import (
    json_object,
    numeric_string,
    object_list,
    object_mapping,
    string,
)

if TYPE_CHECKING:
    from pathlib import Path

    from market_storage.models import Observation, Window

    from snapshot_tunnel.options_model import Exposure


@dataclass(frozen=True, kw_only=True)
class ArchivedGex:
    """A persisted complete chain and its calculated strike exposures."""

    snapshot: Snapshot
    exposures: tuple[Exposure, ...]


@dataclass(frozen=True, kw_only=True)
class ArchivedRate:
    """An oscillator value with separate source and availability timestamps."""

    observed: datetime
    available: datetime
    value: float | None


@dataclass(frozen=True, kw_only=True)
class ArchivedOscillators:
    """Independent oscillator histories with recorded receipt times."""

    order_book: tuple[ArchivedRate, ...]
    margin: tuple[ArchivedRate, ...]
    oi: DivergenceSeries


@dataclass(frozen=True, kw_only=True)
class ArchivedCalculations(ArchivedOscillators):
    """Independent indicator streams without a live exchange fallback."""

    gex: tuple[ArchivedGex, ...]


def _snapshot(packet: GexInputs) -> Snapshot:
    index = object_mapping(json_object(packet.spot.payload)["result"])
    return Snapshot(
        asset=packet.spot.asset,
        timestamp=packet.available,
        spot=positive_number(str(index["index_price"])),
        options=normalize_chain(
            json_object(packet.instruments.payload)["result"],
            json_object(packet.options.payload)["result"],
            asset=packet.spot.asset,
        ),
    )


def _book_rates(inputs: CalculationInputs) -> tuple[ArchivedRate, ...]:
    rates: list[ArchivedRate] = []
    for row in inputs.stream(Source.DEPTH):
        book = parse_depth(row.payload, asset=row.asset, received=row.available)
        rates.append(
            ArchivedRate(
                observed=book.observed,
                available=book.available,
                value=limit_order_oscillator_rate(book),
            )
        )
    return tuple(rates)


def _margin_rates(inputs: CalculationInputs) -> tuple[ArchivedRate, ...]:
    records: dict[datetime, tuple[Growth, datetime]] = {}
    for row in inputs.stream(Source.GROWTH_HOURLY):
        for point in parse_growth(row.payload):
            if point.timestamp > row.available:
                msg = "Margin source timestamp exceeds its receipt time."
                raise ValueError(msg)
            records[point.timestamp] = (point, row.available)
    ordered = tuple(records[stamp] for stamp in sorted(records))
    values = growth_momentum(tuple(row[0] for row in ordered), period="24h")
    return tuple(
        ArchivedRate(
            observed=point.timestamp,
            available=max(row[1] for row in ordered[: index + 1]),
            value=values[index],
        )
        for index, (point, _) in enumerate(ordered)
    )


def _interest(row: Observation) -> tuple[InterestSample, ...]:
    result = object_mapping(json_object(row.payload)["result"])
    if string(result["symbol"]) != f"{row.asset}USDT":
        msg = "Unexpected archived OI symbol."
        raise ValueError(msg)
    samples: dict[datetime, InterestSample] = {}
    interval_ms = 300_000
    for item in object_list(result["list"]):
        value = object_mapping(item)
        stamp = string(value["timestamp"])
        if not stamp.isdecimal() or int(stamp) % interval_ms:
            msg = "OI timestamps must use five-minute boundaries."
            raise ValueError(msg)
        observed = datetime.fromtimestamp(int(stamp) / 1000, UTC)
        if observed > row.available:
            msg = "OI source timestamp exceeds its receipt time."
            raise ValueError(msg)
        if observed in samples:
            msg = "Duplicate archived OI timestamp."
            raise ValueError(msg)
        samples[observed] = InterestSample(
            timestamp=observed, value=numeric_string(value["openInterest"])
        )
    return tuple(samples[stamp] for stamp in sorted(samples))


def _matched_interest(
    interest: InterestSample, *, row: Observation, ticker: Observation
) -> MarketSample:
    """Pair OI with its saved price and preserve receipt availability.

    Returns:
        A market sample available only after both responses arrived.

    Raises:
        ValueError: If the saved ticker has an invalid count or symbol.

    """
    result = object_mapping(json_object(ticker.payload)["result"])
    quotes = object_list(result["list"])
    if len(quotes) != 1:
        msg = "Expected one archived ticker."
        raise ValueError(msg)
    quote = object_mapping(quotes[0])
    if string(quote["symbol"]) != f"{row.asset}USDT":
        msg = "Unexpected archived ticker symbol."
        raise ValueError(msg)
    return MarketSample(
        interest=interest,
        price=numeric_string(quote["lastPrice"]),
        received=max(row.available, ticker.available),
    )


def _market_samples(inputs: CalculationInputs) -> tuple[MarketSample, ...]:
    interval = 300
    tickers = {
        int(row.requested.timestamp()) // interval: row
        for row in inputs.stream(Source.TICKER)
    }
    samples: dict[datetime, MarketSample] = {}
    for row in inputs.stream(Source.OI):
        for interest in _interest(row):
            ticker = tickers.get(
                int(interest.timestamp.timestamp()) // interval
            )
            if ticker is None:
                continue
            sample = _matched_interest(interest, row=row, ticker=ticker)
            previous = samples.get(interest.timestamp)
            if previous is None or (sample.received or interest.timestamp) < (
                previous.received or interest.timestamp
            ):
                samples[interest.timestamp] = sample
    causal: list[MarketSample] = []
    known = datetime.min.replace(tzinfo=UTC)
    for stamp in sorted(samples):
        sample = samples[stamp]
        known = max(known, sample.received or sample.interest.timestamp)
        causal.append(replace(sample, received=known))
    return tuple(causal)


def calculate_oscillators(inputs: CalculationInputs) -> ArchivedOscillators:
    """Validate stored non-options responses and calculate causal indicators.

    Returns:
        Book imbalance, hourly growth momentum and five-minute OI divergence.

    """
    return ArchivedOscillators(
        order_book=_book_rates(inputs),
        margin=_margin_rates(inputs),
        oi=divergence_series(
            _market_samples(inputs),
            settings=DivergenceSettings(interval=timedelta(minutes=5)),
        ),
    )


def calculate_archive(path: Path, window: Window) -> ArchivedCalculations:
    """Load inputs first, validate exchange schemas, then calculate indicators.

    Returns:
        GEX, book imbalance, hourly margin momentum, and five-minute OI signals.
        OI uses recorded ticker prices without storing or synthesizing OHLCV.

    """
    inputs = load_inputs(Archive(path=path), window)
    snapshots = tuple(_snapshot(packet) for packet in inputs.gex)
    oscillators = calculate_oscillators(inputs)
    return ArchivedCalculations(
        gex=tuple(
            ArchivedGex(snapshot=snapshot, exposures=strike_exposures(snapshot))
            for snapshot in snapshots
        ),
        order_book=oscillators.order_book,
        margin=oscillators.margin,
        oi=oscillators.oi,
    )
