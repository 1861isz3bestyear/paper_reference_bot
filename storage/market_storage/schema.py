# Copyright 2026 avwap-workspace-tests contributors
"""Maintain backward-compatible raw and compressed response tables."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


def prepare(db: sqlite3.Connection) -> None:
    """Upgrade the archive without destroying older observations."""
    with db:
        _ = db.execute("""CREATE TABLE IF NOT EXISTS tunnel_inputs (
            id INTEGER PRIMARY KEY, asset TEXT NOT NULL, source TEXT NOT NULL,
            requested_at TEXT NOT NULL, available_at TEXT NOT NULL,
            payload TEXT NOT NULL, error TEXT NOT NULL)""")
        rows: list[tuple[object, ...]] = db.execute(
            "PRAGMA table_info(tunnel_inputs)"
        ).fetchall()
        columns = {row[1] for row in rows}
        for name in ("collection_id", "minute_at"):
            if name not in columns:
                _ = db.execute(
                    f"ALTER TABLE tunnel_inputs ADD COLUMN {name} TEXT"
                )
        _ = db.execute("""CREATE TABLE IF NOT EXISTS tunnel_payloads (
            digest TEXT PRIMARY KEY, codec TEXT NOT NULL, payload BLOB NOT NULL)
        """)
        _ = db.execute("""CREATE TABLE IF NOT EXISTS tunnel_observations (
            id INTEGER PRIMARY KEY, asset TEXT NOT NULL, source TEXT NOT NULL,
            requested_at TEXT NOT NULL, available_at TEXT NOT NULL,
            digest TEXT NOT NULL REFERENCES tunnel_payloads(digest),
            error TEXT NOT NULL, collection_id TEXT NOT NULL,
            minute_at TEXT NOT NULL)""")
        for table in ("tunnel_inputs", "tunnel_observations"):
            _ = db.execute(
                f"CREATE INDEX IF NOT EXISTS {table}_source_time "
                f"ON {table}(asset, source, available_at)"
            )
