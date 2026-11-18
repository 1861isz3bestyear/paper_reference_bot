# Copyright 2026 anchored-vwam-backtest contributors
"""Publish immutable daily option levels at their actual availability time."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from snapshot_tunnel.gamma_search import search_flips
from snapshot_tunnel.option_levels import DEFAULT_LEVEL_RANGE, OptionLevelRange
from snapshot_tunnel.options_model import (
    Snapshot,
    strike_exposures,
    wall_tunnel,
)

if TYPE_CHECKING:
    from snapshot_tunnel.daily_gex import DailySettings, WallSettings


@dataclass(frozen=True, kw_only=True)
class DailyPlan:
    """Record the observation, expiry and levels used for one trading day."""

    available: datetime
    expires: datetime
    gex: float
    call_wall: float
    put_wall: float
    call_gex: float
    put_gex: float
    gamma_flip: float

    def is_neutral(self, settings: WallSettings) -> bool:
        """Compare signed exposure with absolute and gross-relative limits.

        Returns:
            Whether the plan lacks a sufficiently strong signed regime.

        """
        threshold = max(
            settings.neutral_gex,
            settings.neutral_fraction * (self.call_gex - self.put_gex),
        )
        return abs(self.gex) <= threshold

    def agrees(self, fresh: DailyPlan, *, tolerance: float) -> bool:
        """Check the original walls without moving pending entry levels.

        Returns:
            Whether both walls and the original regime survive.

        """
        return (
            self.gex * fresh.gex > 0
            and abs(fresh.call_wall / self.call_wall - 1) <= tolerance
            and abs(fresh.put_wall / self.put_wall - 1) <= tolerance
        )


def daily_boundary(now: datetime, settings: DailySettings) -> datetime:
    """Locate the most recent scheduled UTC refresh.

    Returns:
        The inclusive start of the current daily planning period.

    """
    utc = now.astimezone(UTC)
    boundary = datetime.combine(utc.date(), settings.refresh, UTC)
    return boundary if boundary <= utc else boundary - timedelta(days=1)


def plan_from_snapshot(
    snapshot: Snapshot,
    settings: DailySettings,
    *,
    include_flip: bool = True,
    level_range: OptionLevelRange = DEFAULT_LEVEL_RANGE,
) -> DailyPlan | None:
    """Value the selected live expiries using only the observed chain.

    Returns:
        A usable two-wall plan, or no plan when the selected scope is empty.

    """
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
    levels = strike_exposures(selected)
    tunnel = wall_tunnel(levels, spot=selected.spot, level_range=level_range)
    if tunnel is None or tunnel.put >= tunnel.call:
        return None
    flip = search_flips(selected).nearest if include_flip else None
    return DailyPlan(
        available=snapshot.timestamp,
        expires=min(
            snapshot.timestamp + settings.snapshot_age,
            *(option.expiry for option in selected.options),
        ),
        gex=math.fsum(level.net for level in levels),
        call_wall=tunnel.call,
        put_wall=tunnel.put,
        call_gex=math.fsum(level.call for level in levels),
        put_gex=math.fsum(level.put for level in levels),
        gamma_flip=flip if flip is not None else math.nan,
    )


def daily_plans(
    snapshots: tuple[Snapshot, ...], settings: DailySettings
) -> tuple[DailyPlan, ...]:
    """Publish the first usable observation received after each daily refresh.

    Returns:
        Chronological plans, each expiring no later than the next refresh.

    """
    plans: list[DailyPlan] = []
    published: set[datetime] = set()
    for snapshot in sorted(snapshots, key=lambda item: item.timestamp):
        if snapshot.asset != "BTC":
            continue
        boundary = daily_boundary(snapshot.timestamp, settings)
        if boundary in published:
            continue
        plan = plan_from_snapshot(snapshot, settings)
        if plan is not None:
            plans.append(
                replace(
                    plan,
                    expires=min(plan.expires, boundary + timedelta(days=1)),
                )
            )
            published.add(boundary)
    return tuple(plans)


@dataclass(frozen=True, kw_only=True)
class OptionConfirmation:
    """Retain invalid observations so a vanished wall cancels confirmation."""

    available: datetime
    plan: DailyPlan | None
