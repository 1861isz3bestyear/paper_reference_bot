# Copyright 2026 avwap-workspace-tests contributors
"""Merge checked overlapping archives without erasing history."""

import fcntl
import hashlib
import json
import os
import sqlite3
import zlib
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from market_storage.backup import validate
from market_storage.models import Observation, Source, asset_value, aware
from market_storage.schema import prepare
from market_storage.writing import insert_observation

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True, kw_only=True)
class Timeline:
    """Successful source coverage within an observed asset time range."""

    asset: str
    source: Source
    first_requested_at: str
    last_requested_at: str
    successful_buckets: int
    missing_buckets: int


@dataclass(frozen=True, kw_only=True)
class MergeReport:
    """Counts and explicit gaps; a gap is never a fabricated observation."""

    observations: int
    duplicates: int
    timelines: tuple[Timeline, ...]


def _text(value: object) -> str:
    if not isinstance(value, str):
        msg = "Archive metadata must contain text."
        raise TypeError(msg)
    return value


def _time(value: object) -> datetime:
    stamp = datetime.fromisoformat(_text(value))
    aware(stamp)
    return stamp.astimezone(UTC)


def _payload(row: tuple[object, ...], *, compact: bool) -> str:
    if not compact:
        return _text(row[4])
    body = row[4]
    if not isinstance(body, bytes) or row[9] != "zlib":
        msg = "Invalid compressed payload or codec."
        raise ValueError(msg)
    try:
        decoded = zlib.decompress(body)
        result = decoded.decode("utf-8")
    except (zlib.error, UnicodeError) as exc:
        msg = "Corrupt compressed archive payload."
        raise ValueError(msg) from exc
    if hashlib.sha256(decoded).hexdigest() != row[8]:
        msg = "Archive payload digest mismatch."
        raise ValueError(msg)
    return result


def decode_record(row: tuple[object, ...], *, compact: bool) -> Observation:
    """Validate a database row before using it as a domain observation.

    Returns:
        A causal observation with verified payload content.

    """
    payload = _payload(row, compact=compact)
    if not _text(row[5]):
        json.loads(payload)
    return Observation(
        asset=asset_value(_text(row[0])),
        source=Source(_text(row[1])),
        requested=_time(row[2]),
        available=_time(row[3]),
        payload=payload,
        error=_text(row[5]),
        collection=_text(row[6]),
    )


def records(path: Path) -> Iterator[tuple[Observation, datetime]]:
    """Stream checked metadata and decoded payloads from both storage modes.

    Yields:
        Validated observations with their original collection minute.

    """
    validate(path)
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        tables: list[tuple[str]] = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {row[0] for row in tables}
        for table in ("tunnel_inputs", "tunnel_observations"):
            if table not in names:
                continue
            compact = table == "tunnel_observations"
            query = (
                "SELECT"
                " o.asset,o.source,o.requested_at,o.available_at,p.payload,"
                "o.error,o.collection_id,o.minute_at,p.digest,p.codec"
                " FROM tunnel_observations o LEFT JOIN tunnel_payloads p ON"
                " p.digest=o.digest"
                if compact
                else (
                    "SELECT"
                    " asset,source,requested_at,available_at,payload,error,"
                    "collection_id,minute_at"
                    " FROM tunnel_inputs"
                )
            )
            cursor = db.execute(query)
            for row in _rows(cursor):
                record = decode_record(row, compact=compact)
                yield record, _collection_minute(row[7], record)


def _append(
    db: sqlite3.Connection, record: Observation, minute: datetime
) -> bool:
    key = (
        record.asset,
        record.source.value,
        record.requested.isoformat(),
        record.available.isoformat(),
        record.error,
    )
    previous: list[tuple[str]] = db.execute(
        "SELECT digest FROM tunnel_observations WHERE asset=? AND source=? "
        "AND requested_at=? AND available_at=? AND error=?",
        key,
    ).fetchall()
    digest = hashlib.sha256(record.payload.encode()).hexdigest()
    if previous:
        if previous != [(digest,)]:
            msg = "Conflicting payloads for the same observation time."
            raise ValueError(msg)
        return False
    insert_observation(db, record, mode="compact", collection_started=minute)
    return True


