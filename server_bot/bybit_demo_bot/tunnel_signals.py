# Copyright 2026 avwap-workspace-tests contributors
"""Calculate demo entry decisions from a consistent local archive snapshot."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from snapshot_tunnel.local_archive import (
    archive_tunnel_flow,
    inspect_archive,
    load_archive_oscillators,
    load_archive_snapshots,
)
from snapshot_tunnel.tunnel_plans import tunnel_plans

from bybit_demo_bot.settings import INTERVAL_SECONDS
from shared.tunnel import TunnelQuote, long_target_ceiling, regime_state

if TYPE_CHECKING:
    from shared.paper_config import PaperBotConfig


@dataclass(frozen=True, kw_only=True)
class TunnelDecision:
    """An auditable entry calculated before any exchange mutation."""

    direction: int
    state: int
    votes: int
    target_ceiling: float


def archive_decision(
    path: Path, now: datetime, *, price: float, config: PaperBotConfig
) -> TunnelDecision:
    """Read an online backup so collection cannot change an entry calculation.

    Returns:
        The same regime and quorum decision used by the historical calculator.

    """
    with TemporaryDirectory(prefix="gex-demo-") as directory:
        snapshot = Path(directory) / "archive.sqlite"
        with (
            closing(
                sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(snapshot)) as destination,
        ):
            source.backup(destination)
        coverage = inspect_archive(snapshot)
        plans = tunnel_plans(
            tuple(
                s
                for s in load_archive_snapshots(coverage)
                if s.timestamp <= now
            ),
            level_range=config.tunnel.level_range,
        )
        plan = next(
            (p for p in reversed(plans) if p.available <= now < p.expires), None
        )
        levels = plan.levels if plan else None
        flow = archive_tunnel_flow(
            load_archive_oscillators(coverage), config.tunnel
        )
        decision = flow.entry(
            TunnelQuote(
                time=now,
                price=price,
                interval=timedelta(seconds=INTERVAL_SECONDS[config.timeframe]),
            ),
            settings=config.tunnel,
            levels=levels,
        )
        return TunnelDecision(
            direction=decision.direction,
            state=int(regime_state(levels, config.tunnel)) if levels else 0,
            votes=decision.votes,
            target_ceiling=(
                long_target_ceiling(levels, config.tunnel)
                if levels
                else float("nan")
            ),
        )
