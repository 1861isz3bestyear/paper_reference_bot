# Copyright 2026 anchored-vwam-backtest contributors
"""Select saved daily snapshots nearest 08:15 UTC without backdating them."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from snapshot_tunnel.daily_gex import DailySettings
from snapshot_tunnel.daily_plans import plan_from_snapshot
from snapshot_tunnel.gamma_search import search_flips
from snapshot_tunnel.option_levels import DEFAULT_LEVEL_RANGE, OptionLevelRange
from snapshot_tunnel.options_model import strike_exposures
from snapshot_tunnel.tunnel import NegativeGex, TunnelLevels, TunnelRegime

if TYPE_CHECKING:
    from snapshot_tunnel.options_model import Snapshot


@dataclass(frozen=True, kw_only=True)
class TunnelPlan:
    """One saved chain and its non-overlapping 24-hour execution window."""

    available: datetime
    expires: datetime
    levels: TunnelRegime


def selected_snapshots(snapshots: tuple[Snapshot, ...]) -> tuple[Snapshot, ...]:
    """Choose one BTC observation per UTC date, with earlier ties preferred.

    Returns:
        The closest saved observation to 08:15, ordered chronologically.

    """
    days: dict[date, list[Snapshot]] = {}
    for snapshot in snapshots:
        if snapshot.asset == "BTC":
            days.setdefault(
                snapshot.timestamp.astimezone(UTC).date(), []
            ).append(snapshot)
    return tuple(
        min(
            days[day],
            key=lambda item: (
                abs(item.timestamp - datetime.combine(day, time(8, 15), UTC)),
                item.timestamp,
            ),
        )
        for day in sorted(days)
    )


def tunnel_plans(
    snapshots: tuple[Snapshot, ...],
    *,
    level_range: OptionLevelRange = DEFAULT_LEVEL_RANGE,
) -> tuple[TunnelPlan, ...]:
    """Freeze levels from selected chains, starting at actual receipt time.

    Returns:
        Plans ending after 24 hours, at contract expiry or the next plan.

    """
    selected = selected_snapshots(snapshots)
    plans: list[TunnelPlan] = []
    for index, snapshot in enumerate(selected):
        plan = _snapshot_plan(snapshot, level_range=level_range)
        if plan is None:
            continue
        if index + 1 < len(selected):
            plan = replace(
                plan,
                expires=min(plan.expires, selected[index + 1].timestamp),
            )
        plans.append(plan)
    return tuple(plans)


def _snapshot_plan(
    snapshot: Snapshot, *, level_range: OptionLevelRange
) -> TunnelPlan | None:
    """Build a wall-free negative regime or a positive option-level plan.

    Returns:
        A receipt-stamped plan bounded by actual option expiry.

    """
    settings = DailySettings()
    selected = replace(
        snapshot,
        options=tuple(
            option
            for option in snapshot.options
            if option.exchange.lower() == "deribit"
            and snapshot.timestamp
            < option.expiry
            <= snapshot.timestamp + settings.expiry_horizon
        ),
    )
    if not selected.options:
        return None
    gex = math.fsum(level.net for level in strike_exposures(selected))
    if gex < 0:
        return TunnelPlan(
            available=selected.timestamp,
            expires=min(
                selected.timestamp + timedelta(hours=24),
                *(option.expiry for option in selected.options),
            ),
            levels=NegativeGex(gex=gex, flip=search_flips(selected).nearest),
        )
    plan = plan_from_snapshot(selected, settings, level_range=level_range)
    if plan is None:
        return None
    return TunnelPlan(
        available=plan.available,
        expires=min(plan.expires, selected.timestamp + timedelta(hours=24)),
        levels=TunnelLevels(
            gex=plan.gex,
            lower=plan.put_wall,
            upper=plan.call_wall,
            flip=plan.gamma_flip,
        ),
    )
