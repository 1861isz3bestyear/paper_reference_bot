# Copyright 2026 anchored-vwam-backtest contributors
"""Execute a reference VWAP-zero plan as two independent Bybit hedge legs."""

import json
from datetime import UTC, datetime
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    InvalidOperation,
)
from uuid import uuid4

from bybit_demo_bot.cli import INTERVAL_SECONDS, ROOT, BybitDemoBot
from bybit_demo_bot.client import BybitDemoError, HedgeEntry
from bybit_demo_bot.replica import TERMINAL, amount, timestamp
from shared.hedge_strategy import HEDGE_MODE, StopOptions, stop_price
from shared.reference_target import config_fingerprint


class HedgeReplicaBot(BybitDemoBot):
    """Represent hedge replica bot."""

    EXECUTOR_NAME = "Bybit VWAP zero hedge"
    MAX_RETRY_SECONDS = 10

    def __init__(self, config, client, state, *, target_path=None) -> None:
        """Initialize the instance."""
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
        self.target_path = target_path or ROOT / "reference_target.json"
        self._instrument = None
        self._protected = {}

    def _target(self, now=None):
        payload = json.loads(self.target_path.read_text())
        now = now or datetime.now(UTC)
        if (
            payload.get("version") != 2
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
        age = (now - timestamp(payload["published_at"])).total_seconds()
        if not -5 <= age <= 120:
            message = f"Hedge reference heartbeat unavailable: age={age:.3f}s"
            raise RuntimeError(message)
        interval = INTERVAL_SECONDS[self.config.timeframe]
        cursor = payload["last_processed_candle"]
        run = timestamp(payload["reference_run"])
        if (
            not cursor
            or timestamp(cursor) < run
            or not interval
            <= (now - timestamp(cursor)).total_seconds()
            <= 3 * interval + 30
        ):
            message = "Hedge reference candle is stale or invalid"
            raise RuntimeError(message)
        plan = payload["hedge_plan"]
        if plan:
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
                plan["entry_timeout_bars"]
                != self.config.hedge_entry_timeout_bars
                or amount(plan["deposit"]) <= 0
            ):
                message = "Invalid hedge cycle parameters"
                raise RuntimeError(message)
            if not 0 <= amount(plan["fee_pct"]) < 100:
                message = "Invalid hedge fee"
                raise RuntimeError(message)
            for side, leg in plan["legs"].items():
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
                if stop is not None and (
                    amount(stop) <= 0
                    or (side == "Long" and amount(stop) >= entry)
                    or (side == "Short" and amount(stop) <= entry)
                ):
                    message = "Invalid hedge stop price"
                    raise RuntimeError(message)
            if (
                plan["legs"]["Long"]["entry_price"]
                != plan["legs"]["Short"]["entry_price"]
            ):
                message = "Both hedge entries must be at VWAP zero"
                raise RuntimeError(message)
            plan = dict(plan, key=payload["reference_run"] + "/" + plan["id"])
        return plan

    def _cleanup(self, positions):
        cycle = self.state.hedge_state or {}
        unresolved = False
        errors = []
        for order in cycle.get("orders", {}).values():
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

    def _quantize(self, value, rounding):
        tick = amount(self._instrument["priceFilter"]["tickSize"])
        if tick <= 0:
            message = "Invalid price tick"
            raise RuntimeError(message)
        return (amount(value) / tick).to_integral_value(
            rounding=rounding
        ) * tick

    def _orders(self, plan):
        self._instrument = self._instrument or self.client.instruments(
            self.symbol
        )
        lots = self._instrument["lotSizeFilter"]
        step = amount(lots["qtyStep"])
        if step <= 0:
            message = "Invalid quantity step"
            raise RuntimeError(message)
        orders = {}
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
            if (side == "Long" and (tp <= entry or sl >= entry)) or (
                side == "Short" and (tp >= entry or (sl and sl <= entry))
            ):
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

    def reconcile_once(self, now=None):
        """Reconcile once."""
        now = now or datetime.now(UTC)
        positions = self.client.hedge_positions(self.symbol)
        if self.state.halted_reason:
            _ = self._cleanup(positions)
            return (
                f"trading halted: {self.state.halted_reason}; hedge cleanup"
                " continues"
            )
        try:
            plan = self._target(now)
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
                        "hedge entry window expired; waiting for a new zero"
                        " touch"
                    )
                if positions or self.client.has_open_orders(self.symbol):
                    message = "Cannot adopt untracked hedge positions/orders"
                    raise RuntimeError(message)
                cycle = {
                    "key": plan["key"],
                    "plan": plan,
                    "orders": self._orders(plan),
                    "complete": False,
                }
                self.state.hedge_state = cycle
                self.state.save()
            if cycle["complete"]:
                if positions:
                    message = "Unexpected hedge position after completed cycle"
                    raise RuntimeError(message)
                return None
            all_done = True
            submissions = []
            sibling_exited = any(
                order.get("seen_position")
                and not any(
                    int(p["positionIdx"]) == (1 if side == "Long" else 2)
                    for p in positions
                )
                for side, order in cycle["orders"].items()
            )
            for side, order in cycle["orders"].items():
                idx = 1 if side == "Long" else 2
                position = next(
                    (p for p in positions if int(p["positionIdx"]) == idx), None
                )
                if order["submitted_at"] is None:
                    if now.timestamp() > deadline or sibling_exited:
                        continue
                    order["submitted_at"] = now.isoformat()
                    self.state.save()  # Intent survives an accepted order with a lost response.
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
                        return (
                            "hedge order response unavailable; intent"
                            f" retained: {exc}"
                        )
                    submissions.append(
                        f'hedge submitted {side} limit at {order["price"]}'
                    )
                    all_done = False
                    continue
                observed = self.client.order_by_link(self.symbol, order["link"])
                if observed is None:
                    if (
                        now - timestamp(order["submitted_at"])
                    ).total_seconds() > 60:
                        message = "Hedge order status unknown after 60 seconds"
                        raise RuntimeError(message)
                    return (
                        "hedge awaiting order acknowledgement; no resubmission"
                    )
                status = observed["orderStatus"]
                if status == "Rejected":
                    message = "Hedge entry rejected"
                    raise RuntimeError(message)
                if status not in TERMINAL:
                    all_done = False
                    if now.timestamp() > deadline or sibling_exited:
                        self.client.cancel_by_link(self.symbol, order["link"])
                if (
                    (
                        status == "Filled"
                        or amount(observed.get("cumExecQty", 0)) > 0
                    )
                    and not position
                    and not order.get("seen_position")
                ):
                    if (
                        now - timestamp(order["submitted_at"])
                    ).total_seconds() > 60:
                        message = (
                            "Filled hedge order has no confirmed position;"
                            " review required"
                        )
                        raise RuntimeError(message)
                    return "hedge waiting for position visibility"
                if position:
                    all_done = False
                    if not order.get("seen_position"):
                        order["seen_position"] = True
                        self.state.save()
                    self._instrument = (
                        self._instrument or self.client.instruments(self.symbol)
                    )
                    key = (position["size"], position["avgPrice"], order["tp"])
                    if self._protected.get(idx) != key:
                        qty = amount(position["size"])
                        if qty > amount(order["quantity"]):
                            message = (
                                "Hedge position exceeds tracked order size"
                            )
                            raise RuntimeError(message)
                        entry = amount(position["avgPrice"])
                        allocation = float(qty * entry) * (
                            1 + cycle["plan"]["fee_pct"] / 100
                        )
                        stops = stop_price(
                            float(entry),
                            float(qty),
                            allocation,
                            options=StopOptions(
                                deposit=cycle["plan"]["deposit"],
                                percent=self.config.stop_loss_pct,
                                basis=self.config.stop_loss_basis,
                                fee_pct=cycle["plan"]["fee_pct"],
                            ),
                        )
                        sl = (
                            self._quantize(
                                stops[side],
                                ROUND_UP if side == "Long" else ROUND_DOWN,
                            )
                            if stops[side]
                            else Decimal(0)
                        )
                        self.client.hedge_protection(
                            self.symbol,
                            idx,
                            stop_loss=sl,
                            take_profit=amount(order["tp"]),
                        )
                        self._protected[idx] = key
            if all_done:
                cycle["complete"] = True
                self.state.save()
            return "; ".join(submissions) or None
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
