# Copyright 2026 avwap-workspace-tests contributors
"""Calculate demo entry decisions from a consistent local archive snapshot."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import requests
from snapshot_tunnel.bybit_retest import load_retest_votes
from snapshot_tunnel.local_archive import (
    archive_tunnel_flow,
    inspect_archive,
    load_archive_oscillators,
    load_archive_snapshots,
)
from snapshot_tunnel.tunnel_flow import TunnelFlow
from snapshot_tunnel.tunnel_plans import tunnel_plans

from bybit_demo_bot.settings import INTERVAL_SECONDS
from shared.tunnel import TunnelQuote, long_target_ceiling, regime_state

if TYPE_CHECKING:
    from shared.paper_config import PaperBotConfig
    from shared.tunnel import TunnelRegime, TunnelSettings


@dataclass(frozen=True, kw_only=True)
class TunnelDecision:
    """An auditable entry calculated before any exchange mutation."""

    direction: int
    state: int
    votes: int
    target_ceiling: float


@dataclass(frozen=True, kw_only=True)
class ArchivedEvidence:
    """Active option levels and oscillator streams from one archive version."""

    levels: TunnelRegime | None = None
    flow: TunnelFlow = field(default_factory=TunnelFlow)


def _archive_evidence(
    path: Path, now: datetime, *, settings: TunnelSettings
) -> ArchivedEvidence:
    if settings.retest is not None and not any((
        settings.use_gex,
        settings.use_book,
        settings.use_margin,
        settings.use_oi,
    )):
        return ArchivedEvidence()
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
            level_range=settings.level_range,
        )
        plan = next(
            (p for p in reversed(plans) if p.available <= now < p.expires), None
        )
        return ArchivedEvidence(
            levels=plan.levels if plan else None,
            flow=archive_tunnel_flow(
                load_archive_oscillators(coverage), settings
            ),
        )


def archive_decision(
    path: Path, now: datetime, *, price: float, config: PaperBotConfig
) -> TunnelDecision:
    """Combine consistent archive evidence with public Retest spot klines.

    Returns:
        The same regime and quorum decision used by the historical calculator.

    """
    settings = config.tunnel
    evidence = _archive_evidence(path, now, settings=settings)
    flow, levels = evidence.flow, evidence.levels
    if settings.retest is not None:
        with requests.Session() as session:
            retest = load_retest_votes(
                session, now, now, settings=settings.retest
            )
        flow = replace(flow, retest=retest)
    decision = flow.entry(
        TunnelQuote(
            time=now,
            price=price,
            interval=timedelta(seconds=INTERVAL_SECONDS[config.timeframe]),
        ),
        settings=settings,
        levels=levels,
    )
    return TunnelDecision(
        direction=decision.direction,
        state=int(regime_state(levels, settings)) if levels else 0,
        votes=decision.votes,
        target_ceiling=(
            long_target_ceiling(levels, settings) if levels else float("nan")
        ),
    )
