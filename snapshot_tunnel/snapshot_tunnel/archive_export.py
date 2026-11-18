# Copyright 2026 anchored-vwam-backtest contributors
"""Export complete archived options chains without computing other streams."""

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from market_storage.archive import Archive
from market_storage.inputs import GexInputs, load_inputs
from market_storage.models import Window

from snapshot_tunnel.archive_model import RecordedCollection
from snapshot_tunnel.options_csv import snapshots_csv
from snapshot_tunnel.options_data import normalize_chain
from snapshot_tunnel.options_model import (
    Snapshot,
    parse_snapshots,
    positive_number,
)
from snapshot_tunnel.validation import (
    database_rows,
    json_object,
    object_mapping,
    string,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from io import Writer


COLLECTION_QUERIES = {
    "tunnel_inputs": (
        "SELECT COALESCE(collection_id, 'legacy:raw:' || id) AS collection, "
        "available_at, source FROM tunnel_inputs WHERE asset=? AND error='' "
        "AND source IN ('instruments','options','spot')"
    ),
    "tunnel_observations": (
        "SELECT collection_id AS collection, available_at, source "
        "FROM tunnel_observations WHERE asset=? AND error='' "
        "AND source IN ('instruments','options','spot')"
    ),
}


def collection_windows(
    path: Path, *, asset: str
) -> tuple[RecordedCollection, ...]:
    """Read complete collection bounds without decompressing payloads.

    Returns:
        Chronological collection metadata for archive export and GUI replay.

    """
    selected = "BTC" if asset == "BTC" else "ETH"
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        tables = database_rows(
            db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )
        names = {string(row[0]) for row in tables}
        queries = [
            query for name, query in COLLECTION_QUERIES.items() if name in names
        ]
        if not queries:
            return ()
        query = " ".join((
            "SELECT collection, MIN(available_at), MAX(available_at) FROM (",
            " UNION ALL ".join(queries),
            (
                ") GROUP BY collection HAVING COUNT(DISTINCT source)=3 "
                "ORDER BY MAX(available_at), collection"
            ),
        ))
        rows = database_rows(
            db.execute(query, [selected] * len(queries)).fetchall()
        )
    return tuple(
        RecordedCollection(
            identity=string(row[0]),
            window=Window(
                asset=selected,
                start=datetime.fromisoformat(string(row[1])),
                end=datetime.fromisoformat(string(row[2])),
            ),
        )
        for row in rows
    )


def archive_collections(path: Path, *, asset: str) -> Iterator[GexInputs]:
    """Read one collection at a time in bounded extra memory.

    Yields:
        Complete validated collection envelopes in receipt order.

    """
    archive = Archive(path=path)
    for record in collection_windows(path, asset=asset):
        for packet in load_inputs(archive, record.window).gex:
            if packet.instruments.collection == record.identity:
                yield packet


def archived_snapshot(packet: GexInputs) -> Snapshot:
    """Normalize one chain, omitting contracts expired by its receipt time.

    Returns:
        Options snapshot using the complete collection's availability.

    """
    asset = packet.spot.asset
    index = object_mapping(json_object(packet.spot.payload)["result"])
    options = normalize_chain(
        json_object(packet.instruments.payload)["result"],
        json_object(packet.options.payload)["result"],
        asset=asset,
    )
    return Snapshot(
        asset=asset,
        timestamp=packet.available,
        spot=positive_number(str(index["index_price"])),
        options=tuple(
            item for item in options if item.expiry > packet.available
        ),
    )


def write_options(packets: Iterator[GexInputs], stream: Writer[str]) -> int:
    """Validate and serialize one collection at a time in bounded extra memory.

    Returns:
        Number of exported snapshots.

    Raises:
        ValueError: If receipts collide or a chain has no unexpired options.

    """
    receipts: set[datetime] = set()
    for packet in packets:
        snapshot = archived_snapshot(packet)
        if snapshot.timestamp in receipts or not snapshot.options:
            msg = (
                "Snapshots must have unique receipt times and unexpired"
                " options."
            )
            raise ValueError(msg)
        content = snapshots_csv((snapshot,))
        _ = parse_snapshots(content)
        if receipts:
            content = content.partition("\n")[2]
        _ = stream.write(content)
        receipts.add(snapshot.timestamp)
    return len(receipts)


def export_options(archive: Path, output: Path, *, asset: str = "BTC") -> int:
    """Validate complete collections and atomically publish importable CSV.

    Returns:
        Number of exported snapshots, preserving collection receipt times.

    Raises:
        ValueError: If the asset, destination or complete chain is invalid.

    """
    if asset not in {"BTC", "ETH"}:
        msg = "Asset must be BTC or ETH."
        raise ValueError(msg)
    if output.exists() or output.resolve() == archive.resolve():
        msg = "Export destination must be a new file."
        raise ValueError(msg)
    selected = "BTC" if asset == "BTC" else "ETH"
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            count = write_options(
                archive_collections(archive, asset=selected), stream
            )
            if not count:
                msg = (
                    "No complete successful options collections for this asset."
                )
                raise ValueError(msg)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return count


class Arguments(argparse.Namespace):
    """Typed adapter arguments."""

    archive: Path = Path("tunnel_inputs.sqlite")
    output: Path = Path("options_snapshots.csv")
    asset: str = "BTC"


def main() -> int:
    """Report export outcomes without modifying archive or saved GUI history.

    Returns:
        Zero for a validated export, one for invalid input or IO failures.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("archive", type=Path)
    _ = parser.add_argument("--output", required=True, type=Path)
    _ = parser.add_argument("--asset", choices=("BTC", "ETH"), default="BTC")
    args = parser.parse_args(namespace=Arguments())
    try:
        count = export_options(args.archive, args.output, asset=args.asset)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        _ = sys.stderr.write(f"Calculator export failed: {exc}\n")
        return 1
    _ = sys.stdout.write(
        f"Exported {count} options snapshots to {args.output}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
