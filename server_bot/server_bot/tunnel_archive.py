# Copyright 2026 anchored-vwam-backtest contributors
"""Archive snapshot-tunnel exchange inputs with actual availability times."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import requests
from market_storage.archive import Archive
from market_storage.models import Observation, asset_value
from market_storage.models import Source as StoredSource

from shared.validation import json_object

if TYPE_CHECKING:
    from collections.abc import Generator

BYBIT = "https://api.bybit.com/v5/"
BITGET = "https://api.bitget.com/api/v3/market/"
DERIBIT = "https://www.deribit.com/api/v2/public/"
LOGGER = logging.getLogger(__name__)
COLLECTION_SECONDS = 300


@dataclass(frozen=True, kw_only=True)
class Source:
    """Describe an exchange read without storing authentication material."""

    name: str
    url: str
    params: dict[str, str]


@dataclass(frozen=True, kw_only=True)
class Endpoints:
    """Exchange addresses, replaceable by deterministic HTTP test servers."""

    bybit: str = BYBIT
    bitget: str = BITGET
    deribit: str = DERIBIT


@dataclass(frozen=True, kw_only=True)
class CollectionSettings:
    """Independent schedule, retention and transport configuration."""

    asset: str = "BTC"
    storage_mode: Literal["raw", "compact"] = "compact"
    retention: timedelta = timedelta(hours=24)
    endpoints: Endpoints = Endpoints()

    def __post_init__(self) -> None:
        """Reject invalid configuration before creating files or requests.

        Raises:
            ValueError: If retention or asset is invalid.

        """
        _ = asset_value(self.asset)
        if self.retention <= timedelta(0):
            msg = "Retention must be positive."
            raise ValueError(msg)


def sources(
    asset: str, *, endpoints: Endpoints | None = None
) -> tuple[Source, ...]:
    """Describe current GEX/book inputs, 5-minute OI, and hourly margin feeds.

    Returns:
        Public requests with no OHLCV or authenticated exchange data.

    """
    endpoints = endpoints or Endpoints()
    _ = asset_value(asset)
    market = {"category": "linear", "symbol": f"{asset}USDT"}
    return (
        Source(
            name="instruments",
            url=endpoints.deribit + "get_instruments",
            params={"currency": asset, "kind": "option", "expired": "false"},
        ),
        Source(
            name="options",
            url=endpoints.deribit + "get_book_summary_by_currency",
            params={"currency": asset, "kind": "option"},
        ),
        Source(
            name="spot",
            url=endpoints.deribit + "get_index_price",
            params={"index_name": asset.lower() + "_usd"},
        ),
        Source(
            name="depth",
            url=endpoints.bybit + "market/orderbook",
            params={**market, "limit": "50"},
        ),
        Source(
            name="ticker", url=endpoints.bybit + "market/tickers", params=market
        ),
        Source(
            name="oi",
            url=endpoints.bybit + "market/open-interest",
            params={**market, "intervalTime": "5min", "limit": "1"},
        ),
        *bitget_sources(asset, endpoint=endpoints.bitget),
    )


def bitget_sources(asset: str, *, endpoint: str = BITGET) -> tuple[Source, ...]:
    """Describe hourly/daily loan growth and the current VIP0 quote.

    Returns:
        The public lending feeds used by the local calculator.

    """
    return (
        *(
            Source(
                name=f"bitget_growth_{period}",
                url=endpoint + "margin-loan-growth",
                params={
                    "symbol": f"{asset}USDT",
                    "coin": asset,
                    "period": period,
                },
            )
            for period in ("24h", "30d")
        ),
        Source(
            name="bitget_loan_rate",
            url=endpoint + "margin-loans",
            params={"coin": asset, "level": "vip0"},
        ),
    )


def _public_request(
    session: requests.Session, source: Source
) -> requests.Response:
    """Space Bitget requests to respect its public endpoint rate limit.

    Returns:
        The original exchange response.

    """
    if source.name.startswith("bitget_"):
        time.sleep(1)
    return session.get(source.url, params=source.params, timeout=30)


def _validate_response(response: requests.Response, *, source: Source) -> None:
    response.raise_for_status()
    body = json_object(response.text)
    if source.name.startswith("bitget_"):
        if body.get("code") != "00000" or body.get("data") is None:
            msg = "Bitget returned an error or missing data."
            raise ValueError(msg)
        return
    if body.get("retCode", 0) != 0 or "error" in body or "result" not in body:
        msg = "Exchange returned an error or missing result."
        raise ValueError(msg)


def _response_code(response: requests.Response | None) -> int | str | None:
    """Extract only numeric API codes, never arbitrary exchange messages.

    Returns:
        A safe numeric error code, if the response includes one.

    """
    if response is None:
        return None
    try:
        body = json_object(response.text)
    except ValueError, TypeError:
        return None
    code = body.get("retCode", body.get("code"))
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    if isinstance(code, str) and re.fullmatch(r"[0-9]{5,6}", code):
        return code
    return None


def _report_failure(
    source: Source,
    response: requests.Response | None,
    error: Exception,
) -> None:
    """Log safe source diagnostics without exception text or response bodies."""
    code = _response_code(response)
    hint = "Check exchange availability and the archived response body locally."
    LOGGER.warning(
        "Archive source failed: %s",
        json.dumps({
            "source": source.name,
            "error": type(error).__name__,
            "http_status": (
                response.status_code if response is not None else None
            ),
            "api_code": code,
            "hint": hint,
        }),
    )


def _collect_batch(
    archive: Archive,
    session: requests.Session,
    *,
    requests_to_make: tuple[Source, ...],
    asset: str,
    collection: str | None = None,
) -> int:
    """Commit each response independently before exposing any calculation data.

    Returns:
        Number of failed sources, with exact failed responses kept as evidence.

    """
    failures = 0
    collection = collection or uuid4().hex
    collection_started = datetime.now(UTC)
    for source in requests_to_make:
        requested = datetime.now(UTC)
        payload, error = "", ""
        response: requests.Response | None = None
        try:
            response = _public_request(session, source)
            payload = response.text
            _validate_response(response, source=source)
        except (requests.RequestException, ValueError, TypeError) as exc:
            error = type(exc).__name__
            failures += 1
            _report_failure(source, response, exc)
        archive.store(
            Observation(
                asset=asset_value(asset),
                source=StoredSource(source.name),
                requested=requested,
                available=datetime.now(UTC),
                payload=payload,
                error=error,
                collection=collection,
            ),
            collection_started=collection_started,
        )
    return failures


def collect(
    path: Path,
    session: requests.Session,
    *,
    asset: str = "BTC",
    storage_mode: Literal["raw", "compact"] = "raw",
) -> int:
    """Collect an explicit unscheduled batch for compatibility and diagnostics.

    Returns:
        Number of failed sources. Production loops use collect_due instead.

    """
    return _collect_batch(
        Archive(path=path, mode=storage_mode),
        session,
        requests_to_make=sources(asset),
        asset=asset,
    )


@contextmanager
def _collection_lock(path: Path) -> Generator[None]:
    """Serialize standalone and bot-owned collectors sharing one archive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".collection.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def collect_due(
    path: Path,
    session: requests.Session,
    *,
    settings: CollectionSettings | None = None,
) -> int:
    """Collect sources due in their UTC buckets and enforce rolling retention.

    Returns:
        Number of failed sources; failures remain eligible for retry.

    """
    settings = settings or CollectionSettings()
    archive = Archive(path=path, mode=settings.storage_mode)
    with _collection_lock(path):
        now = datetime.now(UTC)
        due = tuple(
            source
            for source in sources(settings.asset, endpoints=settings.endpoints)
            if archive.due(settings.asset, StoredSource(source.name), now=now)
        )
        failures = _collect_batch(
            archive,
            session,
            requests_to_make=due,
            asset=settings.asset,
            collection=f"{settings.asset}:{int(now.timestamp()) // 300}",
        )
        archive.prune(now=datetime.now(UTC), retention=settings.retention)
    return failures


