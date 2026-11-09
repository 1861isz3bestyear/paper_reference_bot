# Copyright 2026 anchored-vwam-backtest contributors
"""Execute a reference VWAP-zero plan as two independent Bybit hedge legs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    InvalidOperation,
)
from typing import TYPE_CHECKING, ClassVar, override
from uuid import uuid4

from bybit_demo_bot.client import BybitDemoError, HedgeEntry
from bybit_demo_bot.executor import BybitDemoBot
from bybit_demo_bot.replica import TERMINAL, amount, timestamp
from bybit_demo_bot.settings import INTERVAL_SECONDS, ROOT
from bybit_demo_bot.state_types import (
    HedgeCycle,
    HedgeExecutionOrder,
    ReplicaHedgePlan,
)
from shared.hedge_strategy import HEDGE_MODE, StopOptions, stop_price
from shared.hedge_validation import parse_hedge_plan
from shared.reference_target import config_fingerprint
from shared.validation import json_object, object_mapping, string

if TYPE_CHECKING:
    from pathlib import Path

    from bybit_demo_bot.client import BybitDemoClient
    from bybit_demo_bot.state import DemoState
    from reference_bot.config import PaperBotConfig
    from shared.hedge_types import HedgeOrder, HedgePlan


HEDGE_TARGET_VERSION = 2
MIN_HEARTBEAT_AGE_SECONDS = -5
MAX_HEARTBEAT_AGE_SECONDS = 120
PERCENT_SCALE = 100


ORDER_ACK_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, kw_only=True)
class HedgeEntryWindow:
    """Current exchange time and whether additional entries are forbidden."""

    now: datetime
    expired: bool


@dataclass(frozen=True, kw_only=True)
class HedgeOrderResult:
    """Distinguish completed legs, submitted orders and uncertain outcomes."""

    complete: bool
    submission: str | None = None
    waiting: str | None = None


class HedgeReplicaBot(BybitDemoBot):
    """Represent hedge replica bot."""

    EXECUTOR_NAME: ClassVar[str] = "Bybit VWAP zero hedge"
    MAX_RETRY_SECONDS: ClassVar[int] = 10

    def __init__(
        self,
        config: PaperBotConfig,
        client: BybitDemoClient,
        state: DemoState,
        *,
        target_path: Path | None = None,
    ) -> None:
        """Initialize the instance.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        super().__init__(config, client, state)
        if (
            getattr(state, "replica_order", None)
            or state.pending_protection_side
        ):
            message = (
                "Resolve legacy order intents before starting the hedge"
                " strategy"
            )
            raise RuntimeError(message)
        self.target_path: Path = target_path or ROOT / "reference_target.json"
        self._instrument: dict[str, object] | None = None
        self._protected: dict[int, tuple[str, str, str]] = {}

    def load_reference_target(
        self, now: datetime | None = None
    ) -> ReplicaHedgePlan | None:
        """Load and validate the published reference target.

        Returns:
            The load reference target result.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        payload = json_object(self.target_path.read_text())
        now = now or datetime.now(UTC)
        if (
            payload.get("version") != HEDGE_TARGET_VERSION
            or payload.get("strategy_mode") != HEDGE_MODE
            or payload.get("config_fingerprint")
            != config_fingerprint(self.config)
        ):
            message = "Hedge reference configuration/version mismatch"
            raise RuntimeError(message)
        if (
            payload["symbol"] != self.symbol
            or payload["timeframe"] != self.config.timeframe
        ):
            message = "Hedge reference market mismatch"
            raise RuntimeError(message)
        age = (now - timestamp(string(payload["published_at"]))).total_seconds()
        if not MIN_HEARTBEAT_AGE_SECONDS <= age <= MAX_HEARTBEAT_AGE_SECONDS:
            message = f"Hedge reference heartbeat unavailable: age={age:.3f}s"
            raise RuntimeError(message)
        interval = INTERVAL_SECONDS[self.config.timeframe]
        cursor = string(payload["last_processed_candle"])
        run = timestamp(string(payload["reference_run"]))
        if (
            not cursor
            or timestamp(cursor) < run
            or not interval
            <= (now - timestamp(cursor)).total_seconds()
            <= 3 * interval + 30
        ):
            message = "Hedge reference candle is stale or invalid"
            raise RuntimeError(message)
        raw_plan = payload["hedge_plan"]
        plan = parse_hedge_plan(raw_plan) if raw_plan is not None else None
        if plan is None:
            return None
        self._validate_hedge_plan(plan, run=run, cursor=cursor)
        return ReplicaHedgePlan(
            **plan, key=string(payload["reference_run"]) + "/" + plan["id"]
        )

    def _validate_hedge_plan(
        self, plan: HedgePlan, *, run: datetime, cursor: str
    ) -> None:
        """Require a complete cycle within the producer's chronological range.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        if set(plan["legs"]) != {"Long", "Short"}:
            message = "Hedge plan must contain both legs"
            raise RuntimeError(message)
        created = timestamp(plan["created_at"])
        if (
            created < run
            or created > timestamp(cursor)
            or plan["id"] != plan["created_at"]
        ):
            message = "Invalid hedge cycle timestamp"
            raise RuntimeError(message)
        if (
            plan["entry_timeout_bars"] != self.config.hedge_entry_timeout_bars
            or amount(plan["deposit"]) <= 0
        ):
            message = "Invalid hedge cycle parameters"
            raise RuntimeError(message)
        if not 0 <= amount(plan["fee_pct"]) < PERCENT_SCALE:
            message = "Invalid hedge fee"
            raise RuntimeError(message)
        for side, leg in plan["legs"].items():
            self._validate_hedge_leg(side, leg)
        if (
            plan["legs"]["Long"]["entry_price"]
            != plan["legs"]["Short"]["entry_price"]
        ):
            message = "Both hedge entries must be at VWAP zero"
            raise RuntimeError(message)

    @staticmethod
    def _validate_hedge_leg(side: str, leg: HedgeOrder) -> None:
        """Validate hedge quantities and protective price ordering.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        entry = amount(leg["entry_price"])
        tp = amount(leg["take_profit"])
        if (
            entry <= 0
            or tp <= 0
            or amount(leg["quantity"]) <= 0
            or amount(leg["allocation"]) <= 0
        ):
            message = "Invalid hedge order values"
            raise RuntimeError(message)
        if (side == "Long" and tp <= entry) or (
            side == "Short" and tp >= entry
        ):
            message = "Hedge TP is on the wrong side"
            raise RuntimeError(message)
        stop = leg["stop_price"]
        if stop is None:
            return
        if amount(stop) <= 0:
            message = "Invalid hedge stop price"
            raise RuntimeError(message)
        if (side == "Long" and amount(stop) >= entry) or (
            side == "Short" and amount(stop) <= entry
        ):
            message = "Invalid hedge stop price"
            raise RuntimeError(message)

    def _cleanup(self, positions: list[dict[str, object]]) -> bool:
        cycle = self.state.hedge_state
        orders: dict[str, HedgeExecutionOrder] = (
            cycle["orders"] if cycle else {}
        )
        unresolved = False
        errors: list[str] = []
        for order in orders.values():
            if order["submitted_at"] is None:
                continue
            try:
                observed = self.client.order_by_link(self.symbol, order["link"])
                if not observed or observed["orderStatus"] not in TERMINAL:
                    self.client.cancel_by_link(self.symbol, order["link"])
                    unresolved = True
            except BybitDemoError as exc:
                errors.append(str(exc))
                unresolved = True
        for position in positions:
            try:
                self.client.hedge_close(self.symbol, position)
            except BybitDemoError as exc:
                errors.append(str(exc))
        if errors:
            message = "Hedge cleanup incomplete; retry required: " + "; ".join(
                errors
            )
            raise BybitDemoError(message)
        return not positions and not unresolved

    def _quantize(self, value: object, rounding: str) -> Decimal:
        if self._instrument is None:
            message = "Instrument metadata must be loaded before rounding"
            raise RuntimeError(message)
        tick = amount(
            object_mapping(self._instrument["priceFilter"])["tickSize"]
        )
        if tick <= 0:
            message = "Invalid price tick"
            raise RuntimeError(message)
        return (amount(value) / tick).to_integral_value(
            rounding=rounding
        ) * tick

    def _orders(self, plan: ReplicaHedgePlan) -> dict[str, HedgeExecutionOrder]:
        self._instrument = self._instrument or self.client.instruments(
            self.symbol
        )
        lots = object_mapping(self._instrument["lotSizeFilter"])
        step = amount(lots["qtyStep"])
        if step <= 0:
            message = "Invalid quantity step"
            raise RuntimeError(message)
        orders: dict[str, HedgeExecutionOrder] = {}
        for side, leg in plan["legs"].items():
            quantity = (amount(leg["quantity"]) / step).to_integral_value(
                rounding=ROUND_DOWN
            ) * step
            entry = self._quantize(leg["entry_price"], ROUND_HALF_UP)
            tp = self._quantize(
                leg["take_profit"], ROUND_DOWN if side == "Long" else ROUND_UP
            )
            allocation = float(quantity * entry) * (1 + plan["fee_pct"] / 100)
            stops = (
                stop_price(
                    float(entry),
                    float(quantity),
                    allocation,
                    options=StopOptions(
                        deposit=plan["deposit"],
                        percent=self.config.stop_loss_pct,
                        basis=self.config.stop_loss_basis,
                        fee_pct=plan["fee_pct"],
                    ),
                )
                if quantity > 0
                else {"Long": None, "Short": None}
            )
            sl = (
                self._quantize(
                    stops[side], ROUND_UP if side == "Long" else ROUND_DOWN
                )
                if stops[side]
                else Decimal(0)
            )
            if (
                entry <= 0
                or quantity < amount(lots["minOrderQty"])
                or quantity
                > amount(
                    lots.get(
                        "maxMktOrderQty", lots.get("maxOrderQty", quantity)
                    )
                )
                or quantity * entry
                < max(
                    amount(lots.get("minNotionalValue", 0)),
                    amount(self.config.minimum_order_size),
                )
            ):
                message = (
                    "Hedge leg is below exchange/configured minimum or above"
                    " maximum"
                )
                raise RuntimeError(message)
            invalid_bands = (
                tp <= entry or sl >= entry
                if side == "Long"
                else tp >= entry or bool(sl and sl <= entry)
            )
            if invalid_bands:
                message = "Hedge bands/stops collapse at exchange tick size"
                raise RuntimeError(message)
            orders[side] = {
                "link": "hedge-" + uuid4().hex[:30],
                "submitted_at": None,
                "quantity": str(quantity),
                "price": str(entry),
                "tp": str(tp),
                "sl": str(sl),
            }
        needed = sum(
            amount(o["quantity"]) * amount(o["price"]) for o in orders.values()
        ) * (1 + amount(plan["fee_pct"]) / 100)
        if needed > self.client.available_usdt():
            message = "Insufficient balance for both hedge legs"
            raise RuntimeError(message)
        return orders

    def _protect_hedge_position(
        self,
        side: str,
        order: HedgeExecutionOrder,
        *,
        position: dict[str, object],
        plan: ReplicaHedgePlan,
    ) -> None:
        """Refresh protection after the actual fill becomes visible.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        idx = 1 if side == "Long" else 2
        if not order.get("seen_position"):
            order["seen_position"] = True
            self.state.save()
        self._instrument = self._instrument or self.client.instruments(
            self.symbol
        )
        key = (str(position["size"]), str(position["avgPrice"]), order["tp"])
        if self._protected.get(idx) == key:
            return
        qty = amount(position["size"])
        if qty > amount(order["quantity"]):
            message = "Hedge position exceeds tracked order size"
            raise RuntimeError(message)
        entry = amount(position["avgPrice"])
        allocation = float(qty * entry) * (1 + plan["fee_pct"] / 100)
        stops = stop_price(
            float(entry),
            float(qty),
            allocation,
            options=StopOptions(
                deposit=plan["deposit"],
                percent=self.config.stop_loss_pct,
                basis=self.config.stop_loss_basis,
                fee_pct=plan["fee_pct"],
            ),
        )
        sl = (
            self._quantize(
                stops[side], ROUND_UP if side == "Long" else ROUND_DOWN
            )
            if stops[side]
            else Decimal(0)
        )
        self.client.hedge_protection(
            self.symbol, idx, stop_loss=sl, take_profit=amount(order["tp"])
        )
        self._protected[idx] = key

    def _submit_hedge_entry(
        self, side: str, order: HedgeExecutionOrder, now: datetime
    ) -> HedgeOrderResult:
        """Persist the entry intent before submitting exactly one order.

        Returns:
            The submit hedge entry result.

        """
        order["submitted_at"] = now.isoformat()
        self.state.save()
        try:
            _ = self.client.hedge_limit_order(
                self.symbol,
                HedgeEntry(
                    side="Buy" if side == "Long" else "Sell",
                    quantity=amount(order["quantity"]),
                    price=amount(order["price"]),
                    take_profit=amount(order["tp"]),
                    stop_loss=amount(order["sl"]),
                    link=order["link"],
                ),
            )
        except BybitDemoError as exc:
            return HedgeOrderResult(
                complete=False,
                waiting=(
                    f"hedge order response unavailable; intent retained: {exc}"
                ),
            )
        return HedgeOrderResult(
            complete=False,
            submission=f'hedge submitted {side} limit at {order["price"]}',
        )

    def _observe_hedge_order(
        self,
        order: HedgeExecutionOrder,
        position: dict[str, object] | None,
        *,
        window: HedgeEntryWindow,
    ) -> HedgeOrderResult:
        """Reconcile acknowledgement, cancellation and delayed fill visibility.

        Returns:
            The observe hedge order result.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        submitted_at = order["submitted_at"]
        if submitted_at is None:
            message = "Cannot observe an unsubmitted hedge order"
            raise RuntimeError(message)
        elapsed = (window.now - timestamp(submitted_at)).total_seconds()
        observed = self.client.order_by_link(self.symbol, order["link"])
        if observed is None:
            if elapsed > ORDER_ACK_TIMEOUT_SECONDS:
                message = "Hedge order status unknown after 60 seconds"
                raise RuntimeError(message)
            return HedgeOrderResult(
                complete=False,
                waiting="hedge awaiting order acknowledgement; no resubmission",
            )
        status = observed["orderStatus"]
        if status == "Rejected":
            message = "Hedge entry rejected"
            raise RuntimeError(message)
        complete = status in TERMINAL
        if not complete and window.expired:
            self.client.cancel_by_link(self.symbol, order["link"])
        if (
            (status == "Filled" or amount(observed.get("cumExecQty", 0)) > 0)
            and not position
            and not order.get("seen_position")
        ):
            if elapsed > ORDER_ACK_TIMEOUT_SECONDS:
                message = (
                    "Filled hedge order has no confirmed position; review"
                    " required"
                )
                raise RuntimeError(message)
            return HedgeOrderResult(
                complete=False, waiting="hedge waiting for position visibility"
            )
        return HedgeOrderResult(complete=complete and not position)

    def _reconcile_cycle_orders(
        self,
        cycle: HedgeCycle,
        positions: list[dict[str, object]],
        *,
        now: datetime,
        deadline: float,
    ) -> str | None:
        """Reconcile legs sequentially and stop on ambiguous exchange responses.

        Returns:
            The action description, or None when no action is needed.

        """
        indexed = {int(str(p["positionIdx"])): p for p in positions}
        sibling_exited = any(
            order.get("seen_position")
            and (1 if side == "Long" else 2) not in indexed
            for side, order in cycle["orders"].items()
        )
        window = HedgeEntryWindow(
            now=now, expired=now.timestamp() > deadline or sibling_exited
        )
        all_done = True
        submissions: list[str] = []
        for side, order in cycle["orders"].items():
            position = indexed.get(1 if side == "Long" else 2)
            if order["submitted_at"] is None:
                if window.expired:
                    continue
                result = self._submit_hedge_entry(side, order, now)
            else:
                result = self._observe_hedge_order(
                    order, position, window=window
                )
                if not result.waiting and position:
                    self._protect_hedge_position(
                        side, order, position=position, plan=cycle["plan"]
                    )
            if result.waiting:
                return result.waiting
            if result.submission:
                submissions.append(result.submission)
            all_done = all_done and result.complete
        if all_done:
            cycle["complete"] = True
            self.state.save()
        return "; ".join(submissions) or None

    def _reconcile_hedge_target(
        self, now: datetime, positions: list[dict[str, object]]
    ) -> str | None:
        """End obsolete cycles before adopting and reconciling a fresh plan.

        Returns:
            The action description, or None when no action is needed.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        plan = self.load_reference_target(now)
        cycle = self.state.hedge_state
        if cycle and (not plan or plan["key"] != cycle["key"]):
            if not self._cleanup(positions):
                return (
                    "hedge cycle ended; cancelling entries and closing"
                    " remaining legs"
                )
            cycle["complete"] = True
            self.state.save()
        if not plan:
            return None
        deadline = (
            timestamp(plan["created_at"]).timestamp()
            + (1 + plan["entry_timeout_bars"])
            * INTERVAL_SECONDS[self.config.timeframe]
        )
        if not cycle or cycle["key"] != plan["key"]:
            if now.timestamp() > deadline:
                return (
                    "hedge entry window expired; waiting for a new zero touch"
                )
            if positions or self.client.has_open_orders(self.symbol):
                message = "Cannot adopt untracked hedge positions/orders"
                raise RuntimeError(message)
            cycle = HedgeCycle(
                key=plan["key"],
                plan=plan,
                orders=self._orders(plan),
                complete=False,
            )
            self.state.hedge_state = cycle
            self.state.save()
        if cycle["complete"]:
            if positions:
                message = "Unexpected hedge position after completed cycle"
                raise RuntimeError(message)
            return None
        return self._reconcile_cycle_orders(
            cycle, positions, now=now, deadline=deadline
        )

    @override
    def reconcile_once(self, now: datetime | None = None) -> str | None:
        """Reconcile a hedge cycle, retaining intents after ambiguous responses.

        Returns:
            The exchange action or waiting status, when applicable.

        """
        now = now or datetime.now(UTC)
        positions = self.client.hedge_positions(self.symbol)
        if self.state.halted_reason:
            _ = self._cleanup(positions)
            return (
                f"trading halted: {self.state.halted_reason}; hedge cleanup"
                " continues"
            )
        try:
            return self._reconcile_hedge_target(now, positions)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            InvalidOperation,
        ) as exc:
            self.state.halted_reason = str(exc)
            self.state.save()
            _ = self._cleanup(positions)
            return f"trading halted: {exc}; hedge cleanup continues"
