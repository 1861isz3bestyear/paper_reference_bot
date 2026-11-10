# Copyright 2026 avwap-workspace-tests contributors
"""Create consistent SQLite snapshots while collection continues."""

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def validate(path: Path) -> None:
    """Reject corrupt or unrelated downloads before replacing local data.

    Raises:
        ValueError: If the file is not a complete market archive.

    """
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        rows: list[tuple[str]] = db.execute("PRAGMA quick_check").fetchall()
        if rows != [("ok",)]:
            msg = "SQLite archive integrity check failed."
            raise ValueError(msg)
        tables: list[tuple[str]] = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {row[0] for row in tables}
        if not {"tunnel_inputs", "tunnel_observations"} & names:
            msg = "Downloaded database is not a market archive."
            raise ValueError(msg)
        for table in names & {"tunnel_inputs", "tunnel_observations"}:
            columns: list[tuple[object, ...]] = db.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
            required = {
                "asset",
                "source",
                "requested_at",
                "available_at",
                "error",
                "collection_id",
            }
            if not required <= {row[1] for row in columns}:
                msg = "Market archive schema is incomplete."
                raise ValueError(msg)
        if "tunnel_observations" in names:
            missing = db.execute(
                "SELECT p.payload FROM tunnel_observations o "
                "LEFT JOIN tunnel_payloads p ON p.digest=o.digest "
                "WHERE p.digest IS NULL LIMIT 1"
            )
            if missing.fetchone() is not None:
                msg = "Market archive has missing compressed payloads."
                raise ValueError(msg)


def snapshot(source: Path, destination: Path) -> None:
    """Use SQLite online backup, including committed WAL contents.

    Raises:
        ValueError: If source and destination refer to the same database.

    """
    if source.resolve() == destination.resolve():
        msg = "Backup destination must differ from the source."
        raise ValueError(msg)
    with (
        closing(
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        ) as original,
        closing(sqlite3.connect(destination)) as backup,
    ):
        original.backup(backup)
    validate(destination)
