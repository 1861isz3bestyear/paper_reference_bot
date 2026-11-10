# Copyright 2026 avwap-workspace-tests contributors
"""Commit exact inputs before exposing them to calculation consumers."""

import hashlib
import sqlite3
import zlib
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from market_storage.models import Observation, Source, Window, aware
from market_storage.schema import prepare

DUE_QUERIES = {
    "tunnel_inputs": (
        "SELECT 1 FROM tunnel_inputs WHERE asset=? AND source=? "
        "AND error='' AND requested_at>=? AND available_at<=? LIMIT 1"
    ),
    "tunnel_observations": (
        "SELECT 1 FROM tunnel_observations WHERE asset=? AND source=? "
        "AND error='' AND requested_at>=? AND available_at<=? LIMIT 1"
    ),
}
READ_QUERIES = {
    "tunnel_inputs": (
        "SELECT source,requested_at,available_at,payload, "
        "COALESCE(collection_id, 'legacy:raw:' || id) "
        "FROM tunnel_inputs WHERE asset=? AND error='' AND available_at>=? "
        "AND available_at<=? ORDER BY available_at,id"
    ),
    "tunnel_observations": (
        "SELECT"
        " o.source,o.requested_at,o.available_at,p.payload,o.collection_id FROM"
        " tunnel_observations o JOIN tunnel_payloads p ON p.digest=o.digest"
        " WHERE o.asset=? AND o.error='' AND o.available_at>=? AND"
        " o.available_at<=? ORDER BY o.available_at,o.id"
    ),
}
DELETE_QUERIES = {
    "tunnel_inputs": "DELETE FROM tunnel_inputs WHERE available_at<?",
    "tunnel_observations": (
        "DELETE FROM tunnel_observations WHERE available_at<?"
    ),
}

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class Archive:
    """Own the SQLite boundary and compressed content-addressed payloads."""

    path: Path
    mode: Literal["raw", "compact"] = "compact"

    def store(self, observation: Observation) -> None:
        """Durably commit one response before a consumer can load it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            prepare(db)
            with db:
                _insert(db, observation, mode=self.mode)

    def load(self, window: Window) -> tuple[Observation, ...]:
        """Read only successful observations received by the cutoff.

        Returns:
            Validated chronological observations, including revised responses.

        """
        with closing(_read_connection(self.path)) as db:
            rows = _read_rows(db, window)
        return tuple(sorted(rows, key=lambda row: row.available))

    def due(self, asset: str, source: Source, *, now: datetime) -> bool:
        """Persist cadence across restarts and retry failures within a bucket.

        Returns:
            Whether no successful request exists in the current UTC bucket.

        """
        aware(now)
        if not self.path.exists():
            return True
        bucket = int(now.timestamp()) // source.interval * source.interval
        start = datetime.fromtimestamp(bucket, UTC).isoformat()
        with closing(_read_connection(self.path)) as db:
            for table in _tables(db):
                result: list[tuple[object, ...]] = db.execute(
                    DUE_QUERIES[table],
                    (
                        asset,
                        source.value,
                        start,
                        now.astimezone(UTC).isoformat(),
                    ),
                ).fetchall()
                if result:
                    return False
        return True

    def prune(self, *, now: datetime, retention: timedelta) -> None:
        """Remove expired observations and unreferenced compressed payloads.

        Raises:
            ValueError: If retention is not positive.

        """
        aware(now)
        if retention <= timedelta(0):
            msg = "Retention must be positive."
            raise ValueError(msg)
        if not self.path.exists():
            return
        cutoff = (now - retention).astimezone(UTC).isoformat()
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            prepare(db)
            with db:
                for table in _tables(db):
                    _ = db.execute(
                        DELETE_QUERIES[table],
                        (cutoff,),
                    )
                _ = db.execute(
                    "DELETE FROM tunnel_payloads WHERE digest NOT IN "
                    "(SELECT digest FROM tunnel_observations)"
                )


def _insert(
    db: sqlite3.Connection,
    observation: Observation,
    *,
    mode: Literal["raw", "compact"],
) -> None:
    requested = observation.requested.astimezone(UTC).isoformat()
    available = observation.available.astimezone(UTC).isoformat()
    minute = (
        observation.requested.astimezone(UTC)
        .replace(second=0, microsecond=0)
        .isoformat()
    )
    identity = (
        observation.asset,
        observation.source.value,
        requested,
        available,
    )
    tail = (observation.error, observation.collection, minute)
    if mode == "raw":
        _ = db.execute(
            """INSERT INTO tunnel_inputs
            (asset,source,requested_at,available_at,payload,error,collection_id,
             minute_at) VALUES (?,?,?,?,?,?,?,?)""",
            (*identity, observation.payload, *tail),
        )
        return
    encoded = observation.payload.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    _ = db.execute(
        "INSERT OR IGNORE INTO tunnel_payloads VALUES (?, 'zlib', ?)",
        (digest, zlib.compress(encoded)),
    )
    _ = db.execute(
        "INSERT INTO tunnel_observations "
        "(asset,source,requested_at,available_at,digest,error,collection_id,"
        "minute_at) VALUES (?,?,?,?,?,?,?,?)",
        (*identity, digest, *tail),
    )


def _read_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _tables(db: sqlite3.Connection) -> tuple[str, ...]:
    rows: list[tuple[str]] = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('tunnel_inputs','tunnel_observations')"
    ).fetchall()
    return tuple(row[0] for row in rows)


def _read_rows(db: sqlite3.Connection, window: Window) -> list[Observation]:
    observations: list[Observation] = []
    for table in _tables(db):
        compact = table == "tunnel_observations"
        rows: list[tuple[object, ...]] = db.execute(
            READ_QUERIES[table],
            (
                window.asset,
                window.start.astimezone(UTC).isoformat(),
                window.end.astimezone(UTC).isoformat(),
            ),
        ).fetchall()
        for row in rows:
            # Legacy OHLCV and authenticated margin rows are never inputs.
            if row[0] not in {source.value for source in Source}:
                continue
            observations.append(_decode(row, window, compact=compact))
    return observations


def _decode(
    row: tuple[object, ...], window: Window, *, compact: bool
) -> Observation:
    if len(row) != len(
        ("source", "requested", "available", "body", "collection")
    ):
        msg = "Invalid archive observation metadata."
        raise ValueError(msg)
    source, requested, available, body, collection = row
    if not isinstance(source, str) or not isinstance(requested, str):
        msg = "Invalid source or request time."
        raise TypeError(msg)
    if not isinstance(available, str) or not isinstance(collection, str):
        msg = "Invalid receipt time or collection identity."
        raise TypeError(msg)
    if compact and isinstance(body, bytes):
        payload = zlib.decompress(body).decode("utf-8")
    elif not compact and isinstance(body, str):
        payload = body
    else:
        msg = "Invalid stored payload encoding."
        raise TypeError(msg)
    return Observation(
        asset=window.asset,
        source=Source(source),
        requested=datetime.fromisoformat(requested),
        available=datetime.fromisoformat(available),
        payload=payload,
        collection=collection,
    )