def _timelines(db: sqlite3.Connection) -> tuple[Timeline, ...]:
    rows: list[tuple[str, str, str, str]] = db.execute(
        "SELECT asset,source,requested_at,error FROM tunnel_observations "
        "ORDER BY requested_at"
    ).fetchall()
    buckets: dict[tuple[str, Source], set[int]] = {}
    bounds: dict[str, tuple[int, int]] = {}
    for asset, name, stamp, error in rows:
        source = Source(name)
        second = int(datetime.fromisoformat(stamp).timestamp())
        if not error:
            buckets.setdefault((asset, source), set()).add(
                second // source.interval
            )
        first, last = bounds.get(asset, (second, second))
        bounds[asset] = (min(first, second), max(last, second))
    return tuple(
        Timeline(
            asset=asset,
            source=source,
            first_requested_at=datetime.fromtimestamp(first, UTC).isoformat(),
            last_requested_at=datetime.fromtimestamp(last, UTC).isoformat(),
            successful_buckets=len(buckets.get((asset, source), set())),
            missing_buckets=(
                last // source.interval
                - first // source.interval
                + 1
                - len(buckets.get((asset, source), set()))
            ),
        )
        for asset, (first, last) in sorted(bounds.items())
        for source in Source
    )


def _merge_rows(db: sqlite3.Connection, paths: tuple[Path, ...]) -> MergeReport:
    count = duplicates = 0
    for path in paths:
        for record, minute in records(path):
            if _append(db, record, minute):
                count += 1
            else:
                duplicates += 1
    return MergeReport(
        observations=count, duplicates=duplicates, timelines=_timelines(db)
    )


def _build(source: Path, destination: Path, staging: Path) -> MergeReport:
    paths = (destination, source) if destination.exists() else (source,)
    with closing(sqlite3.connect(staging)) as db:
        prepare(db)
        _ = db.execute(
            "CREATE INDEX merge_identity ON tunnel_observations "
            "(asset,source,requested_at,available_at,error)"
        )
        with db:
            report = _merge_rows(db, paths)
    validate(staging)
    return report


def merge(source: Path, destination: Path) -> MergeReport:
    """Publish a checked union under a writer lock; failures preserve history.

    Returns:
        Unique observation count, removed repeats and source gap counts.

    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        destination.with_suffix(destination.suffix + ".lock").open("a") as lock,
        TemporaryDirectory(dir=destination.parent) as directory,
    ):
        fcntl.flock(lock, fcntl.LOCK_EX)
        staging = Path(directory) / "merged.sqlite"
        report = _build(source, destination, staging)
        with staging.open("rb") as completed:
            os.fsync(completed.fileno())
        _ = staging.replace(destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return report


def format_report(report: MergeReport) -> str:
    """Render machine-readable counts and per-source missing UTC buckets.

    Returns:
        One JSON report without fabricated data or inferred outside bounds.

    """
    return json.dumps({
        "observations": report.observations,
        "duplicates": report.duplicates,
        "timelines": [
            {
                "asset": item.asset,
                "source": item.source.value,
                "first_requested_at": item.first_requested_at,
                "last_requested_at": item.last_requested_at,
                "successful_buckets": item.successful_buckets,
                "missing_buckets": item.missing_buckets,
            }
            for item in report.timelines
        ],
    })


def _rows(cursor: sqlite3.Cursor) -> Iterator[tuple[object, ...]]:
    while True:
        batch: list[tuple[object, ...]] = cursor.fetchmany(256)
        if not batch:
            return
        yield from batch


def check(path: Path) -> MergeReport:
    """Audit an archive without mutating it, including conflicts and gaps.

    Returns:
        The same validation report used before cumulative publication.

    """
    with TemporaryDirectory(prefix="market-check-") as directory:
        root = Path(directory)
        return _build(path, root / "absent.sqlite", root / "checked.sqlite")


def _collection_minute(value: object, record: Observation) -> datetime:
    minute = _time(value)
    if minute > record.requested or minute.second or minute.microsecond:
        msg = "Invalid collection minute in archive."
        raise ValueError(msg)
    return minute
