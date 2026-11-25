# Copyright 2026 avwap-workspace-tests contributors
"""Execute only Snapshot tunnel decisions against a Bybit demo account."""

from __future__ import annotations

import json
import logging
import math
import signal
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import sleep
from typing import TYPE_CHECKING

import requests

from bybit_demo_bot.client import BybitDemoError
from bybit_demo_bot.settings import INTERVAL_SECONDS
from bybit_demo_bot.tunnel_journal import TunnelJournal, pending_entry
from bybit_demo_bot.tunnel_orders import (
    positive_decimal,
    price_tick,
    protection,
    round_protection,
    sized_entry,
)
from bybit_demo_bot.tunnel_signals import archive_decision
from shared.tunnel_config import TUNNEL_MODE
from shared.validation import numeric_string, string

if TYPE_CHECKING:
    from pathlib import Path

    from bybit_demo_bot.client import BybitDemoClient
    from shared.paper_config import PaperBotConfig


@dataclass(kw_only=True)
class TunnelDemoBot:
    """Execute durable Snapshot tunnel decisions independently."""

    config: PaperBotConfig
    client: BybitDemoClient
    journal: TunnelJournal
    archive: Path
    running: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        """Reject alternative strategies before touching exchange state.

        Raises:
            ValueError: If configuration is not the exported tunnel strategy.

        """
        self.config.validate()
        if self.config.strategy_mode != TUNNEL_MODE:
            msg = "Bybit demo supports only Snapshot tunnel configs."
            raise ValueError(msg)
        self.journal.prepare()

    def reconcile_once(self, now: datetime | None = None) -> str:
        """Reconcile persisted intent before considering a current candle.

        Returns:
            A status description suitable for the service log.

        Raises:
            RuntimeError: If an unknown position or order prevents safe entry.

        """
        pending = self.journal.get("pending")
        if pending is not None:
            return self.reconcile_entry(pending)
        position = self.client.position("BTCUSDT")
        if position is not None:
            owned = self.journal.get("owned")
            if owned is None:
                msg = "Existing BTC position has no tunnel journal ownership."
                raise RuntimeError(msg)
            return self.protect_entry(owned)
        if self.client.has_open_orders("BTCUSDT"):
            msg = "Existing BTC orders prevent a new tunnel entry."
            raise RuntimeError(msg)
        if not self.config.allow_immediate_reentry and self.journal.get(
            "entered"
        ):
            return "reentry disabled"
        return self.process_quote(now or datetime.now(UTC))

    def process_quote(self, now: datetime) -> str:
        """Evaluate one current candle and persist intent before submitting it.

        Returns:
            The decision or order submission status.

        """
        interval = INTERVAL_SECONDS[self.config.timeframe]
        candle = datetime.fromtimestamp(
            int(now.timestamp()) // interval * interval, UTC
        ).isoformat()
        if self.journal.get("candle") == candle:
            return "candle already processed"
        price = self.client.last_price("BTCUSDT")
        decision = archive_decision(
            self.archive, now, price=float(price), config=self.config
        )
        side = "Long" if decision.direction == 1 else "Short"
        if not decision.direction or self.config.open_position_side not in {
            "Both",
            side,
        }:
            self.journal.record(
                candle,
                json.dumps({
                    "direction": decision.direction,
                    "state": decision.state,
                    "votes": decision.votes,
                    "target_ceiling": (
                        decision.target_ceiling
                        if math.isfinite(decision.target_ceiling)
                        else None
                    ),
                }),
            )
            return "waiting for calculatable GEX and oscillator confirmations"
        intent = sized_entry(
            self.client,
            decision,
            config=self.config,
            candle=candle,
            fingerprint=self.journal.fingerprint,
        )
        self.client.tunnel_leverage()
        content = {
            "link": intent.link,
            "side": intent.side,
            "quantity": str(intent.quantity),
            "stop": str(intent.stop),
            "target": str(intent.target),
            "ceiling": (
                str(intent.ceiling) if intent.ceiling is not None else None
            ),
        }
        self.journal.record(
            candle,
            json.dumps({
                "direction": decision.direction,
                "state": decision.state,
                "votes": decision.votes,
                "target_ceiling": (
                    decision.target_ceiling
                    if math.isfinite(decision.target_ceiling)
                    else None
                ),
            }),
            pending=json.dumps(content),
        )
        # Never retry this POST. A lost response may still mean a filled order.
        _ = self.client.tunnel_market_order(intent)
        return "tunnel entry submitted with exchange SL/TP"

    def reconcile_entry(self, content: str) -> str:
        """Resolve an accepted or ambiguous order without submitting it again.

        Returns:
            A reconciled order or protection status.

        Raises:
            RuntimeError: If the exchange cannot establish the pending outcome.

        """
        intent = pending_entry(content)
        order = self.client.order_by_link("BTCUSDT", intent.link)
        if order is None:
            msg = (
                "Pending tunnel order outcome is unknown; no new order will be"
                " sent."
            )
            raise RuntimeError(msg)
        status = string(order["orderStatus"])
        position = self.client.position("BTCUSDT")
        if position is not None:
            return self.protect_entry(content)
        if status == "Filled" and self.journal.get("owned") is None:
            return "waiting for the filled tunnel position to become visible"
        if status in {
            "Filled",
            "Cancelled",
            "Rejected",
            "PartiallyFilledCanceled",
            "Deactivated",
        }:
            self.journal.finish()
            return "tunnel order terminal; exchange position is flat"
        return "waiting for tunnel entry fill"

    def protect_entry(self, content: str) -> str:
        """Protect a confirmed fill using percentage risk and its frozen cap.

        Returns:
            Confirmation that exchange protection is installed.

        Raises:
            RuntimeError: If the position differs from the persisted entry.

        """
        intent = pending_entry(content)
        position = self.client.position("BTCUSDT")
        if position is None:
            return "position changed; awaiting reconciliation"
        if (
            position["side"] != intent.side
            or position.get("positionIdx", 0) != 0
            or (
                "leverage" in position
                and numeric_string(position["leverage"]) != 1
            )
            or positive_decimal(position["size"]) > intent.quantity
        ):
            msg = "Exchange position does not match the tunnel entry."
            raise RuntimeError(msg)
        entry = positive_decimal(position["avgPrice"])
        stop, target = round_protection(
            protection(entry, intent, config=self.config),
            price_tick(self.client.instruments("BTCUSDT")),
            side=intent.side,
        )
        if not (
            stop < entry < target
            if intent.side == "Buy"
            else target < entry < stop
        ):
            msg = "Fill protection collapses on the exchange grid."
            raise RuntimeError(msg)
        self.client.set_protection("BTCUSDT", stop, target)
        order = self.client.order_by_link("BTCUSDT", intent.link)
        if order is not None and order.get("orderStatus") == "Filled":
            self.journal.finish(owned=True)
        return "tunnel fill protected"

    def stop(self, *_: object) -> None:
        """Stop polling while leaving exchange protection active."""
        self.running = False

    def run(self, poll_seconds: int) -> None:
        """Run reconciliation until shutdown; never retry an uncertain POST."""
        _ = signal.signal(signal.SIGTERM, self.stop)
        _ = signal.signal(signal.SIGINT, self.stop)
        while self.running:
            try:
                result = self.reconcile_once()
            except (
                BybitDemoError,
                requests.RequestException,
                RuntimeError,
                ValueError,
                TypeError,
                OSError,
                sqlite3.Error,
                KeyError,
            ) as exc:
                result = f"tunnel reconciliation paused: {exc}"
            logging.getLogger(__name__).info(
                "%s %s", datetime.now(UTC).isoformat(), result
            )
            for _ in range(poll_seconds):
                if not self.running:
                    break
                sleep(1)
