# Copyright 2026 avwap-workspace-tests contributors
"""Serialize validated option snapshots without persistence side effects."""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from snapshot_tunnel.options_model import Snapshot

COLUMNS = (
    "timestamp",
    "asset",
    "spot",
    "exchange",
    "expiry",
    "strike",
    "side",
    "open_interest",
    "multiplier",
    "iv",
)


def snapshots_csv(snapshots: tuple[Snapshot, ...]) -> str:
    """Serialize validated snapshots at the CSV boundary.

    Returns:
        Importable options CSV.

    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(COLUMNS)
    for snapshot in snapshots:
        writer.writerows(
            (
                snapshot.timestamp.isoformat(),
                snapshot.asset,
                snapshot.spot,
                option.exchange,
                option.expiry.isoformat(),
                option.strike,
                option.side,
                option.open_interest,
                option.multiplier,
                option.iv,
            )
            for option in snapshot.options
        )
    return output.getvalue()
