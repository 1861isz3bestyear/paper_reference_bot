# Copyright 2026 avwap-workspace-tests contributors
"""Persist a validated response within a caller-owned transaction."""

import hashlib
import zlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import sqlite3

    from market_storage.models import Observation


def insert_observation(
    db: sqlite3.Connection,
    observation: Observation,
    *,
    mode: Literal["raw", "compact"],
    collection_started: datetime | None,
) -> None:
    """Insert a validated observation into a caller-owned transaction."""
    requested = observation.requested.astimezone(UTC).isoformat()
    available = observation.available.astimezone(UTC).isoformat()
    minute = (
        (collection_started or observation.requested)
        .astimezone(UTC)
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
