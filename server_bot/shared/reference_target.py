# Copyright 2026 anchored-vwam-backtest contributors
"""Atomic, versioned handoff from reference accounting to the demo executor."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast

from shared.hedge_strategy import HEDGE_MODE

if TYPE_CHECKING:
    from decimal import Decimal
    from pathlib import Path

    from shared.hedge_types import HedgeSnapshot
    from shared.paper_config import PaperBotConfig


class ReferencePosition(Protocol):
    """Read-only position fields needed by the target publisher."""

    @property
    def symbol(self) -> str:
        """Market symbol."""
        ...

    @property
    def side(self) -> str | None:
        """Position direction, or flat."""
        ...

    @property
    def quantity(self) -> Decimal:
        """Position size."""
        ...

    @property
    def entry_price(self) -> float | None:
        """Position entry price."""
        ...

    @property
    def updated_at(self) -> str | None:
        """Position update timestamp."""
        ...


class ReferenceState(Protocol):
    """Represent referencestate."""

    launched_at: str
    last_processed_candle: str | None
    hedge_state: HedgeSnapshot | None


def config_fingerprint(config: PaperBotConfig) -> str:
    # Invalidate snapshots produced by the old, noncausal fill engine.
    """Handle config fingerprint.

    Returns:
        The resulting value.

    """
    return hashlib.sha256(
        ("causal-band-fills-v2\n" + config.to_json()).encode()
    ).hexdigest()


def publish_target(
    path: Path,
    config: PaperBotConfig,
    state: ReferenceState,
    *,
    position: ReferencePosition,
) -> None:
    """Handle publish target."""
    payload: dict[str, object] = {
        "version": 1,
        "published_at": datetime.now(UTC).isoformat(),
        "reference_run": state.launched_at,
        "config_fingerprint": config_fingerprint(config),
        "symbol": position.symbol,
        "timeframe": config.timeframe,
        "last_processed_candle": state.last_processed_candle,
        "side": position.side,
        "quantity": str(position.quantity),
        "entry_price": position.entry_price,
        "position_id": position.updated_at if position.side else None,
    }
    if config.leverage > 1:
        payload["version"] = 3
        payload["execution_mode"] = "isolated-paper-only"
        payload["margin_state"] = cast(
            "object", getattr(state, "margin_state", None)
        )
    elif config.strategy_mode == HEDGE_MODE:
        payload["version"] = 2
        payload["strategy_mode"] = HEDGE_MODE
        payload["hedge_plan"] = (
            state.hedge_state["plan"] if state.hedge_state else None
        )
    temporary = path.with_suffix(path.suffix + ".tmp")
    _ = temporary.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    _ = temporary.replace(path)
