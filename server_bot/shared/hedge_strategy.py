# Copyright 2026 anchored-vwam-backtest contributors
"""Causal VWAP-zero hedge strategy for historical and streaming runners."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import pandas as pd

from shared.candles import Candle, candle_rows
from shared.hedge_types import (
    HEDGE_MODE as HEDGE_MODE,
)
from shared.hedge_types import (
    HedgeOrder,
    HedgePlan,
    HedgePosition,
    HedgeSnapshot,
)
from shared.indicators import add_launch_weekly_anchored_vwap
from shared.margin_types import TradeRecord
from shared.models import Trade

MAXIMUM_PERCENT = 100

if TYPE_CHECKING:
    from datetime import date

    from shared.strategy_config import StrategyConfig


class HedgeState(Protocol):
    """Represent hedgestate."""

    hedge_state: HedgeSnapshot | None
    last_processed_candle: str | None


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, kw_only=True)
class StopOptions:
    """Configure a loss-budget stop calculation."""

    deposit: float
    percent: float
    basis: str
    fee_pct: float = 0.0


@dataclass(frozen=True, kw_only=True)
class HedgeBacktestOptions:
    """Configure hedge-specific historical execution."""

    target_sigma: float
    stop_loss_pct: float
    stop_loss_basis: str
    entry_timeout_bars: int
    minimum_order_size: float
    scheduled_days: pd.Series[date] | None = None
    take_profit_pct: float = 0.0


def stop_price(
    entry: float,
    quantity: float,
    allocation: float,
    *,
    options: StopOptions,
) -> dict[str, float | None]:
    """Long/short stops limiting net loss, including estimated exit fees.

    Returns:
        The computed result.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    if options.basis not in {"Position", "Deposit"}:
        msg = "Stop loss basis must be Position or Deposit."
        raise ValueError(msg)
    if options.percent == 0:
        return {"Long": None, "Short": None}
    risk = (
        (allocation if options.basis == "Position" else options.deposit)
        * options.percent
        / 100
    )
    fee = options.fee_pct / 100
    if risk <= 2 * quantity * entry * fee:
        msg = "Stop loss budget must exceed estimated round-trip fees."
        raise ValueError(msg)
    long_stop = (quantity * entry * (1 + fee) - risk) / (quantity * (1 - fee))
    short_stop = (quantity * entry * (1 - fee) + risk) / (quantity * (1 + fee))
    if long_stop <= 0:
        msg = "Stop loss budget must be smaller than the long position value."
        raise ValueError(msg)
    return {"Long": long_stop, "Short": short_stop}


