# Copyright 2026 avwap-workspace-tests contributors
"""Persist demo entry intent before sending an exchange order."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from shared.validation import database_rows, json_object, string

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class PendingEntry:
    """A durable order identity and frozen protection reference."""

    link: str
    side: Literal["Buy", "Sell"]
    quantity: Decimal
    stop: Decimal
    target: Decimal
    ceiling: Decimal | None


def pending_entry(content: str) -> PendingEntry:
    """Validate a persisted order intent before exchange reconciliation.

    Returns:
        A typed immutable pending entry.

    Raises:
        ValueError: If the stored order side or prices are invalid.

    """
    values = json_object(content)
    side = string(values["side"])
    if side not in {"Buy", "Sell"}:
        msg = "Invalid pending entry side."
        raise ValueError(msg)
    amounts = [
        Decimal(string(values[key])) for key in ("quantity", "stop", "target")
    ]
    if any(not amount.is_finite() or amount <= 0 for amount in amounts):
        msg = "Invalid pending entry quantities or protection."
        raise ValueError(msg)
    ceiling = values["ceiling"]
    cap = Decimal(string(ceiling)) if ceiling is not None else None
    if cap is not None and (not cap.is_finite() or cap <= 0):
        msg = "Invalid frozen target ceiling."
        raise ValueError(msg)
    return PendingEntry(
        link=string(values["link"]),
        side="Buy" if side == "Buy" else "Sell",
        quantity=amounts[0],
        stop=amounts[1],
        target=amounts[2],
        ceiling=cap,
    )


@dataclass(frozen=True, kw_only=True)
class TunnelJournal:
    """Persist decisions and order intent for one configuration."""

    path: Path
    fingerprint: str

    def prepare(self) -> None:
        """Create the journal and reject configuration changes on resume.

        Raises:
            ValueError: If another configuration owns this journal.

        """
        with closing(sqlite3.connect(self.path)) as db, db:
            _ = db.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value"
                " TEXT NOT NULL)"
            )
            _ = db.execute(
                "CREATE TABLE IF NOT EXISTS decisions (candle TEXT PRIMARY KEY,"
                " content TEXT NOT NULL)"
            )
            rows = database_rows(
                db.execute(
                    "SELECT value FROM meta WHERE key='config'"
                ).fetchall()
            )
            if rows and string(rows[0][0]) != self.fingerprint:
                msg = (
                    "Demo journal configuration differs; use a separate state"
                    " path."
                )
                raise ValueError(msg)
            _ = db.execute(
                "INSERT OR IGNORE INTO meta VALUES ('config', ?)",
                (self.fingerprint,),
            )

    def get(self, key: str) -> str | None:
        """Read a persisted execution field.

        Returns:
            Its value, or none when absent.

        """
        with closing(sqlite3.connect(self.path)) as db:
            rows = database_rows(
                db.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)
                ).fetchall()
            )
        return string(rows[0][0]) if rows else None

    def record(
        self, candle: str, content: str, *, pending: str | None = None
    ) -> None:
        """Commit a candle decision and optional pending order atomically."""
        with closing(sqlite3.connect(self.path)) as db, db:
            _ = db.execute(
                "INSERT INTO decisions VALUES (?, ?)", (candle, content)
            )
            _ = db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('candle', ?)", (candle,)
            )
            if pending is not None:
                _ = db.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('pending', ?)",
                    (pending,),
                )
                _ = db.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('entered', 'yes')"
                )

    def finish(self, *, owned: bool = False) -> None:
        """Clear intent only after terminal order reconciliation."""
        with closing(sqlite3.connect(self.path)) as db, db:
            if owned:
                _ = db.execute(
                    "INSERT OR REPLACE INTO meta SELECT 'owned', value FROM"
                    " meta WHERE key='pending'"
                )
            _ = db.execute("DELETE FROM meta WHERE key='pending'")
