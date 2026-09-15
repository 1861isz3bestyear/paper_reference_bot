"""Execute reference's published position, without an independent strategy or SL."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from bybit_demo_bot.cli import BybitDemoBot, INTERVAL_SECONDS, ROOT
from bybit_demo_bot.client import BybitDemoError
from shared.reference_target import config_fingerprint


TERMINAL = {"Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated"}


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return result


def amount(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("quantity must be finite and nonnegative")
    return result


class ReferenceReplicaBot(BybitDemoBot):
    EXECUTOR_NAME = "Bybit DEMO reference replica"
    MAX_RETRY_SECONDS = 10
    HEARTBEAT_TIMEOUT = 120
    ORDER_TIMEOUT = 60

    def __init__(self, config, client, state, *, target_path: Path | None = None):
        super().__init__(config, client, state)
        self.target_path = target_path or ROOT / "reference_target.json"
        self.fingerprint = config_fingerprint(config)
        self._cleared_position = None
        self._limits = None
        self._missing_since = None

    def _target(self, now: datetime | None = None) -> dict:
        try:
            target = json.loads(self.target_path.read_text(encoding="utf-8"))
            now = now or datetime.now(timezone.utc)
            if target["version"] != 1 or target["config_fingerprint"] != self.fingerprint:
                raise ValueError("reference configuration/version does not match demo")
            if target["symbol"] != self.symbol or target["timeframe"] != self.config.timeframe:
                raise ValueError("reference symbol/timeframe does not match demo")
            heartbeat_age = (now - timestamp(target["published_at"])).total_seconds()
            if not -5 <= heartbeat_age <= self.HEARTBEAT_TIMEOUT:
                raise ValueError(
                    f"reference heartbeat is stale or in the future: age={heartbeat_age:.3f}s; "
                    f"published_at={target['published_at']}; checked_at={now.isoformat()}"
                )
            run = timestamp(target["reference_run"])
            interval = INTERVAL_SECONDS[self.config.timeframe]
            cursor = target["last_processed_candle"]
            if cursor is None:
                if target["side"] is not None or not 0 <= (now - run).total_seconds() <= 3 * interval + 30:
                    raise ValueError("reference has no current processed candle")
            else:
                candle = timestamp(cursor)
                if candle < run or not interval <= (now - candle).total_seconds() <= 3 * interval + 30:
                    raise ValueError("reference processed candle is stale or invalid")
            side = target["side"]
            quantity = amount(target["quantity"])
            if side not in (None, "Long", "Short"):
                raise ValueError("invalid reference side")
            if side is None:
                if quantity != 0 or target["position_id"] is not None:
                    raise ValueError("invalid flat reference target")
            elif quantity <= 0 or not target["position_id"] or amount(target["entry_price"]) <= 0:
                raise ValueError("invalid reference position")
            else:
                timestamp(target["position_id"])
            target["quantity"] = quantity
            target["side"] = {None: None, "Long": "Buy", "Short": "Sell"}[side]
            target["id"] = f'{target["reference_run"]}/{target["position_id"]}' if side else None
            return target
        except (OSError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
            raise RuntimeError(f"reference target unavailable: {exc}") from None

    def _halt(self, reason: str) -> None:
        self.state.halted_reason = reason
        self.state.save()

    def _flatten_halted(self, position) -> str:
        # Keep reconciling even after halting: a delayed entry must not escape
        # cleanup, and a failed/partial emergency close must be retried.
        self.state.observed_position_side = str(position["side"]) if position else None
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
                # order history is unavailable. Keep the intent for the next pass.
                print(f"replica cancellation error; will retry: {exc}", flush=True)
        if position:
            self.client.market_order(
                self.symbol, "Sell" if position["side"] == "Buy" else "Buy",
                amount(position["size"]), reduce_only=True,
            )
            return f"trading halted: {self.state.halted_reason}; emergency close submitted"
        return f"trading halted: {self.state.halted_reason}; exchange position flat"

    def _validate_entry(self, quantity: Decimal) -> None:
        if self._limits is None:
            self._limits = self.client.instruments(self.symbol)["lotSizeFilter"]
        step = amount(self._limits["qtyStep"])
        minimum = amount(self._limits["minOrderQty"])
        maximum = amount(self._limits.get("maxMktOrderQty", self._limits.get("maxOrderQty", quantity)))
        if step <= 0 or quantity % step != 0 or not minimum <= quantity <= maximum:
            raise RuntimeError("reference quantity cannot be executed exactly under exchange limits")
        price = amount(self.client.last_price(self.symbol))
        notional = quantity * price
        if price <= 0 or notional < amount(self._limits.get("minNotionalValue", 0)):
            raise RuntimeError("reference quantity is below exchange minimum notional")
        if notional > amount(self.client.available_usdt()):
            raise RuntimeError("demo balance cannot fund the exact reference quantity")

    def _submit(self, side: str, quantity: Decimal, expected_side: str | None,
                expected_quantity: Decimal, reference_id: str | None, now: datetime,
                *, reduce_only: bool = False) -> str:
        if not reduce_only:
            self._validate_entry(quantity)
        link = "ref-" + uuid4().hex  # Bybit's maximum orderLinkId length is 36.
        self.state.replica_order = {
            "link": link, "submitted_at": now.isoformat(),
            "expected_side": expected_side, "expected_quantity": str(expected_quantity),
            "reference_id": reference_id,
        }
        self.state.save()  # A timeout/crash must never cause a second submission.
        self.client.market_order(self.symbol, side, quantity, reduce_only=reduce_only, order_link_id=link)
        return f'reference replica submitted {"reduce" if reduce_only else "entry"} {side} {quantity}; order={link}'

    def _pending(self, position, now: datetime) -> str | None:
        pending = self.state.replica_order
        expired = (now - timestamp(pending["submitted_at"])).total_seconds() > self.ORDER_TIMEOUT
        try:
            order = self.client.order_by_link(self.symbol, pending["link"])
        except BybitDemoError:
            if not expired:
                raise
            self._halt("replica order/fill reconciliation timed out; operator review required")
            return self._flatten_halted(position)
        actual_side = position["side"] if position else None
        actual_qty = amount(position["size"]) if position else Decimal("0")
        if order and order.get("orderStatus") in TERMINAL:
            if order["orderStatus"] != "Filled":
                self._halt(f'replica order {pending["link"]} ended {order["orderStatus"]}; operator review required')
                return self._flatten_halted(position)
            if actual_side == pending["expected_side"] and actual_qty == amount(pending["expected_quantity"]):
                self.state.replica_position_id = pending["reference_id"]
                self.state.replica_order = None
                self.state.observed_position_side = actual_side
                self.state.save()
                self._cleared_position = None
                return "reference replica fill confirmed"
        if expired:
            self._halt("replica order/fill reconciliation timed out; operator review required")
            return self._flatten_halted(position)
        return "reference replica awaiting order/fill reconciliation"

    def reconcile_once(self, now: datetime | None = None) -> str | None:
        supplied_now = now
        position = self.client.position(self.symbol)
        now = supplied_now or datetime.now(timezone.utc)
        if self.state.halted_reason:
            return self._flatten_halted(position)
        try:
            if (not self.target_path.exists() and not position and not self.state.replica_initialized
                    and not self.state.replica_order and not self.state.pending_protection_side):
                self._missing_since = self._missing_since or now
                if (now - self._missing_since).total_seconds() <= self.HEARTBEAT_TIMEOUT:
                    return "reference replica waiting for initial reference target"
            target = self._target(supplied_now)
            self._missing_since = None
            # Do not silently discard an uncertain entry from the older executor.
            if self.state.pending_protection_side and position is None:
                raise RuntimeError("legacy entry remains unresolved; operator review required")
            self.state.pending_protection_side = None
            self.state.pending_take_profit = None
            self.state.pending_strategy_close = False
            self.state.consumed_signal_side = None
            self.state.launched_at = target["reference_run"]
            self.state.last_processed_candle = target["last_processed_candle"]
            self.state.save()
            if self.state.replica_order:
                return self._pending(position, now)
            if position:
                key = (position["side"], str(position["size"]), str(position["avgPrice"]))
                # Clear legacy or manually added protection before it can become
                # a second source of normal exits. Recheck exchange values each poll.
                if (key != self._cleared_position or amount(position.get("stopLoss") or 0)
                        or amount(position.get("takeProfit") or 0)):
                    self.client.set_protection(self.symbol, Decimal("0"), Decimal("0"))
                    self._cleared_position = key
            else:
                self._cleared_position = None
            side = position["side"] if position else None
            quantity = amount(position["size"]) if position else Decimal("0")
            if not self.state.replica_initialized:
                # On upgrade adopt an existing matching position. A different
                # size is closed first, avoiding below-minimum incremental orders. Never reset the real account or simulate past fills.
                self.state.replica_position_id = target["id"] if side == target["side"] else None
                self.state.replica_initialized = True
            elif side is None and self.state.replica_position_id and self.state.replica_position_id == target["id"]:
                raise RuntimeError("exchange position disappeared while reference still holds; operator review required")
            self.state.save()
            if position and (side != target["side"] or quantity != target["quantity"]
                             or self.state.replica_position_id != target["id"]):
                return self._submit("Sell" if side == "Buy" else "Buy", quantity,
                                    None, Decimal("0"), None, now, reduce_only=True)
            if quantity < target["quantity"]:
                return self._submit(target["side"], target["quantity"] - quantity,
                                    target["side"], target["quantity"], target["id"], now)
            self.state.replica_position_id = target["id"]
            self.state.observed_position_side = side
            self.state.save()
            return None
        except (RuntimeError, ValueError, KeyError, TypeError, InvalidOperation, BybitDemoError) as exc:
            # Order creation errors are ambiguous: preserve their intent and
            # reconcile by link ID before deciding to halt or submit anything else.
            if isinstance(exc, BybitDemoError) and self.state.replica_order and not self.state.halted_reason:
                return f"replica order error; intent retained for reconciliation: {exc}"
            self._halt(str(exc))
            return self._flatten_halted(position)
