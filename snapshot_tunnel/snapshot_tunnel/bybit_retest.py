# Copyright 2026 anchored-vwam-backtest contributors
"""Load public Bybit spot inputs for historical and running Retest decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from snapshot_tunnel.retest import HOUR, RetestHistory, SpotBar, retest_votes
from snapshot_tunnel.validation import numeric_rows, object_mapping

if TYPE_CHECKING:
    import requests

    from snapshot_tunnel.tunnel import RetestSettings
    from snapshot_tunnel.tunnel_flow import OscillatorObservation

_KLINES = "https://api.bybit.com/v5/market/kline"
_PAGE_LIMIT = 1000
type Interval = Literal["1", "5", "60"]


def _spot_bar(row: list[float], *, interval_ms: int) -> SpotBar:
    stamp, opening, high, low, close, *_ = row
    if stamp % interval_ms or not 0 < low <= min(opening, close):
        msg = "Bybit returned an invalid spot kline."
        raise ValueError(msg)
    if high < max(opening, close):
        msg = "Bybit returned an invalid spot high."
        raise ValueError(msg)
    return SpotBar(
        time=datetime.fromtimestamp(stamp / 1000, UTC),
        open=opening,
        high=high,
        low=low,
        close=close,
    )


def _spot_bars(
    session: requests.Session,
    start: datetime,
    end: datetime,
    *,
    interval: Interval,
    symbol: str,
) -> tuple[SpotBar, ...]:
    interval_ms = int(interval) * 60_000
    first = int(start.timestamp() * 1000)
    last = int(end.timestamp() * 1000)
    bars: dict[datetime, SpotBar] = {}
    for cursor in range(first, last + 1, _PAGE_LIMIT * interval_ms):
        page_end = min(last, cursor + _PAGE_LIMIT * interval_ms - 1)
        response = session.get(
            _KLINES,
            params={
                "category": "spot",
                "symbol": symbol,
                "interval": interval,
                "start": cursor,
                "end": page_end,
                "limit": _PAGE_LIMIT,
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = object_mapping(cast("object", response.json()))
        if payload.get("retCode") != 0:
            msg = f"Bybit spot kline request failed: {payload.get('retMsg')}"
            raise ValueError(msg)
        rows = numeric_rows(object_mapping(payload["result"])["list"], 7)
        for row in rows:
            if not cursor <= row[0] <= page_end:
                continue
            bar = _spot_bar(row, interval_ms=interval_ms)
            if bar.time in bars:
                msg = "Bybit returned duplicate spot klines."
                raise ValueError(msg)
            bars[bar.time] = bar
    return tuple(bars[stamp] for stamp in sorted(bars))


def load_retest_votes(
    session: requests.Session,
    start: datetime,
    cutoff: datetime,
    *,
    settings: RetestSettings,
) -> tuple[OscillatorObservation, ...]:
    """Load spot quotes with hourly warmup for an inclusive decision window.

    Returns:
        Chronological votes from the sample covering start through cutoff.

    """
    history = load_retest_history(
        session, start, cutoff, settings=settings, symbol="BTCUSDT"
    )
    return retest_votes(history, settings=settings)


def load_retest_history(
    session: requests.Session,
    start: datetime,
    cutoff: datetime,
    *,
    settings: RetestSettings,
    symbol: str,
) -> RetestHistory:
    """Load completed hourly candles and causal spot opening samples.

    Returns:
        Chronological bars with enough warmup before the first sample.

    """
    start, cutoff = start.astimezone(UTC), cutoff.astimezone(UTC)
    first_hour = start.replace(minute=0, second=0, microsecond=0)
    last_hour = cutoff.replace(minute=0, second=0, microsecond=0)
    minutes = 1 if settings.interval == "1m" else 5
    first_sample = start.replace(
        minute=start.minute // minutes * minutes, second=0, microsecond=0
    )
    samples = _spot_bars(
        session,
        first_sample,
        cutoff,
        interval="1" if minutes == 1 else "5",
        symbol=symbol,
    )
    hourly = _spot_bars(
        session,
        first_hour - settings.lookback * HOUR,
        last_hour - HOUR,
        interval="60",
        symbol=symbol,
    )
    return RetestHistory(hourly=hourly, samples=samples)
