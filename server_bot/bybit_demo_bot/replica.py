# Copyright 2026 anchored-vwam-backtest contributors
"""Execute the reference position without independent trading decisions."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, ClassVar, Literal, override

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from bybit_demo_bot.client import BybitDemoClient
    from bybit_demo_bot.state import DemoState
    from bybit_demo_bot.state_types import ReplicaOrder
    from reference_bot.config import PaperBotConfig
from uuid import uuid4

from bybit_demo_bot.client import BybitDemoError
from bybit_demo_bot.executor import BybitDemoBot
from bybit_demo_bot.settings import INTERVAL_SECONDS, ROOT
from shared.hedge_validation import optional_str
from shared.reference_target import config_fingerprint
from shared.validation import json_object, object_mapping, string

MIN_HEARTBEAT_AGE_SECONDS = -5


TERMINAL = {
    "Filled",
    "Cancelled",
    "Rejected",
    "PartiallyFilledCanceled",
    "Deactivated",
}


def timestamp(value: str) -> datetime:
    """Timestamp.

    Returns:
        The timezone-aware strategy timestamp.

    Raises:
        ValueError: If the requested operation cannot safely proceed.

    """
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        message = "timestamp must include a timezone"
        raise ValueError(message)
    return result


def amount(value: object) -> Decimal:
    """Amount.

    Returns:
        The calculated decimal amount.

    Raises:
        ValueError: If the requested operation cannot safely proceed.

    """
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        message = "quantity must be finite and nonnegative"
        raise ValueError(message)
    return result


@dataclass(frozen=True, kw_only=True)
class ReferenceTarget:
    """The validated exchange position requested by a reference snapshot."""

    reference_run: str
    last_processed_candle: str | None
    side: Literal["Buy", "Sell"] | None
    quantity: Decimal
    id: str | None


@dataclass(frozen=True, kw_only=True)
class ReplicaAction:
    """An exact exchange transition and the position expected after its fill."""

    side: str
    quantity: Decimal
    expected_side: str | None
    expected_quantity: Decimal
    reference_id: str | None
    reduce_only: bool = False


class ReferenceReplicaBot(BybitDemoBot):
    """Represent reference replica bot."""

    EXECUTOR_NAME: ClassVar[str] = "Bybit DEMO reference replica"
    MAX_RETRY_SECONDS: ClassVar[int] = 10
    HEARTBEAT_TIMEOUT: ClassVar[int] = 120
    ORDER_TIMEOUT: ClassVar[int] = 60

    def __init__(
        self,
        config: PaperBotConfig,
        client: BybitDemoClient,
        state: DemoState,
        *,
        target_path: Path | None = None,
    ) -> None:
        """Initialize the instance."""
        super().__init__(config, client, state)
        self.target_path: Path = target_path or ROOT / "reference_target.json"
        self.fingerprint: str = config_fingerprint(config)
        self._cleared_position: tuple[str, str, str] | None = None
        self._limits: dict[str, object] | None = None
        self._missing_since: datetime | None = None

    def load_reference_target(
        self, now: datetime | None = None
    ) -> ReferenceTarget:
        """Load and validate the published reference target.

        Returns:
            The load reference target result.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        try:
            return self._validate_target(
                json_object(self.target_path.read_text(encoding="utf-8")),
                now or datetime.now(UTC),
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            InvalidOperation,
        ) as exc:
            message = f"reference target unavailable: {exc}"
            raise RuntimeError(message) from None

    def _validate_target(
        self, target: dict[str, object], now: datetime
    ) -> ReferenceTarget:
        if (
            target["version"] != 1
            or target["config_fingerprint"] != self.fingerprint
        ):
            message = "reference configuration/version does not match demo"
            raise ValueError(message)
        if (
            target["symbol"] != self.symbol
            or target["timeframe"] != self.config.timeframe
        ):
            message = "reference symbol/timeframe does not match demo"
            raise ValueError(message)
        heartbeat_age = (
            now - timestamp(string(target["published_at"]))
        ).total_seconds()
        if (
            not MIN_HEARTBEAT_AGE_SECONDS
            <= heartbeat_age
            <= self.HEARTBEAT_TIMEOUT
        ):
            message = (
                "reference heartbeat is stale or in the future:"
                f" age={heartbeat_age:.3f}s;"
                f" published_at={target['published_at']};"
                f" checked_at={now.isoformat()}"
            )
            raise ValueError(message)
        self._validate_cursor(target, now)
        side = target["side"]
        quantity = amount(target["quantity"])
        if side not in {None, "Long", "Short"}:
            message = "invalid reference side"
            raise ValueError(message)
        if side is None:
            if quantity != 0 or target["position_id"] is not None:
                message = "invalid flat reference target"
                raise ValueError(message)
        elif (
            quantity <= 0
            or not target["position_id"]
            or amount(target["entry_price"]) <= 0
        ):
            message = "invalid reference position"
            raise ValueError(message)
        else:
            _ = timestamp(string(target["position_id"]))
        return ReferenceTarget(
            reference_run=string(target["reference_run"]),
            last_processed_candle=optional_str(target["last_processed_candle"]),
            side=(
                "Buy" if side == "Long" else "Sell" if side == "Short" else None
            ),
            quantity=quantity,
            id=(
                f'{target["reference_run"]}/{target["position_id"]}'
                if side
                else None
            ),
        )

    def _validate_cursor(
        self, target: dict[str, object], now: datetime
    ) -> None:
        run = timestamp(string(target["reference_run"]))
        interval = INTERVAL_SECONDS[self.config.timeframe]
        cursor = target["last_processed_candle"]
        if cursor is None:
            if (
                target["side"] is not None
                or not 0 <= (now - run).total_seconds() <= 3 * interval + 30
            ):
                message = "reference has no current processed candle"
                raise ValueError(message)
            return
        candle = timestamp(string(cursor))
        if (
            candle < run
            or not interval
            <= (now - candle).total_seconds()
            <= 3 * interval + 30
        ):
            message = "reference processed candle is stale or invalid"
            raise ValueError(message)

    def _halt(self, reason: str) -> None:
        self.state.halted_reason = reason
        self.state.save()

    def _flatten_halted(self, position: Mapping[str, object] | None) -> str:
        # Keep reconciling even after halting: a delayed entry must not escape
        # cleanup, and a failed/partial emergency close must be retried.
        self.state.observed_position_side = (
            str(position["side"]) if position else None
        )
        if position is None:
            self.state.replica_position_id = None
        self.state.save()
        pending = self.state.replica_order
        if pending:
            try:
                order = self.client.order_by_link(self.symbol, pending["link"])
                if not order or order.get("orderStatus") not in TERMINAL:
                    self.client.cancel_by_link(self.symbol, pending["link"])
            except BybitDemoError as exc:
                # Still attempt to close an observed position if cancellation or
                # order history is unavailable. Keep the intent for the next
                # pass.
                _ = sys.stdout.write(
                    str(f"replica cancellation error; will retry: {exc}") + "\n"
                )
                _ = sys.stdout.flush()
        if position:
            _ = self.client.market_order(
                self.symbol,
                "Sell" if position["side"] == "Buy" else "Buy",
                amount(position["size"]),
                reduce_only=True,
            )
            return (
                f"trading halted: {self.state.halted_reason}; emergency close"
                " submitted"
            )
        return (
            f"trading halted: {self.state.halted_reason}; exchange position"
            " flat"
        )

    def _validate_entry(self, quantity: Decimal) -> None:
        if self._limits is None:
            self._limits = object_mapping(
                self.client.instruments(self.symbol)["lotSizeFilter"]
            )
        step = amount(self._limits["qtyStep"])
        minimum = amount(self._limits["minOrderQty"])
        maximum = amount(
            self._limits.get(
                "maxMktOrderQty", self._limits.get("maxOrderQty", quantity)
            )
        )
        if (
            step <= 0
            or quantity % step != 0
            or not minimum <= quantity <= maximum
        ):
            message = (
                "reference quantity cannot be executed exactly under exchange"
                " limits"
            )
            raise RuntimeError(message)
        price = amount(self.client.last_price(self.symbol))
        notional = quantity * price
        if price <= 0 or notional < amount(
            self._limits.get("minNotionalValue", 0)
        ):
            message = "reference quantity is below exchange minimum notional"
            raise RuntimeError(message)
        if notional > amount(self.client.available_usdt()):
            message = "demo balance cannot fund the exact reference quantity"
            raise RuntimeError(message)

    def _submit(self, action: ReplicaAction, now: datetime) -> str:
        if not action.reduce_only:
            self._validate_entry(action.quantity)
        link = "ref-" + uuid4().hex  # Bybit's maximum orderLinkId length is 36.
        self.state.replica_order = {
            "link": link,
            "submitted_at": now.isoformat(),
            "expected_side": action.expected_side,
            "expected_quantity": str(action.expected_quantity),
            "reference_id": action.reference_id,
        }
        # A timeout or crash must never cause a second submission.
        self.state.save()
        _ = self.client.market_order(
            self.symbol,
            action.side,
            action.quantity,
            reduce_only=action.reduce_only,
            order_link_id=link,
        )
        return (
            "reference replica submitted"
            f' {"reduce" if action.reduce_only else "entry"} {action.side}'
            f" {action.quantity};"
            f" order={link}"
        )

    def _pending(
        self,
        position: Mapping[str, object] | None,
        pending: ReplicaOrder,
        *,
        now: datetime,
    ) -> str | None:
        expired = (
            now - timestamp(string(pending.get("submitted_at")))
        ).total_seconds() > self.ORDER_TIMEOUT
        try:
            order = self.client.order_by_link(self.symbol, pending["link"])
        except BybitDemoError:
            if not expired:
                raise
            self._halt(
                "replica order/fill reconciliation timed out; operator review"
                " required"
            )
            return self._flatten_halted(position)
        actual_side = string(position["side"]) if position else None
        actual_qty = amount(position["size"]) if position else Decimal(0)
        if order and order.get("orderStatus") in TERMINAL:
            if order["orderStatus"] != "Filled":
                self._halt(
                    f'replica order {pending["link"]} ended'
                    f' {order["orderStatus"]}; operator review required'
                )
                return self._flatten_halted(position)
            if actual_side == optional_str(
                pending.get("expected_side")
            ) and actual_qty == amount(
                string(pending.get("expected_quantity"))
            ):
                self.state.replica_position_id = optional_str(
                    pending.get("reference_id")
                )
                self.state.replica_order = None
                self.state.observed_position_side = actual_side
                self.state.save()
                self._cleared_position = None
                return "reference replica fill confirmed"
        if expired:
            self._halt(
                "replica order/fill reconciliation timed out; operator review"
                " required"
            )
            return self._flatten_halted(position)
        return "reference replica awaiting order/fill reconciliation"

    @override
    def reconcile_once(self, now: datetime | None = None) -> str | None:
        """Reconcile once.

        Returns:
            The action description, or None when no action is needed.

        """
        supplied_now = now
        position = self.client.position(self.symbol)
        now = supplied_now or datetime.now(UTC)
        if self.state.halted_reason:
            return self._reconcile_halted_reference(position, supplied_now)
        try:
            return self._reconcile_current_reference(
                position, now, supplied_now=supplied_now
            )
        except (
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            InvalidOperation,
            BybitDemoError,
        ) as exc:
            # Order creation errors are ambiguous: preserve their intent and
            # reconcile by link ID before deciding to halt or submit anything
            # else.
            if (
                isinstance(exc, BybitDemoError)
                and self.state.replica_order
                and not self.state.halted_reason
            ):
                return (
                    "replica order error; intent retained for reconciliation:"
                    f" {exc}"
                )
            self._halt(str(exc))
            return self._flatten_halted(position)

    def _reconcile_halted_reference(
        self, position: Mapping[str, object] | None, now: datetime | None
    ) -> str:
        """Flatten exposure and report producer health while keeping the halt.

        Returns:
            The reconcile halted reference result.

        """
        action = self._flatten_halted(position)
        if self.state.halted_reason and self.state.halted_reason.startswith(
            "reference target unavailable:"
        ):
            # This is a diagnostic read only: a healthy producer cannot
            # clear
            # uncertain exchange orders or the persisted safety halt.
            try:
                _ = self.load_reference_target(now)
            except RuntimeError:
                return (
                    action + "; current reference unavailable; halt is latched"
                )
            return (
                action
                + "; current reference healthy; checked recovery required"
                " (bybit_demo_bot.recover)"
            )
        return action

    def _reconcile_current_reference(
        self,
        position: Mapping[str, object] | None,
        now: datetime,
        *,
        supplied_now: datetime | None,
    ) -> str | None:
        """Validate the producer before reconciling persisted exchange intents.

        Returns:
            The action description, or None when no action is needed.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        if (
            not self.target_path.exists()
            and not position
            and not self.state.replica_initialized
            and not self.state.replica_order
            and not self.state.pending_protection_side
        ):
            self._missing_since = self._missing_since or now
            if (
                now - self._missing_since
            ).total_seconds() <= self.HEARTBEAT_TIMEOUT:
                return "reference replica waiting for initial reference target"
        target = self.load_reference_target(supplied_now)
        self._missing_since = None
        # Do not silently discard an uncertain entry from the older
        # executor.
        if self.state.pending_protection_side and position is None:
            message = (
                "legacy entry remains unresolved; operator review required"
            )
            raise RuntimeError(message)
        self.state.pending_protection_side = None
        self.state.pending_take_profit = None
        self.state.pending_strategy_close = False
        self.state.consumed_signal_side = None
        self.state.launched_at = target.reference_run
        self.state.last_processed_candle = target.last_processed_candle
        self.state.save()
        if self.state.replica_order:
            return self._pending(position, self.state.replica_order, now=now)
        return self._reconcile_reference_position(position, target, now)

    def _reconcile_reference_position(
        self,
        position: Mapping[str, object] | None,
        target: ReferenceTarget,
        now: datetime,
    ) -> str | None:
        """Clear legacy protection and reconcile exposure differences.

        Returns:
            The action description, or None when no action is needed.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        if position:
            key = (
                string(position["side"]),
                str(position["size"]),
                str(position["avgPrice"]),
            )
            # Clear legacy or manually added protection before it can become
            # a second source of normal exits. Recheck exchange values each
            # poll.
            if (
                key != self._cleared_position
                or amount(position.get("stopLoss") or 0)
                or amount(position.get("takeProfit") or 0)
            ):
                self.client.set_protection(self.symbol, Decimal(0), Decimal(0))
                self._cleared_position = key
        else:
            self._cleared_position = None
        side = string(position["side"]) if position else None
        quantity = amount(position["size"]) if position else Decimal(0)
        if not self.state.replica_initialized:
            # On upgrade adopt an existing matching position. A different
            # size is closed first, avoiding below-minimum incremental
            # orders. Never reset the real account or simulate past fills.
            self.state.replica_position_id = (
                target.id if side == target.side else None
            )
            self.state.replica_initialized = True
        elif (
            side is None
            and self.state.replica_position_id
            and self.state.replica_position_id == target.id
        ):
            message = (
                "exchange position disappeared while reference still holds;"
                " operator review required"
            )
            raise RuntimeError(message)
        self.state.save()
        if position and (
            side != target.side
            or quantity != target.quantity
            or self.state.replica_position_id != target.id
        ):
            return self._submit(
                ReplicaAction(
                    side="Sell" if side == "Buy" else "Buy",
                    quantity=quantity,
                    expected_side=None,
                    expected_quantity=Decimal(0),
                    reference_id=None,
                    reduce_only=True,
                ),
                now,
            )
        if quantity < target.quantity:
            return self._submit(
                ReplicaAction(
                    side=string(target.side),
                    quantity=target.quantity - quantity,
                    expected_side=target.side,
                    expected_quantity=target.quantity,
                    reference_id=target.id,
                ),
                now,
            )
        self.state.replica_position_id = target.id
        self.state.observed_position_side = side
        self.state.save()
        return None