class Arguments(argparse.Namespace):
    """Typed command-line destinations."""

    output: Path = Path("tunnel_inputs.sqlite")
    asset: str = "BTC"
    poll_seconds: int = 300
    once: bool = False
    storage_mode: Literal["raw", "compact"] = "compact"
    retention_hours: int = 24
    bybit_url: str = BYBIT
    bitget_url: str = BITGET
    deribit_url: str = DERIBIT


def main() -> None:
    """Collect once or continuously without submitting exchange orders.

    Raises:
        SystemExit: If the requested operation cannot safely proceed.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--output", type=Path, default=Path("tunnel_inputs.sqlite")
    )
    _ = parser.add_argument("--asset", choices=("BTC", "ETH"), default="BTC")
    _ = parser.add_argument("--poll-seconds", type=int, default=300)
    _ = parser.add_argument("--once", action="store_true")
    _ = parser.add_argument(
        "--storage-mode", choices=("raw", "compact"), default="compact"
    )
    _ = parser.add_argument("--retention-hours", type=int, default=24)
    _ = parser.add_argument("--bybit-url", default=BYBIT)
    _ = parser.add_argument("--bitget-url", default=BITGET)
    _ = parser.add_argument("--deribit-url", default=DERIBIT)
    args = Arguments()
    _ = parser.parse_args(namespace=args)
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    if args.poll_seconds > COLLECTION_SECONDS:
        parser.error(
            "--poll-seconds must not exceed the five-minute schedule (300)"
        )
    if args.retention_hours < 1:
        parser.error("--retention-hours must be positive")
    settings = CollectionSettings(
        asset=args.asset,
        storage_mode=args.storage_mode,
        retention=timedelta(hours=args.retention_hours),
        endpoints=Endpoints(
            bybit=args.bybit_url,
            bitget=args.bitget_url,
            deribit=args.deribit_url,
        ),
    )
    with requests.Session() as session:
        while True:
            failures = collect_due(args.output, session, settings=settings)
            _ = sys.stdout.write(
                str(json.dumps({"failed_sources": failures})) + "\n"
            )
            _ = sys.stdout.flush()
            if args.once:
                raise SystemExit(int(failures > 0))
            time.sleep(args.poll_seconds - time.time() % args.poll_seconds)


if __name__ == "__main__":
    main()
