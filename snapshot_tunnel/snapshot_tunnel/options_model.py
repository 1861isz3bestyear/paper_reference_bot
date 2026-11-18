# Copyright 2026 anchored-vwam-backtest contributors
"""Validated options snapshots and constant-volatility GEX calculations."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

import numpy as np

from snapshot_tunnel.option_levels import DEFAULT_LEVEL_RANGE, OptionLevelRange

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from snapshot_tunnel.order_book import BookSnapshot


@dataclass(frozen=True, kw_only=True)
class Option:
    """Linear option with contract OI and underlying units per contract."""

    exchange: str
    expiry: datetime
    strike: float
    side: Literal["call", "put"]
    open_interest: float
    multiplier: float
    iv: float


@dataclass(frozen=True, kw_only=True)
class Snapshot:
    """One asset at one observation time with a common USD spot reference."""

    timestamp: datetime
    asset: Literal["BTC", "ETH"]
    spot: float
    options: tuple[Option, ...]
    valuation_time: datetime | None = None
    order_book: BookSnapshot | None = None


@dataclass(frozen=True, kw_only=True)
class CandleClose:
    """One completed underlying candle used to reprice an option snapshot."""

    timestamp: datetime
    close: float


@dataclass(frozen=True, kw_only=True)
class CandleGex:
    """Signed and absolute GEX calculated at one candle close."""

    timestamp: datetime
    spot: float
    signed: float
    absolute: float


@dataclass(frozen=True, kw_only=True)
class Exposure:
    """USD delta-notional change per one percent underlying move."""

    strike: float
    call: float
    put: float

    @property
    def net(self) -> float:
        """Exposure under the calls-positive, puts-negative convention."""
        return math.fsum((self.call, self.put))

    @property
    def absolute(self) -> float:
        """Unsigned exposure."""
        return math.fsum((self.call, -self.put))


@dataclass(frozen=True, kw_only=True)
class RankedWalls:
    """Independent call and put strikes ordered by side exposure magnitude."""

    calls: tuple[Exposure, ...]
    puts: tuple[Exposure, ...]


def ranked_walls(
    levels: tuple[Exposure, ...],
    *,
    spot: float,
    level_range: OptionLevelRange = DEFAULT_LEVEL_RANGE,
) -> RankedWalls:
    """Select P1/P2 calls and N1/N2 puts regardless of aggregate GEX sign.

    Returns:
        Up to two strikes per side within the configured range of spot.
        Ties prefer the lower strike; distant strikes are never substituted.

    """
    levels = tuple(
        level
        for level in levels
        if level_range.includes(level.strike, spot=spot)
    )
    return RankedWalls(
        calls=tuple(
            sorted(
                (level for level in levels if level.call > 0),
                key=lambda level: (-level.call, level.strike),
            )[:2]
        ),
        puts=tuple(
            sorted(
                (level for level in levels if level.put < 0),
                key=lambda level: (level.put, level.strike),
            )[:2]
        ),
    )


@dataclass(frozen=True, kw_only=True)
class WallTunnel:
    """Price extrema of the two strongest positive and negative GEX ranks."""

    put: float
    call: float
    extrema: tuple[float, ...] = ()


def wall_tunnel(
    levels: tuple[Exposure, ...],
    *,
    spot: float,
    level_range: OptionLevelRange = DEFAULT_LEVEL_RANGE,
) -> WallTunnel | None:
    """Bound P1, P2, N1 and N2 by strike rather than exposure sign.

    Returns:
        Lowest and highest available ranked strikes, or no active exposure.
        Missing ranks are omitted; exposure ties choose the lower strike.

    """
    ranks = ranked_walls(levels, spot=spot, level_range=level_range)
    calls, puts = ranks.calls, ranks.puts
    strikes = [level.strike for level in (*calls, *puts)]
    if not strikes:
        return None
    extrema = {level.strike for level in calls} & {
        level.strike for level in puts
    }
    return WallTunnel(
        put=min(strikes), call=max(strikes), extrema=tuple(sorted(extrema))
    )


def positive_number(value: str, *, allow_zero: bool = False) -> float:
    """Parse a finite nonnegative or positive number.

    Returns:
        The validated value.

    Raises:
        ValueError: If the value is outside the permitted numeric domain.

    """
    result = float(value)
    if (
        not math.isfinite(result)
        or result < 0
        or (result == 0 and not allow_zero)
    ):
        msg = "Numbers must be finite and positive (OI may be zero)."
        raise ValueError(msg)
    return result


def timestamp(value: str) -> datetime:
    """Parse an ISO timestamp with an explicit timezone.

    Returns:
        An aware timestamp.

    Raises:
        ValueError: If timezone information is missing.

    """
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        msg = "Timestamps must include a timezone."
        raise ValueError(msg)
    return result


def _option(row: dict[str, str], observed: datetime) -> Option:
    """Validate one CSV contract.

    Returns:
        A typed option.

    Raises:
        ValueError: If side, exchange or expiry is invalid.

    """
    side = row["side"]
    expiry = timestamp(row["expiry"])
    if side not in {"call", "put"} or not row["exchange"].strip():
        msg = "Options require an exchange and side call or put."
        raise ValueError(msg)
    if expiry <= observed:
        msg = "Every option must expire after its snapshot timestamp."
        raise ValueError(msg)
    return Option(
        exchange=row["exchange"].strip().upper(),
        expiry=expiry,
        strike=positive_number(row["strike"]),
        side="call" if side == "call" else "put",
        open_interest=positive_number(row["open_interest"], allow_zero=True),
        multiplier=positive_number(row["multiplier"]),
        iv=positive_number(row["iv"]),
    )


def parse_snapshots(text: str) -> tuple[Snapshot, ...]:
    """Validate CSV rows before constructing domain snapshots.

    Returns:
        Snapshots sorted by observation time and asset.

    Raises:
        ValueError: If rows are empty, malformed, duplicated or inconsistent.

    """
    required = {
        "timestamp",
        "asset",
        "spot",
        "exchange",
        "expiry",
        "strike",
        "side",
        "open_interest",
        "multiplier",
        "iv",
    }
    reader = csv.DictReader(io.StringIO(text))
    if (
        reader.fieldnames is None
        or set(reader.fieldnames) != required
        or len(reader.fieldnames) != len(required)
    ):
        msg = "CSV columns must be: " + ", ".join(sorted(required))
        raise ValueError(msg)
    groups: dict[tuple[datetime, str], Snapshot] = {}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            msg = "Every CSV row must contain exactly the required fields."
            raise ValueError(msg)
        observed = timestamp(row["timestamp"])
        asset = row["asset"].strip().upper()
        if asset not in {"BTC", "ETH"}:
            msg = "Supported assets are BTC and ETH."
            raise ValueError(msg)
        key = (observed, asset)
        spot = positive_number(row["spot"])
        previous = groups.get(key)
        option = _option(row, observed)
        options = previous.options if previous else ()
        if previous is not None and previous.spot != spot:
            msg = "A snapshot must use a single spot price."
            raise ValueError(msg)
        identity = (option.exchange, option.expiry, option.strike, option.side)
        if any(
            (o.exchange, o.expiry, o.strike, o.side) == identity
            for o in options
        ):
            msg = "Duplicate contract within a snapshot."
            raise ValueError(msg)
        groups[key] = Snapshot(
            timestamp=observed,
            asset="BTC" if asset == "BTC" else "ETH",
            spot=spot,
            options=(*options, option),
        )
    if not groups:
        msg = "The options CSV is empty."
        raise ValueError(msg)
    return tuple(groups[key] for key in sorted(groups))


def parse_candle_closes(text: str) -> tuple[CandleClose, ...]:
    """Validate completed candle timestamps and closes from CSV text.

    Returns:
        Candle closes sorted chronologically.

    Raises:
        ValueError: If the CSV is malformed, duplicated, or not chronological.

    """
    reader = csv.DictReader(io.StringIO(text))
    required = {"timestamp", "close"}
    if (
        reader.fieldnames is None
        or set(reader.fieldnames) != required
        or len(reader.fieldnames) != len(required)
    ):
        msg = "Candle CSV columns must be: timestamp, close"
        raise ValueError(msg)
    candles: dict[datetime, CandleClose] = {}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            msg = "Every candle row must contain timestamp and close."
            raise ValueError(msg)
        observed = timestamp(row["timestamp"])
        if observed in candles:
            msg = "Candle timestamps must be unique."
            raise ValueError(msg)
        candles[observed] = CandleClose(
            timestamp=observed,
            close=positive_number(row["close"]),
        )
    if not candles:
        msg = "The candle CSV is empty."
        raise ValueError(msg)
    return tuple(candles[key] for key in sorted(candles))


def exposure(option: Option, snapshot: Snapshot, *, spot: float) -> float:
    """Calculate unsigned Black-Scholes GEX with zero rates and fixed IV.

    Returns:
        USD exposure per one percent price move.

    Raises:
        ValueError: If numeric inputs exceed the supported calculation range.

    """
    valued_at = snapshot.valuation_time or snapshot.timestamp
    if option.expiry <= valued_at:
        return 0.0
    years = (option.expiry - valued_at).total_seconds() / (365 * 86400)
    volatility = option.iv * math.sqrt(years)
    if volatility == 0 or not math.isfinite(volatility):
        msg = "Volatility is outside the supported numeric range."
        raise ValueError(msg)
    d1 = (
        math.log(spot) - math.log(option.strike)
    ) / volatility + volatility / 2
    amount = (
        math.exp(-d1 * d1 / 2)
        / volatility
        / math.sqrt(2 * math.pi)
        * option.open_interest
        * option.multiplier
        * spot
        * 0.01
    )
    if not math.isfinite(amount):
        msg = "Exposure is outside the supported numeric range."
        raise ValueError(msg)
    return amount


def strike_exposures(snapshot: Snapshot) -> tuple[Exposure, ...]:
    """Aggregate calls and puts by strike across the selected contracts.

    Returns:
        Sorted strike exposures.

    """
    values: dict[float, Exposure] = {}
    for option in snapshot.options:
        previous = values.get(
            option.strike, Exposure(strike=option.strike, call=0, put=0)
        )
        amount = exposure(option, snapshot, spot=snapshot.spot)
        values[option.strike] = Exposure(
            strike=option.strike,
            call=math.fsum(
                (previous.call, amount if option.side == "call" else 0)
            ),
            put=math.fsum(
                (previous.put, -amount if option.side == "put" else 0)
            ),
        )
    return tuple(values[key] for key in sorted(values))


def net_exposure(snapshot: Snapshot, spot: float) -> float:
    """Reprice signed exposure at a hypothetical spot.

    Returns:
        USD exposure per one percent price move.

    """
    return math.fsum(
        exposure(option, snapshot, spot=spot)
        * (1 if option.side == "call" else -1)
        for option in snapshot.options
    )


def grid_exposures(
    snapshot: Snapshot, prices: tuple[float, ...]
) -> tuple[float, ...]:
    """Vectorize grid sampling without changing the option exposure formula.

    Returns:
        Signed dollar exposures in input price order.

    Raises:
        ValueError: If volatility or evaluated exposure is not finite.

    """
    valued_at = snapshot.valuation_time or snapshot.timestamp
    options = tuple(o for o in snapshot.options if o.expiry > valued_at)
    if not options or not prices:
        return (0.0,) * len(prices)
    volatility = np.array([
        o.iv * math.sqrt((o.expiry - valued_at).total_seconds() / (365 * 86400))
        for o in options
    ])
    if not np.all(np.isfinite(volatility) & (volatility > 0)):
        msg = "Volatility is outside the supported numeric range."
        raise ValueError(msg)
    spots = np.array(prices)[:, None]
    strikes = np.array([o.strike for o in options])
    weights = np.array([
        o.open_interest * o.multiplier * (1 if o.side == "call" else -1)
        for o in options
    ])
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        d1 = np.log(spots / strikes) / volatility + volatility / 2
        amounts = (
            np.exp(-d1 * d1 / 2)
            / volatility
            / math.sqrt(2 * math.pi)
            * weights
            * spots
            * 0.01
        )
        values: NDArray[np.float64] = np.sum(amounts, axis=1)
    if not np.all(np.isfinite(values)):
        msg = "Exposure is outside the supported numeric range."
        raise ValueError(msg)
    return tuple(float(value) for value in values.flat)


def value_snapshot(snapshot: Snapshot, candle: CandleClose) -> Snapshot:
    """Advance valuation without changing the original observation time.

    Returns:
        A repriced chain containing only unexpired contracts.

    Raises:
        ValueError: If valuation precedes observation or the price is invalid.

    """
    if candle.timestamp < snapshot.timestamp:
        msg = "Cannot value an options snapshot before its observation."
        raise ValueError(msg)
    return replace(
        snapshot,
        spot=positive_number(str(candle.close)),
        valuation_time=candle.timestamp,
        options=tuple(
            option
            for option in snapshot.options
            if option.expiry > candle.timestamp
        ),
    )


def update_gex_once_per_candle(
    snapshot: Snapshot, candles: tuple[CandleClose, ...]
) -> tuple[CandleGex, ...]:
    """Reprice one option snapshot once at every completed candle close.

    Returns:
        One GEX result for each supplied candle, in chronological order.

    """
    return tuple(_gex_at_candle_close(snapshot, candle) for candle in candles)


def _gex_at_candle_close(snapshot: Snapshot, candle: CandleClose) -> CandleGex:
    """Calculate both GEX views from one exposure pass for a candle.

    Returns:
        The signed and absolute GEX at the candle close.

    """
    snapshot = value_snapshot(snapshot, candle)
    amounts = tuple(
        (
            exposure(option, snapshot, spot=candle.close),
            1 if option.side == "call" else -1,
        )
        for option in snapshot.options
    )
    return CandleGex(
        timestamp=candle.timestamp,
        spot=candle.close,
        signed=math.fsum(amount * sign for amount, sign in amounts),
        absolute=math.fsum(amount for amount, _ in amounts),
    )


def gamma_flips(
    snapshot: Snapshot, prices: tuple[float, ...]
) -> tuple[float, ...]:
    """Find sign-changing roots in the supplied increasing price grid.

    Returns:
        Refined roots; a flat zero profile has no identified flip.

    """
    roots: list[float] = []
    samples = zip(prices, grid_exposures(snapshot, prices), strict=True)
    nonzero = [(price, value) for price, value in samples if value != 0]
    for start, end in pairwise(nonzero):
        left, low_value = start
        right, high_value = end
        if (low_value > 0) == (high_value > 0):
            continue
        for _ in range(40):
            middle = (left + right) / 2
            value = net_exposure(snapshot, middle)
            if value == 0:
                left = right = middle
                break
            if (value > 0) == (low_value > 0):
                left, low_value = middle, value
            else:
                right = middle
        roots.append((left + right) / 2)
    return tuple(roots)


def max_pain(snapshot: Snapshot) -> float | None:
    """Minimize linear intrinsic payout over observed strikes for one expiry.

    Returns:
        The lowest minimizing strike, or None for mixed expiry or zero OI.

    """
    if len({o.expiry for o in snapshot.options}) != 1:
        return None
    if not any(o.open_interest > 0 for o in snapshot.options):
        return None
    payouts = {
        strike: sum(
            max((strike - o.strike) * (1 if o.side == "call" else -1), 0)
            * o.open_interest
            * o.multiplier
            for o in snapshot.options
        )
        for strike in sorted({o.strike for o in snapshot.options})
    }
    return min(payouts, key=lambda strike: payouts[strike])