@dataclass
class HedgeEngine:
    """Represent hedgeengine."""

    cash: float
    fee_pct: float
    target_sigma: float
    stop_loss_pct: float
    stop_loss_basis: str = "Position"
    entry_timeout_bars: int = 3
    minimum_order_size: float = 0.0
    positions: dict[str, HedgePosition] = field(default_factory=dict)
    pending: dict[str, HedgeOrder] = field(default_factory=dict)
    history: list[TradeRecord] = field(default_factory=list)
    plan: HedgePlan | None = None
    touching: bool = False
    pending_bars: int = 0
    last_time: str | None = None
    take_profit_pct: float = 0.0

    def __post_init__(self) -> None:
        """Implement __post_init__.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        for value in (
            self.cash,
            self.fee_pct,
            self.target_sigma,
            self.stop_loss_pct,
            self.minimum_order_size,
            self.take_profit_pct,
        ):
            if not math.isfinite(value):
                msg = "Hedge parameters must be finite."
                raise ValueError(msg)
        if (
            self.cash < 0
            or not 0 <= self.fee_pct < MAXIMUM_PERCENT
            or self.target_sigma <= 0
            or self.stop_loss_pct < 0
            or self.take_profit_pct < 0
        ):
            msg = "Invalid hedge capital, fee, target sigma or stop loss."
            raise ValueError(msg)
        if self.stop_loss_basis not in {"Position", "Deposit"}:
            msg = "Stop loss basis must be Position or Deposit."
            raise ValueError(msg)
        if (
            not _is_integer(self.entry_timeout_bars)
            or self.entry_timeout_bars < 1
        ):
            msg = "Entry timeout must be at least one candle."
            raise ValueError(msg)

    def snapshot(self) -> HedgeSnapshot:
        """Handle snapshot.

        Returns:
            The resulting value.

        """
        return deepcopy(
            HedgeSnapshot(
                cash=self.cash,
                fee_pct=self.fee_pct,
                target_sigma=self.target_sigma,
                stop_loss_pct=self.stop_loss_pct,
                stop_loss_basis=self.stop_loss_basis,
                entry_timeout_bars=self.entry_timeout_bars,
                minimum_order_size=self.minimum_order_size,
                positions=self.positions,
                pending=self.pending,
                history=self.history,
                plan=self.plan,
                touching=self.touching,
                pending_bars=self.pending_bars,
                last_time=self.last_time,
                take_profit_pct=self.take_profit_pct,
            )
        )

    def _close(
        self,
        side: str,
        price: float,
        timestamp: pd.Timestamp,
        *,
        reason: str,
    ) -> None:
        position = self.positions.pop(side)
        quantity = position["quantity"]
        gross = (
            quantity
            * (price - position["entry_price"])
            * (1 if side == "Long" else -1)
        )
        exit_fee = quantity * price * self.fee_pct / 100
        pnl = gross - position["entry_fee"] - exit_fee
        self.cash += position["allocation"] + pnl
        self.history.append(
            TradeRecord(
                deposit_size=position["allocation"],
                side=side,
                entry_time=position["entry_time"],
                exit_time=str(timestamp),
                entry_price=position["entry_price"],
                exit_price=price,
                quantity=quantity,
                pnl=pnl,
                entry_fee=position["entry_fee"],
                exit_fee=exit_fee,
                return_pct=100 * pnl / position["allocation"],
                exit_reason=reason,
            )
        )

    def _stop(self, side: str, row: Candle, *, entry_bar: bool = False) -> bool:
        position = self.positions[side]
        stop = position["stop_price"]
        if stop is None:
            return False
        touched = row.low <= stop if side == "Long" else row.high >= stop
        if touched:
            price = (
                stop
                if entry_bar
                else (
                    min(float(row.open), stop)
                    if side == "Long"
                    else max(float(row.open), stop)
                )
            )
            self._close(side, price, row.time, reason="Stop loss")
            return True
        return False

    def _process_open_positions(self, row: Candle) -> bool:
        """Close positions whose stop or target was reached.

        Returns:
            Whether at least one position was closed.

        """
        exited = False
        for side in list(self.positions):
            if self._stop(side, row):
                exited = True
                continue
            position = self.positions[side]
            target_reached = (
                side == "Long" and row.high >= position["take_profit"]
            ) or (side == "Short" and row.low <= position["take_profit"])
            if target_reached:
                target = position["take_profit"]
                price = (
                    max(float(row.open), target)
                    if side == "Long"
                    else min(float(row.open), target)
                )
                self._close(
                    side,
                    price,
                    row.time,
                    reason=position.get(
                        "take_profit_reason", "Reached hedge TP band"
                    ),
                )
                exited = True
        if exited:
            self.pending.clear()
        return exited

    def _fill_pending_orders(self, row: Candle, timestamp: str) -> None:
        """Fill pending hedge orders touched by the candle.

        Raises:
            ValueError: If a pending order has no cycle plan.

        """
        for side, order in list(self.pending.items()):
            entry = order["entry_price"]
            touched = row.low <= entry if side == "Long" else row.high >= entry
            if not touched:
                continue
            price = (
                min(float(row.open), entry)
                if side == "Long"
                else max(float(row.open), entry)
            )
            quantity = min(
                order["quantity"],
                order["allocation"] / (price * (1 + self.fee_pct / 100)),
            )
            allocation = quantity * price * (1 + self.fee_pct / 100)
            if self.plan is None:
                msg = "Pending hedge orders require a cycle plan."
                raise ValueError(msg)
            stops = stop_price(
                price,
                quantity,
                allocation,
                options=StopOptions(
                    deposit=self.plan["deposit"],
                    percent=self.stop_loss_pct,
                    basis=self.stop_loss_basis,
                    fee_pct=self.fee_pct,
                ),
            )
            take_profit, reason = self._take_profit(order, price, side)
            self.cash -= allocation
            self.positions[side] = HedgePosition(
                quantity=quantity,
                entry_price=price,
                allocation=allocation,
                entry_fee=quantity * price * self.fee_pct / 100,
                entry_time=timestamp,
                stop_price=stops[side],
                take_profit=take_profit,
                take_profit_reason=reason,
            )
            del self.pending[side]
            _ = self._stop(side, row, entry_bar=True)

    def _take_profit(
        self, order: HedgeOrder, price: float, side: str
    ) -> tuple[float, str]:
        """Return the first reached target and its reason.

        Returns:
            The target price and the reason it will close the position.

        """
        target = order["take_profit"]
        reason = "Reached hedge TP band"
        if self.take_profit_pct <= 0:
            return target, reason
        percent_target = price * (
            1 + self.take_profit_pct / 100
            if side == "Long"
            else 1 - self.take_profit_pct / 100
        )
        target_is_sooner = (side == "Long" and percent_target <= target) or (
            side == "Short" and percent_target >= target
        )
        if target_is_sooner:
            return percent_target, "Take profit"
        return target, reason

    def _arm_new_cycle(self, row: Candle, vwap: float, std: float) -> None:
        """Arm both hedge legs after a fresh VWAP touch.

        Raises:
            ValueError: If the short target would be non-positive.

        """
        allocation = self.cash / 2
        if allocation < self.minimum_order_size or allocation <= 0:
            return
        if vwap - self.target_sigma * std <= 0:
            msg = "Short TP band must be positive."
            raise ValueError(msg)
        quantity = allocation / (vwap * (1 + self.fee_pct / 100))
        stops = stop_price(
            vwap,
            quantity,
            allocation,
            options=StopOptions(
                deposit=self.cash,
                percent=self.stop_loss_pct,
                basis=self.stop_loss_basis,
                fee_pct=self.fee_pct,
            ),
        )
        legs = {
            side: HedgeOrder(
                entry_price=float(vwap),
                quantity=quantity,
                allocation=allocation,
                take_profit=float(
                    vwap
                    + (1 if side == "Long" else -1) * self.target_sigma * std
                ),
                stop_price=stops[side],
            )
            for side in ("Long", "Short")
        }
        timestamp = pd.Timestamp(row.time).isoformat()
        self.plan = HedgePlan(
            id=timestamp,
            created_at=timestamp,
            deposit=self.cash,
            legs=legs,
            entry_timeout_bars=self.entry_timeout_bars,
            fee_pct=self.fee_pct,
        )
        self.pending = deepcopy(legs)
        self.pending_bars = 0

    def process(
        self, row: Candle, vwap: float, std: float, *, allow_new: bool = True
    ) -> bool:
        """Handle process.

        Returns:
            The resulting value.

        """
        timestamp = pd.Timestamp(row.time).isoformat()
        if self.last_time is not None and pd.Timestamp(
            timestamp
        ) <= pd.Timestamp(self.last_time):
            return False
        self.last_time = timestamp
        active_at_start = bool(self.positions or self.pending)
        _ = self._process_open_positions(row)
        self._fill_pending_orders(row, timestamp)
        if active_at_start:
            self.pending_bars += 1
            if self.pending_bars >= self.entry_timeout_bars:
                self.pending.clear()
        if self.plan and not self.positions and not self.pending:
            self.plan = None
        valid = pd.notna(vwap) and pd.notna(std) and vwap > 0 and std > 0
        touched = bool(valid and row.low <= vwap <= row.high)
        cycle_inactive = not self.positions and not self.pending
        fresh_touch = touched and not self.touching and not active_at_start
        if allow_new and fresh_touch and cycle_inactive:
            self._arm_new_cycle(row, vwap, std)
        self.touching = touched
        return True

    def equity(self, price: float) -> float:
        """Handle equity.

        Returns:
            The resulting value.

        """
        return self.cash + sum(
            p["allocation"]
            - p["entry_fee"]
            + p["quantity"]
            * (price - p["entry_price"])
            * (1 if side == "Long" else -1)
            for side, p in self.positions.items()
        )

    def close_all(
        self, price: float, timestamp: pd.Timestamp, reason: str
    ) -> None:
        """Handle close all."""
        for side in list(self.positions):
            self._close(side, float(price), timestamp, reason=reason)
        self.pending.clear()
        self.plan = None

    def trades(self) -> list[Trade]:
        """Handle trades.

        Returns:
            The resulting value.

        """
        return [
            Trade(
                deposit_size=t["deposit_size"],
                side=t["side"],
                entry_price=t["entry_price"],
                exit_price=t["exit_price"],
                quantity=t["quantity"],
                pnl=t["pnl"],
                return_pct=t["return_pct"],
                exit_reason=t["exit_reason"],
                entry_time=pd.Timestamp(t["entry_time"]),
                exit_time=pd.Timestamp(t["exit_time"]),
            )
            for t in self.history
        ]


def hedge_backtest(
    data: pd.DataFrame,
    initial_capital: float,
    fee_pct: float,
    *,
    options: HedgeBacktestOptions,
) -> tuple[pd.DataFrame, list[Trade]]:
    """Handle hedge backtest.

    Returns:
        The resulting value.

    """
    engine = HedgeEngine(
        initial_capital,
        fee_pct,
        options.target_sigma,
        options.stop_loss_pct,
        options.stop_loss_basis,
        options.entry_timeout_bars,
        options.minimum_order_size,
        take_profit_pct=options.take_profit_pct,
    )
    rows: list[dict[str, object]] = []
    for i, row in enumerate(candle_rows(data)):
        session_end = options.scheduled_days is not None and (
            i == len(data) - 1
            or options.scheduled_days.iloc[i + 1]
            != options.scheduled_days.iloc[i]
        )
        _ = engine.process(
            row,
            float(row.order_vwap),
            float(row.order_std),
            allow_new=not session_end,
        )
        if session_end:
            engine.close_all(row.close, row.time, "End of scheduled session")
            engine.touching = False
        if i == len(data) - 1:
            engine.close_all(row.close, row.time, "End of strategy")
        rows.append({
            "time": row.time,
            "equity": engine.equity(row.close),
            "position": sum(
                p["quantity"] * (1 if s == "Long" else -1)
                for s, p in engine.positions.items()
            ),
            "close": row.close,
            "anchored_vwap": row.anchored_vwap,
        })
    return pd.DataFrame(rows), engine.trades()


def process_hedge_history(
    state: HedgeState,
    candles: pd.DataFrame,
    config: StrategyConfig,
    *,
    launched_at: pd.Timestamp,
    fee_pct: float,
) -> int:
    """Advance a persisted paper hedge ledger without end-of-backtest.

    liquidation.

    Returns:
        The computed result.

    Raises:
        RuntimeError: If the operation cannot be completed.

    """
    anchor = pd.Timestamp(launched_at) - pd.Timedelta(
        days=(
            config.anchor_before_days
            if config.anchor_before_strategy_start
            else 0
        )
    )
    data = add_launch_weekly_anchored_vwap(
        candles, anchor, config.vwap_anchor_reset_weeks
    )
    data["order_vwap"] = data["anchored_vwap"].shift(1)
    data["order_std"] = data["anchored_std"].shift(1)
    take_profit_pct = config.take_profit_pct
    engine = (
        HedgeEngine(**state.hedge_state)
        if state.hedge_state
        else HedgeEngine(
            config.initial_capital,
            fee_pct,
            config.close_order_vwap_sigma,
            config.stop_loss_pct,
            config.stop_loss_basis,
            config.hedge_entry_timeout_bars,
            config.minimum_order_size,
            take_profit_pct=take_profit_pct,
        )
    )
    current_risk = (
        engine.target_sigma,
        engine.stop_loss_pct,
        engine.stop_loss_basis,
        engine.entry_timeout_bars,
        engine.minimum_order_size,
        engine.take_profit_pct,
    )
    configured_risk = (
        config.close_order_vwap_sigma,
        config.stop_loss_pct,
        config.stop_loss_basis,
        config.hedge_entry_timeout_bars,
        config.minimum_order_size,
        config.take_profit_pct,
    )
    if current_risk != configured_risk:
        if engine.positions or engine.pending:
            msg = "Cannot change hedge risk/TP settings during an active cycle."
            raise RuntimeError(msg)
        engine.target_sigma = config.close_order_vwap_sigma
        engine.stop_loss_pct = config.stop_loss_pct
        engine.stop_loss_basis = config.stop_loss_basis
        engine.entry_timeout_bars = config.hedge_entry_timeout_bars
        engine.minimum_order_size = config.minimum_order_size
        engine.take_profit_pct = config.take_profit_pct
    processed = 0
    for row in candle_rows(data):
        if row.time < launched_at:
            continue
        if engine.process(row, float(row.order_vwap), float(row.order_std)):
            processed += 1
            state.last_processed_candle = pd.Timestamp(row.time).isoformat()
    state.hedge_state = engine.snapshot()
    return processed
