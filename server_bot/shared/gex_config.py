# Copyright 2026 anchored-vwam-backtest contributors
"""Serialize the daily GEX reference runner's independent configuration."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import time, timedelta
from typing import TYPE_CHECKING, Literal

from shared.daily_gex import (
    DailySettings,
    FlowMode,
    FlowSettings,
    FreshConfirmation,
    WallSettings,
)
from shared.strategy_data import validate_side
from shared.validation import (
    boolean,
    json_object,
    number,
    object_mapping,
    string,
)

if TYPE_CHECKING:
    from pathlib import Path


MAX_PERCENT = 100
MAX_POLL_SECONDS = 60


@dataclass(frozen=True, kw_only=True)
class GexRisk:
    """Configure paper allocation, per-side fees and protective exits."""

    capital: float = 1000
    fee_pct: float = 0.04
    minimum_order: float = 5
    stop_loss_pct: float = 2
    take_profit_pct: float = 0
    side: Literal["Long", "Short", "Both"] = "Both"

    def __post_init__(self) -> None:
        """Reject invalid account settings.

        Raises:
            ValueError: If amounts or percentage limits are invalid.

        """
        amounts = (
            self.capital,
            self.fee_pct,
            self.minimum_order,
            self.stop_loss_pct,
            self.take_profit_pct,
        )
        if (
            not all(math.isfinite(value) and value >= 0 for value in amounts)
            or self.capital <= 0
            or self.minimum_order <= 0
            or max(self.fee_pct, self.stop_loss_pct) >= MAX_PERCENT
        ):
            msg = "Invalid GEX risk settings."
            raise ValueError(msg)
        _ = validate_side(self.side)


@dataclass(frozen=True, kw_only=True)
class GexPaperConfig:
    """Keep daily strategy settings separate from VWAP and hedge modes."""

    daily: DailySettings = field(default_factory=DailySettings)
    risk: GexRisk = field(default_factory=GexRisk)
    poll_seconds: float = 5
    maximum_quote_age: timedelta = timedelta(seconds=5)

    def __post_init__(self) -> None:
        """Require bounded polling and quote freshness.

        Raises:
            ValueError: If polling or quote age is invalid.

        """
        if (
            not math.isfinite(self.poll_seconds)
            or not 1 <= self.poll_seconds <= MAX_POLL_SECONDS
            or not timedelta(0)
            < self.maximum_quote_age
            <= timedelta(seconds=30)
        ):
            msg = "GEX polling must be 1-60 seconds and quote age 0-30 seconds."
            raise ValueError(msg)

    def to_json(self) -> str:
        """Export exact daily settings with explicit time units.

        Returns:
            A configuration accepted by the GEX reference runner.

        """
        return (
            json.dumps(
                {
                    "strategy": "daily_gex",
                    "daily": asdict(self.daily),
                    "risk": asdict(self.risk),
                    "poll_seconds": self.poll_seconds,
                    "maximum_quote_age": self.maximum_quote_age,
                },
                default=_encode_time,
                indent=2,
            )
            + "\n"
        )

    @classmethod
    def load(cls, path: Path) -> GexPaperConfig:
        """Validate external JSON before constructing domain settings.

        Returns:
            A validated daily GEX configuration.

        Raises:
            ValueError: If the strategy or side is unsupported.

        """
        values = json_object(path.read_text(encoding="utf-8"))
        if values.get("strategy") != "daily_gex":
            msg = "Expected a daily_gex reference configuration."
            raise ValueError(msg)
        risk = object_mapping(values["risk"])
        side = validate_side(string(risk["side"]))
        return cls(
            daily=_daily_settings(object_mapping(values["daily"])),
            risk=GexRisk(
                capital=number(risk["capital"]),
                fee_pct=number(risk["fee_pct"]),
                minimum_order=number(risk["minimum_order"]),
                stop_loss_pct=number(risk["stop_loss_pct"]),
                take_profit_pct=number(risk["take_profit_pct"]),
                side=side,
            ),
            poll_seconds=number(values["poll_seconds"]),
            maximum_quote_age=timedelta(
                seconds=number(values["maximum_quote_age"])
            ),
        )


def _encode_time(value: object) -> float | str:
    """Serialize domain durations as seconds and UTC clocks as ISO text.

    Returns:
        A JSON scalar.

    Raises:
        TypeError: If a value has no supported representation.

    """
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, time):
        return value.isoformat()
    msg = "Unsupported GEX configuration value."
    raise TypeError(msg)


def _daily_settings(values: dict[str, object]) -> DailySettings:
    """Parse a complete daily strategy configuration.

    Returns:
        Validated wall, flow and fresh-chain settings.

    """
    walls = object_mapping(values["walls"])
    raw_fresh = values["fresh_confirmation"]
    fresh = object_mapping(raw_fresh) if raw_fresh is not None else None
    return DailySettings(
        refresh=time.fromisoformat(string(values["refresh"])),
        intrabar=boolean(values["intrabar"]),
        gamma_flip_direction=boolean(values.get("gamma_flip_direction", False)),
        snapshot_age=timedelta(seconds=number(values["snapshot_age"])),
        expiry_horizon=timedelta(seconds=number(values["expiry_horizon"])),
        fresh_confirmation=(
            FreshConfirmation(
                maximum_age=timedelta(seconds=number(fresh["maximum_age"])),
                wall_drift=number(fresh["wall_drift"]),
            )
            if fresh is not None
            else None
        ),
        walls=WallSettings(
            proximity=number(walls["proximity"]),
            momentum_offset=number(walls["momentum_offset"]),
            neutral_gex=number(walls["neutral_gex"]),
            neutral_fraction=number(walls.get("neutral_fraction", 0.0)),
            stop_offset=(
                number(walls["stop_offset"])
                if walls["stop_offset"] is not None
                else None
            ),
        ),
        flow=_flow_settings(object_mapping(values["flow"])),
    )


def _flow_settings(values: dict[str, object]) -> FlowSettings:
    """Parse independent confirmation filters with explicit seconds.

    Returns:
        Validated flow settings.

    """
    return FlowSettings(
        mode=FlowMode(string(values["mode"])),
        threshold=number(values["threshold"]),
        oi_window=timedelta(seconds=number(values["oi_window"])),
        oi_age=timedelta(seconds=number(values["oi_age"])),
        oi_delay=timedelta(seconds=number(values["oi_delay"])),
        oi_tolerance=number(values["oi_tolerance"]),
        volume_recent=timedelta(seconds=number(values["volume_recent"])),
        volume_baseline=timedelta(seconds=number(values["volume_baseline"])),
        volume_tolerance=number(values["volume_tolerance"]),
        book_age=timedelta(seconds=number(values["book_age"])),
        use_oi=boolean(values["use_oi"]),
        use_volume=boolean(values["use_volume"]),
    )
