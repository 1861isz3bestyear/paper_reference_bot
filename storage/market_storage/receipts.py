# Copyright 2026 avwap-workspace-tests contributors
"""Queue only acknowledged copied observations for deletion after 48 hours."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from market_storage.merge import decode_record, records
from market_storage.models import aware

if TYPE_CHECKING:
    from pathlib import Path

    from market_storage.models import Observation


def fingerprint(record: Observation) -> str:
    """Identify exact response content and causal time independently of row IDs.

    Returns:
        A stable digest shared by raw and compact records.

    """
    content = json.dumps(
        [
            record.asset,
            record.source.value,
            record.requested.astimezone(UTC).isoformat(),
            record.available.astimezone(UTC).isoformat(),
            record.error,
            record.payload,
        ],
        ensure_ascii=True,
    )
    return hashlib.sha256(content.encode()).hexdigest()


def enable_copy_retention(path: Path) -> None:
    """Switch this VPS archive from age pruning to acknowledged-copy cleanup."""
    with closing(sqlite3.connect(path)) as db, db:
        _ = db.execute(
            "CREATE TABLE IF NOT EXISTS copy_receipts ("
            "fingerprint TEXT PRIMARY KEY, copied_at TEXT NOT NULL, "
            "delete_after TEXT NOT NULL)"
        )


def manifest(path: Path) -> bytes:
    """Build acknowledgements from the downloaded, checked snapshot only.

    Returns:
        Newline-delimited SHA256 fingerprints suitable for SSH stdin.

    """
    tokens = sorted({fingerprint(row) for row, _ in records(path)})
    return "".join(token + "\n" for token in tokens).encode("ascii")


def acknowledge(path: Path, content: str, *, now: datetime) -> int:
    """Mark matching unchanged records copied; retries keep the first deadline.

    Returns:
        Number of matched unique fingerprints, including previous receipts.

    Raises:
        ValueError: If the receipt protocol contains an invalid fingerprint.

    """
    aware(now)
    tokens = set(content.splitlines())
    if any(
        len(token) != hashlib.sha256().digest_size * 2
        or any(c not in "0123456789abcdef" for c in token)
        for token in tokens
    ):
        msg = "Invalid copy receipt fingerprint."
        raise ValueError(msg)
    matched = tokens & {fingerprint(row) for row, _ in records(path)}
    enable_copy_retention(path)
    stamp = now.astimezone(UTC)
    with closing(sqlite3.connect(path)) as db, db:
        _ = db.executemany(
            "INSERT OR IGNORE INTO copy_receipts VALUES (?,?,?)",
            [
                (
                    token,
                    stamp.isoformat(),
                    (stamp + timedelta(hours=48)).isoformat(),
                )
                for token in matched
            ],
        )
    return len(matched)


def cleanup(path: Path, *, now: datetime) -> int:
    """Delete due copied rows only; never delete unacknowledged observations.

    Returns:
        Deleted observation count; payloads are reclaimed when unreferenced.

    """
    aware(now)
    with closing(sqlite3.connect(path)) as db:
        due: list[tuple[str]] = db.execute(
            "SELECT fingerprint FROM copy_receipts WHERE delete_after<=?",
            (now.astimezone(UTC).isoformat(),),
        ).fetchall()
        tokens = {row[0] for row in due}
        targets = {fingerprint(row) for row, _ in records(path)} & tokens
        return _delete_copied(db, targets)


def _delete_copied(db: sqlite3.Connection, targets: set[str]) -> int:
    # Decode within the same write transaction used for deletion, preventing
    # a collector from changing a row between its validation and removal.
    removed = 0
    with db:
        _ = db.execute("BEGIN IMMEDIATE")
        for table, query in _cleanup_queries().items():
            removed += _delete_table(db, targets, table=table, query=query)
        _ = db.execute(
            "DELETE FROM tunnel_payloads WHERE digest NOT IN "
            "(SELECT digest FROM tunnel_observations)"
        )
    return removed


def _cleanup_queries() -> dict[str, str]:
    return {
        "tunnel_inputs": (
            "SELECT"
            " id,asset,source,requested_at,available_at,payload,error,"
            "collection_id,minute_at"
            " FROM tunnel_inputs"
        ),
        "tunnel_observations": (
            "SELECT"
            " o.id,o.asset,o.source,o.requested_at,o.available_at,p.payload,"
            "o.error,o.collection_id,o.minute_at,p.digest,p.codec"
            " FROM tunnel_observations o JOIN tunnel_payloads p ON"
            " p.digest=o.digest"
        ),
    }


def _delete_table(
    db: sqlite3.Connection, targets: set[str], *, table: str, query: str
) -> int:
    queries = {
        "tunnel_inputs": "DELETE FROM tunnel_inputs WHERE id=?",
        "tunnel_observations": "DELETE FROM tunnel_observations WHERE id=?",
    }
    rows: list[tuple[object, ...]] = db.execute(query).fetchall()
    removed = 0
    for row in rows:
        record = decode_record(row[1:], compact=table == "tunnel_observations")
        if fingerprint(record) in targets:
            removed += db.execute(queries[table], (row[0],)).rowcount
    return removed
