# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of bybit demo bot cli."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from bybit_demo_bot.state_types import (
    HedgeCycle,
    ReplicaOrder,
    parse_hedge_cycle,
    parse_replica_order,
)
from shared.hedge_validation import optional_str
from shared.validation import boolean, json_object, string

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "bybit_demo_state.json"


@dataclass
class DemoState:
    """Represent demo state."""

    launched_at: str
    last_processed_candle: str | None = None
    hedge_state: HedgeCycle | None = None
    pending_protection_side: str | None = None
    pending_take_profit: str | None = None
    halted_reason: str | None = None
    consumed_signal_side: str | None = None
    observed_position_side: str | None = None
    pending_strategy_close: bool = False
    replica_initialized: bool = False
    replica_position_id: str | None = None
    replica_order: ReplicaOrder | None = None

    @classmethod
    def load_or_create(cls, *, resume: bool) -> DemoState:
        """Load or create.

        Returns:
            The load or create result.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        if resume and STATE_FILE.is_file():
            try:
                return cls.read_state(STATE_FILE)
            except (OSError, TypeError, json.JSONDecodeError) as exc:
                message = f"Cannot read {STATE_FILE.name}: {exc}"
                raise RuntimeError(message) from None
        state = cls(datetime.now(UTC).isoformat())
        state.save()
        return state

    @classmethod
    def read_state(cls, path: Path) -> Self:
        """Validate a saved execution state before constructing an executor.

        Returns:
            The restored execution state.

        """
        data = json_object(path.read_text(encoding="utf-8"))
        consumed = optional_str(data.get("consumed_signal_side"))
        if "consumed_signal_side" not in data and data.get(
            "last_processed_candle"
        ):
            consumed = "legacy"
        hedge = data.get("hedge_state")
        order = data.get("replica_order")
        return cls(
            launched_at=string(data["launched_at"]),
            last_processed_candle=optional_str(
                data.get("last_processed_candle")
            ),
            hedge_state=parse_hedge_cycle(hedge) if hedge is not None else None,
            pending_protection_side=optional_str(
                data.get("pending_protection_side")
            ),
            pending_take_profit=optional_str(data.get("pending_take_profit")),
            halted_reason=optional_str(data.get("halted_reason")),
            consumed_signal_side=consumed,
            observed_position_side=optional_str(
                data.get("observed_position_side")
            ),
            pending_strategy_close=boolean(
                data.get("pending_strategy_close", False)
            ),
            replica_initialized=boolean(data.get("replica_initialized", False)),
            replica_position_id=optional_str(data.get("replica_position_id")),
            replica_order=(
                parse_replica_order(order) if order is not None else None
            ),
        )

    def save(self) -> None:
        """Save."""
        temporary = STATE_FILE.with_suffix(".json.tmp")
        _ = temporary.write_text(
            json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8"
        )
        _ = Path(temporary).replace(STATE_FILE)
