# Copyright 2026 anchored-vwam-backtest contributors
"""Archive snapshot-tunnel exchange inputs with actual availability times."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
import zlib
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import requests

from shared.validation import json_object

BYBIT = "https://api.bybit.com/v5/"
BITGET = "https://api.bitget.com/api/v3/market/"
DERIBIT = "https://www.deribit.com/api/v2/public/"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class Source:
    """Describe an exchange read without storing authentication material."""

    name: str
    url: str
    params: dict[str, str]


def sources(asset: str) -> tuple[Source, ...]:
    """Return raw inputs required to reconstruct chains and oscillators.

    Returns:
        Public source requests, including hourly warmup series.

    Raises:
        ValueError: If the asset is unsupported.

    """
    if asset not in {"BTC", "ETH"}:
        msg = "Asset must be BTC or ETH."
        raise ValueError(msg)
    market = {"category": "linear", "symbol": f"{asset}USDT"}
    return (
        Source(
            name="instruments",
            url=DERIBIT + "get_instruments",
            params={"currency": asset, "kind": "option", "expired": "false"},
        ),
        Source(
            name="options",
            url=DERIBIT + "get_book_summary_by_currency",
            params={"currency": asset, "kind": "option"},
        ),
        Source(
            name="spot",
            url=DERIBIT + "get_index_price",
            params={"index_name": asset.lower() + "_usd"},
        ),
        Source(
            name="depth",
            url=BYBIT + "market/orderbook",
            params={**market, "limit": "50"},
        ),
        Source(
            name="candles",
            url=BYBIT + "market/kline",
            params={**market, "interval": "1", "limit": "1000"},
        ),
        Source(
            name="ticker",
            url=BYBIT + "market/tickers",
            params=market,
        ),
        Source(
            name="hourly_candles",
            url=BYBIT + "market/kline",
            params={**market, "interval": "60", "limit": "1000"},
        ),
        Source(
            name="oi",
            url=BYBIT + "market/open-interest",
            params={**market, "intervalTime": "1h", "limit": "200"},
        ),
        *bitget_sources(asset),
    )


def bitget_sources(asset: str) -> tuple[Source, ...]:
    """Describe the same public lending feeds used by the local calculator.

    Returns:
        Hourly/daily growth windows and the current VIP0 quote.

    """
    return (
        *(
            Source(
                name=f"bitget_growth_{period}",
                url=BITGET + "margin-loan-growth",
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
            url=BITGET + "margin-loans",
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
    if source.url.startswith(BITGET):
        time.sleep(1)
    return session.get(source.url, params=source.params, timeout=30)


def _validate_response(response: requests.Response, *, source: Source) -> None:
    response.raise_for_status()
    body = json_object(response.text)
    if source.url.startswith(BITGET):
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


def _prepare_archive(db: sqlite3.Connection) -> None:
    """Upgrade existing archives without discarding historical observations."""
    with db:
        _ = db.execute("""CREATE TABLE IF NOT EXISTS tunnel_inputs (
                id INTEGER PRIMARY KEY, asset TEXT NOT NULL,
                source TEXT NOT NULL, requested_at TEXT NOT NULL,
                available_at TEXT NOT NULL, payload TEXT NOT NULL,
                error TEXT NOT NULL)""")
        schema: list[tuple[object, ...]] = db.execute(
            "PRAGMA table_info(tunnel_inputs)"
        ).fetchall()
        columns = {row[1] for row in schema}
        for name in ("collection_id", "minute_at"):
            if name not in columns:
                _ = db.execute(
                    f"ALTER TABLE tunnel_inputs ADD COLUMN {name} TEXT"
                )
        _ = db.execute(
            "CREATE INDEX IF NOT EXISTS tunnel_inputs_minute "
            "ON tunnel_inputs(asset, minute_at, collection_id)"
        )


def _prepare_compact_archive(db: sqlite3.Connection) -> None:
    """Create compressed payload storage and timed observations."""
    with db:
        _ = db.execute("""CREATE TABLE IF NOT EXISTS tunnel_payloads (
            digest TEXT PRIMARY KEY, codec TEXT NOT NULL,
            payload BLOB NOT NULL)""")
        _ = db.execute("""CREATE TABLE IF NOT EXISTS tunnel_observations (
            id INTEGER PRIMARY KEY, asset TEXT NOT NULL,
            source TEXT NOT NULL, requested_at TEXT NOT NULL,
            available_at TEXT NOT NULL, digest TEXT NOT NULL
                REFERENCES tunnel_payloads(digest),
            error TEXT NOT NULL, collection_id TEXT NOT NULL,
            minute_at TEXT NOT NULL)""")
        _ = db.execute(
            "CREATE INDEX IF NOT EXISTS tunnel_observations_minute "
            "ON tunnel_observations(asset, minute_at, collection_id)"
        )


def _store_observation(
    db: sqlite3.Connection,
    values: tuple[str, ...],
    *,
    storage_mode: Literal["raw", "compact"],
) -> None:
    """Store exact response bytes once while retaining every observation."""
    asset, source, requested, available, payload, error, collection, minute = (
        values
    )
    if storage_mode == "raw":
        _ = db.execute(
            "INSERT INTO tunnel_inputs "
            "(asset, source, requested_at, available_at, payload, error, "
            "collection_id, minute_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return
    encoded = payload.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    _ = db.execute(
        "INSERT OR IGNORE INTO tunnel_payloads VALUES (?, 'zlib', ?)",
        (digest, zlib.compress(encoded)),
    )
    _ = db.execute(
        "INSERT INTO tunnel_observations "
        "(asset, source, requested_at, available_at, digest, error, "
        "collection_id, minute_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            asset,
            source,
            requested,
            available,
            digest,
            error,
            collection,
            minute,
        ),
    )


def collect(
    path: Path,
    session: requests.Session,
    *,
    asset: str = "BTC",
    storage_mode: Literal["raw", "compact"] = "raw",
) -> int:
    """Commit each response independently, retaining failed-source diagnostics.

    Raw JSON remains an external-data boundary; replay consumers must validate
    individual rows. No oscillator decisions or historical availability are
    inferred here. Returns the number of failed sources.

    Returns:
        The collect result.

    """
    requests_to_make = sources(asset)
    path.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    with closing(sqlite3.connect(path, timeout=30)) as db:
        if storage_mode == "raw":
            _prepare_archive(db)
        else:
            _prepare_compact_archive(db)
        collection_id = uuid4().hex
        minute = datetime.now(UTC).replace(second=0, microsecond=0).isoformat()
        for source in requests_to_make:
            requested = datetime.now(UTC).isoformat()
            payload, error = "", ""
            response: requests.Response | None = None
            try:
                response = _public_request(session, source)
                available = datetime.now(UTC).isoformat()
                payload = response.text
                _validate_response(response, source=source)
            except (requests.RequestException, ValueError, TypeError) as exc:
                available = datetime.now(UTC).isoformat()
                # Exception URLs/headers may contain credentials. Keep only
                # type.
                error = type(exc).__name__
                failures += 1
                _report_failure(source, response, exc)
            with db:
                _store_observation(
                    db,
                    (
                        asset,
                        source.name,
                        requested,
                        available,
                        payload,
                        error,
                        collection_id,
                        minute,
                    ),
                    storage_mode=storage_mode,
                )
    return failures


class Arguments(argparse.Namespace):
    """Typed command-line destinations."""

    output: Path = Path("tunnel_inputs.sqlite")
    asset: str = "BTC"
    poll_seconds: int = 60
    once: bool = False
    storage_mode: Literal["raw", "compact"] = "raw"


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
    _ = parser.add_argument("--poll-seconds", type=int, default=60)
    _ = parser.add_argument("--once", action="store_true")
    _ = parser.add_argument(
        "--storage-mode", choices=("raw", "compact"), default="raw"
    )
    args = Arguments()
    _ = parser.parse_args(namespace=args)
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    with requests.Session() as session:
        while True:
            failures = collect(
                args.output,
                session,
                asset=args.asset,
                storage_mode=args.storage_mode,
            )
            _ = sys.stdout.write(
                str(json.dumps({"failed_sources": failures})) + "\n"
            )
            _ = sys.stdout.flush()
            if args.once:
                raise SystemExit(int(failures > 0))
            time.sleep(args.poll_seconds - time.time() % args.poll_seconds)


if __name__ == "__main__":
    main()
