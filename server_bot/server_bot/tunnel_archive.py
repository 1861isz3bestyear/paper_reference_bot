# Copyright 2026 anchored-vwam-backtest contributors
"""Archive snapshot-tunnel exchange inputs with actual availability times."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

import requests

from shared.validation import json_object

BYBIT = "https://api.bybit.com/v5/"
DERIBIT = "https://www.deribit.com/api/v2/public/"


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
    )


def _margin_request(session: requests.Session) -> requests.Response:
    key = os.environ.get("BYBIT_API_KEY", "")
    secret = os.environ.get("BYBIT_API_SECRET", "")
    if not key or not secret:
        msg = "Margin history requires BYBIT_API_KEY and BYBIT_API_SECRET."
        raise ValueError(msg)
    now = datetime.now(UTC)
    query = urlencode(
        {
            "currency": "USDT",
            "vipLevel": "No VIP",
            "startTime": int((now - timedelta(days=30)).timestamp() * 1000),
            "endTime": int(now.timestamp() * 1000),
        },
        quote_via=quote,
    )
    stamp = str(int(now.timestamp() * 1000))
    signature = hmac.new(
        secret.encode(), f"{stamp}{key}5000{query}".encode(), hashlib.sha256
    ).hexdigest()
    return session.get(
        BYBIT + "spot-margin-trade/interest-rate-history?" + query,
        headers={
            "X-BAPI-API-KEY": key,
            "X-BAPI-TIMESTAMP": stamp,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN": signature,
        },
        timeout=30,
    )


def _validate_response(response: requests.Response) -> None:
    response.raise_for_status()
    body = json_object(response.text)
    if body.get("retCode", 0) != 0 or "error" in body or "result" not in body:
        msg = "Exchange returned an error or missing result."
        raise ValueError(msg)


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


def collect(
    path: Path, session: requests.Session, *, asset: str = "BTC"
) -> int:
    """Commit each response independently, retaining failed-source diagnostics.

    Raw JSON remains an external-data boundary; replay consumers must validate
    individual rows. No oscillator decisions or historical availability are
    inferred here. Returns the number of failed sources.

    """
    requests_to_make = (
        *sources(asset),
        Source(name="margin", url="", params={}),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    with closing(sqlite3.connect(path, timeout=30)) as db:
        _prepare_archive(db)
        collection_id = uuid4().hex
        minute = datetime.now(UTC).replace(second=0, microsecond=0).isoformat()
        for source in requests_to_make:
            requested = datetime.now(UTC).isoformat()
            payload, error = "", ""
            try:
                response = (
                    _margin_request(session)
                    if source.name == "margin"
                    else session.get(
                        source.url, params=source.params, timeout=30
                    )
                )
                available = datetime.now(UTC).isoformat()
                payload = response.text
                _validate_response(response)
            except (requests.RequestException, ValueError, TypeError) as exc:
                available = datetime.now(UTC).isoformat()
                # Exception URLs/headers may contain credentials. Keep only type.
                error = type(exc).__name__
                failures += 1
            with db:
                _ = db.execute(
                    """INSERT INTO tunnel_inputs
                    (asset, source, requested_at, available_at, payload, error,
                     collection_id, minute_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
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
                )
    return failures


class Arguments(argparse.Namespace):
    """Typed command-line destinations."""

    output: Path = Path("tunnel_inputs.sqlite")
    asset: str = "BTC"
    poll_seconds: int = 60
    once: bool = False


def main() -> None:
    """Collect once or continuously without submitting exchange orders."""
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--output", type=Path, default=Path("tunnel_inputs.sqlite")
    )
    _ = parser.add_argument("--asset", choices=("BTC", "ETH"), default="BTC")
    _ = parser.add_argument("--poll-seconds", type=int, default=60)
    _ = parser.add_argument("--once", action="store_true")
    args = Arguments()
    _ = parser.parse_args(namespace=args)
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    with requests.Session() as session:
        while True:
            failures = collect(args.output, session, asset=args.asset)
            _ = sys.stdout.write(
                str(json.dumps({"failed_sources": failures})) + "\n"
            )
            _ = sys.stdout.flush()
            if args.once:
                raise SystemExit(int(failures > 0))
            time.sleep(args.poll_seconds - time.time() % args.poll_seconds)


if __name__ == "__main__":
    main()
