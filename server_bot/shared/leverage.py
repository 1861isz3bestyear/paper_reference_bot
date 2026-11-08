# Copyright 2026 anchored-vwam-backtest contributors
"""Isolated USDT perpetual simulation, with explicit candle-resolution.

assumptions.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import TYPE_CHECKING, Literal, Protocol

import pandas as pd

from shared.candles import Candle, candle_rows
from shared.hedge_strategy import HEDGE_MODE, StopOptions, stop_price
from shared.indicators import add_launch_weekly_anchored_vwap
from shared.leverage_data import (
    INTERVAL_MINUTES,
    load_leverage_data,
    validate_contract,
)
from shared.margin_types import (
    Contract,
    MarginOrder,
    MarginPosition,
    MarginSettings,
    MarginSnapshot,
    MarginTradeRecord,
    RiskTier,
    validate_leverage,
)
from shared.models import Trade

LEVERAGE_STEP_TOLERANCE = 1e-8
MAXIMUM_PERCENT = 100

if TYPE_CHECKING:
    from datetime import date

    from shared.paper_config import PaperBotConfig


class MarginState(Protocol):
    """Represent marginstate."""

    margin_state: MarginSnapshot | None
    last_processed_candle: str | None


@dataclass(frozen=True)
class LeveragedTrade(Trade):
    """Represent leveragedtrade."""

    leverage: int
    margin: float
    entry_notional: float
    entry_fee: float
    exit_fee: float
    funding_paid: float
    liquidation_price: float
    gross_pnl: float
    liquidation_adjustment: float


@dataclass(frozen=True, kw_only=True)
class OpenOptions:
    """Configure allocation and order behavior for a margin entry."""

    allocation: float
    target: float | None = None
    limit: bool = False


@dataclass(frozen=True, kw_only=True)
class ExitDecision:
    """Describe an executable strategy exit before changing the account."""

    price: float
    reason: Literal["Take profit", "Reached exit band", "Close below AVWAP"]
    limit: bool


def floor_step(value: float, step: float) -> float:
    """Handle floor step.

    Returns:
        The resulting value.

    """
    return float(
        (Decimal(str(value)) / Decimal(str(step))).to_integral_value(
            rounding=ROUND_DOWN
        )
        * Decimal(str(step))
    )


@dataclass
class MarginEngine:
    """Represent marginengine."""

    cash: float
    settings: MarginSettings
    contract: Contract
    positions: dict[str, MarginPosition] = field(default_factory=dict)
    pending: dict[str, MarginOrder] = field(default_factory=dict)
    history: list[MarginTradeRecord] = field(default_factory=list)
    last_time: str | None = None
    touching: bool = False
    pending_bars: int = 0
    previous_close: float | None = None
    previous_vwap: float | None = None
    reentry_ready: bool = True
    last_mark: float | None = None

    def __post_init__(self) -> None:
        """Implement __post_init__.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        validate_leverage(
            self.settings["leverage"], self.settings["slippage_bps"]
        )
        validate_contract(self.contract)
        lev = self.settings["leverage"]
        if (
            lev > self.contract["max_leverage"]
            or abs(
                lev / self.contract["leverage_step"]
                - round(lev / self.contract["leverage_step"])
            )
            > LEVERAGE_STEP_TOLERANCE
        ):
            msg = "Selected leverage is not supported by this contract."
            raise ValueError(msg)
        if not math.isfinite(self.cash) or self.cash < 0:
            msg = "Margin cash must be finite and nonnegative."
            raise ValueError(msg)
        if not 0 <= self.settings["fee_pct"] < MAXIMUM_PERCENT:
            msg = "Invalid perpetual fee."
            raise ValueError(msg)

    @property
    def fee(self) -> float:
        """Handle fee."""
        return self.settings["fee_pct"] / 100

    def snapshot(self) -> MarginSnapshot:
        """Handle snapshot.

        Returns:
            The resulting value.

        """
        return deepcopy(
            MarginSnapshot(
                cash=self.cash,
                settings=self.settings,
                contract=self.contract,
                positions=self.positions,
                pending=self.pending,
                history=self.history,
                last_time=self.last_time,
                touching=self.touching,
                pending_bars=self.pending_bars,
                previous_close=self.previous_close,
                previous_vwap=self.previous_vwap,
                reentry_ready=self.reentry_ready,
                last_mark=self.last_mark,
            )
        )

    def tier(self, notional: float) -> RiskTier:
        """Handle tier.

        Returns:
            The resulting value.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        for tier in self.contract["tiers"]:
            if notional <= tier["limit"]:
                return tier
        msg = "Position exceeds the available exchange risk tiers."
        raise ValueError(msg)

    def liquidation_price(self, side: str, p: MarginPosition) -> float:
        """Handle liquidation price.

        Returns:
            The resulting value.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        q, entry = p["quantity"], p["entry_price"]
        margin = p["margin"] - p["funding_paid"]
        # Solve equity = maintenance margin + estimated taker close fee,
        # checking the tier at the resulting mark notional.
        for tier in self.contract["tiers"]:
            rate = tier["mmr"] + self.fee
            price = (
                (q * entry - margin - tier["deduction"]) / (q * (1 - rate))
                if side == "Long"
                else (q * entry + margin + tier["deduction"]) / (q * (1 + rate))
            )
            if price <= 0 and side == "Long":
                return 0.0
            if price > 0 and self.tier(q * price) == tier:
                return price
        msg = "Cannot resolve liquidation price within risk tiers."
        raise ValueError(msg)

    def equity(self, mark: float) -> float:
        """Handle equity.

        Returns:
            The resulting value.

        """
        return self.cash + sum(
            p["margin"]
            + p["reserve"]
            - p["funding_paid"]
            + p["quantity"]
            * (mark - p["entry_price"])
            * (1 if side == "Long" else -1)
            for side, p in self.positions.items()
        )

    def limit_price(self, price: float, *, buying: bool) -> float:
        """Handle limit price.

        Returns:
            The resulting value.

        """
        tick = Decimal(str(self.contract["price_tick"]))
        return float(
            (Decimal(str(price)) / tick).to_integral_value(
                rounding=ROUND_DOWN if buying else ROUND_UP
            )
            * tick
        )

    def _open(
        self,
        side: str,
        price: float,
        timestamp: pd.Timestamp,
        *,
        options: OpenOptions,
    ) -> bool:
        if not options.limit:
            price *= (
                1
                + (1 if side == "Long" else -1)
                * self.settings["slippage_bps"]
                / 10000
            )
        lev = self.settings["leverage"]
        # Reserve opening and estimated closing fees in addition to initial
        # margin.
        q = floor_step(
            min(
                options.allocation / (price * (1 / lev + 2 * self.fee)),
                self.contract["max_quantity"],
            ),
            self.contract["quantity_step"],
        )
        notional = q * price
        if q < self.contract["min_quantity"] or notional < max(
            self.contract["min_notional"], self.settings["minimum_order_size"]
        ):
            return False
        tier = self.tier(notional)
        if lev > tier["max_leverage"]:
            msg = (
                "Selected leverage exceeds the risk-tier maximum for this"
                " position size."
            )
            raise ValueError(msg)
        margin, entry_fee, reserve = (
            notional / lev,
            notional * self.fee,
            notional * self.fee,
        )
        cost = margin + entry_fee + reserve
        if cost > self.cash + 1e-8:
            return False
        p = MarginPosition(
            liquidation_price=0.0,
            stop=None,
            quantity=q,
            entry_price=price,
            entry_time=str(timestamp),
            margin=margin,
            entry_fee=entry_fee,
            reserve=reserve,
            allocation=cost,
            funding_paid=0.0,
            target=options.target,
        )
        p["liquidation_price"] = self.liquidation_price(side, p)
        if (side == "Long" and p["liquidation_price"] >= price) or (
            side == "Short" and p["liquidation_price"] <= price
        ):
            msg = "Initial margin is insufficient for maintenance and fees."
            raise ValueError(msg)
        if self.settings["strategy_mode"] == HEDGE_MODE:
            p["stop"] = stop_price(
                price,
                q,
                margin,
                options=StopOptions(
                    deposit=self.cash
                    + sum(x["allocation"] for x in self.positions.values()),
                    percent=self.settings["stop_loss_pct"],
                    basis=self.settings["stop_loss_basis"],
                    fee_pct=self.settings["fee_pct"],
                ),
            )[side]
        else:
            pct = self.settings["stop_loss_pct"] / 100
            p["stop"] = (
                price * (1 - pct if side == "Long" else 1 + pct)
                if pct
                else None
            )
        self.cash -= cost
        self.positions[side] = p
        return True

    def _close(
        self,
        side: str,
        price: float,
        timestamp: pd.Timestamp,
        *,
        reason: str,
        limit: bool = False,
    ) -> None:
        p = self.positions.pop(side)
        if not limit:
            price *= (
                1
                + (-1 if side == "Long" else 1)
                * self.settings["slippage_bps"]
                / 10000
            )
        exit_fee = p["quantity"] * price * self.fee
        gross = (
            p["quantity"]
            * (price - p["entry_price"])
            * (1 if side == "Long" else -1)
        )
        raw_pnl = gross - p["entry_fee"] - exit_fee - p["funding_paid"]
        pnl = raw_pnl
        # An isolated liquidation loses the allocated margin; residual margin is
        # conservatively forfeited. Other legs and free wallet cash stay
        # separate.
        if reason == "Liquidation":
            pnl = -p["allocation"]
        else:
            pnl = max(-p["allocation"], pnl)
        self.cash += p["allocation"] + pnl
        self.history.append(
            MarginTradeRecord(
                deposit_size=p["allocation"],
                side=side,
                entry_time=p["entry_time"],
                exit_time=str(timestamp),
                entry_price=p["entry_price"],
                exit_price=price,
                quantity=p["quantity"],
                pnl=pnl,
                return_pct=100 * pnl / p["allocation"],
                exit_reason=reason,
                leverage=self.settings["leverage"],
                margin=p["margin"],
                entry_notional=p["quantity"] * p["entry_price"],
                entry_fee=p["entry_fee"],
                exit_fee=exit_fee,
                funding_paid=p["funding_paid"],
                liquidation_price=p["liquidation_price"],
                gross_pnl=gross,
                liquidation_adjustment=pnl - raw_pnl,
            )
        )

    def _adverse(
        self, side: str, row: Candle, *, entry_bar: bool = False
    ) -> bool:
        p = self.positions[side]
        p["liquidation_price"] = self.liquidation_price(side, p)
        liq = p["liquidation_price"]
        # Mark and trade OHLC do not establish which barrier was reached first.
        # Liquidation takes priority whenever both are reachable in one candle.
        if (side == "Long" and row.mark_low <= liq) or (
            side == "Short" and row.mark_high >= liq
        ):
            self._close(side, liq, row.time, reason="Liquidation")
            return True
        stop = p["stop"]
        if stop is not None and (
            (side == "Long" and row.low <= stop)
            or (side == "Short" and row.high >= stop)
        ):
            price = (
                stop
                if entry_bar
                else (
                    min(row.open, stop)
                    if side == "Long"
                    else max(row.open, stop)
                )
            )
            self._close(side, price, row.time, reason="Stop loss")
            return True
        return False

    def close_all(
        self, price: float, timestamp: pd.Timestamp, reason: str
    ) -> None:
        """Handle close all."""
        for side in list(self.positions):
            self._close(side, price, timestamp, reason=reason)
        self.pending.clear()

    def process(
        self, row: Candle, vwap: float, std: float, *, allow_new: bool = True
    ) -> bool:
        """Handle process.

        Returns:
            The resulting value.

        """
        if self.last_time and pd.Timestamp(row.time) <= pd.Timestamp(
            self.last_time
        ):
            return False
        self.last_time = str(row.time)
        self.last_mark = float(row.mark_close)
        active = bool(self.positions or self.pending)
        history_length = len(self.history)
        self._settle_positions(row, vwap, std)
        exited = len(self.history) > history_length
        if exited:
            self.pending.clear()
            self.reentry_ready = True
        self._fill_pending(row)
        if active:
            self.pending_bars += 1
            if self.pending_bars >= self.settings["hedge_entry_timeout_bars"]:
                self.pending.clear()
        valid = (
            math.isfinite(vwap) and math.isfinite(std) and vwap > 0 and std >= 0
        )
        touched = valid and std > 0 and row.low <= vwap <= row.high
        if (
            allow_new
            and not exited
            and not self.positions
            and not self.pending
            and self.cash > 0
        ):
            mode = self.settings["strategy_mode"]
            if mode == HEDGE_MODE:
                if touched and not self.touching and not active:
                    self._arm_hedge(vwap, std)
            elif mode == "AVWAP crossover" and math.isfinite(vwap):
                self._enter_crossover(row, vwap)
            elif mode == "VWAP band mean reversion" and valid:
                self._enter_band(row, vwap, std)
        self.touching = touched
        self.previous_close = float(row.close)
        self.previous_vwap = vwap
        return True

    def _settle_positions(self, row: Candle, vwap: float, std: float) -> None:
        """Settle funding, then adverse exits, then strategy exits per leg."""
        for side in list(self.positions):
            p = self.positions[side]
            # Funding is settled at candle open, before any new fills.
            p["funding_paid"] += (
                p["quantity"]
                * row.mark_open
                * row.funding_rate
                * (1 if side == "Long" else -1)
            )
            if self._adverse(side, row):
                continue
            decision = self._profit_exit(side, row)
            if decision is None:
                decision = self._strategy_exit(side, row, vwap, std=std)
            if decision is not None:
                self._close(
                    side,
                    decision.price,
                    row.time,
                    reason=decision.reason,
                    limit=decision.limit,
                )

    def _profit_exit(self, side: str, row: Candle) -> ExitDecision | None:
        """Calculate the fixed take-profit exit, including favorable gaps.

        Returns:
            The reachable profit exit, or None when no target is reached.

        """
        if not self.settings["take_profit_pct"]:
            return None
        target = self.positions[side]["entry_price"] * (
            1
            + (1 if side == "Long" else -1)
            * self.settings["take_profit_pct"]
            / 100
        )
        if (side == "Long" and row.high >= target) or (
            side == "Short" and row.low <= target
        ):
            return ExitDecision(
                price=(
                    max(row.open, target)
                    if side == "Long"
                    else min(row.open, target)
                ),
                reason="Take profit",
                limit=True,
            )
        return None

    def _strategy_exit(
        self, side: str, row: Candle, vwap: float, *, std: float
    ) -> ExitDecision | None:
        """Calculate an exit band or crossover decision.

        Returns:
            The strategy exit, or None when its conditions are not met.

        """
        target = self.positions[side]["target"]
        upward = side == "Long"
        mode = self.settings["strategy_mode"]
        if (
            mode == "VWAP band mean reversion"
            and math.isfinite(vwap)
            and math.isfinite(std)
        ):
            direction = 1 if side == "Long" else -1
            strategy_direction = 1 if self.settings["trend_mode"] else -1
            entry_sigma = (
                direction * strategy_direction * self.settings["band_sigma"]
            )
            exit_sigma = (
                direction
                * strategy_direction
                * self.settings["exit_band_sigma"]
            )
            target = vwap + exit_sigma * std
            upward = exit_sigma >= entry_sigma
        if target is not None:
            target = self.limit_price(target, buying=side == "Short")
        if target is not None and (
            (upward and row.high >= target)
            or (not upward and row.low <= target)
        ):
            return ExitDecision(
                price=(
                    max(row.open, target) if upward else min(row.open, target)
                ),
                reason="Reached exit band",
                limit=True,
            )
        if (
            mode == "AVWAP crossover"
            and row.close < vwap
            and self.previous_close is not None
            and self.previous_vwap is not None
            and self.previous_close >= self.previous_vwap
        ):
            return ExitDecision(
                price=row.close, reason="Close below AVWAP", limit=False
            )
        return None

    def _fill_pending(self, row: Candle) -> None:
        """Fill reachable orders before expiry and check entry-bar adversity."""
        for side, order in list(self.pending.items()):
            entry = order["price"]
            if (side == "Long" and row.low <= entry) or (
                side == "Short" and row.high >= entry
            ):
                price = (
                    min(row.open, entry)
                    if side == "Long"
                    else max(row.open, entry)
                )
                # Keep planned quantity bounded even after favorable price gaps.
                allocation = min(
                    order["allocation"], order["allocation"] * price / entry
                )
                if self._open(
                    side,
                    price,
                    row.time,
                    options=OpenOptions(
                        allocation=allocation,
                        target=order["target"],
                        limit=True,
                    ),
                ):
                    _ = self._adverse(side, row, entry_bar=True)
                del self.pending[side]

    def _arm_hedge(self, vwap: float, std: float) -> None:
        """Reserve paired entry orders with positive take-profit bands.

        Raises:
            ValueError: If the short take-profit band is not positive.

        """
        if vwap - self.settings["exit_band_sigma"] * std <= 0:
            msg = "Short TP band must be positive."
            raise ValueError(msg)
        self.pending = {
            side: MarginOrder(
                price=self.limit_price(vwap, buying=side == "Long"),
                allocation=self.cash / 2,
                target=self.limit_price(
                    vwap
                    + (1 if side == "Long" else -1)
                    * std
                    * self.settings["exit_band_sigma"],
                    buying=side == "Short",
                ),
            )
            for side in ("Long", "Short")
        }
        self.pending_bars = 0

    def _enter_crossover(self, row: Candle, vwap: float) -> None:
        """Open a long on an upward crossing when reentry is permitted."""
        if row.close < vwap:
            self.reentry_ready = True
        cross = row.close > vwap and (
            self.previous_vwap is None
            or not math.isfinite(self.previous_vwap)
            or self.previous_close is None
            or self.previous_close <= self.previous_vwap
        )
        if (
            cross
            and (self.settings["allow_reentry"] or self.reentry_ready)
            and self._open(
                "Long",
                row.close,
                row.time,
                options=OpenOptions(allocation=self.cash),
            )
        ):
            self.reentry_ready = False

    def _enter_band(self, row: Candle, vwap: float, std: float) -> None:
        """Attempt the first eligible band, trying short before long."""
        upper = vwap + std * self.settings["band_sigma"]
        lower = vwap - std * self.settings["band_sigma"]
        choices = (
            [("Short", lower), ("Long", upper)]
            if self.settings["trend_mode"]
            else [("Short", upper), ("Long", lower)]
        )
        for side, candidate_price in choices:
            order_price = self.limit_price(
                candidate_price, buying=side == "Long"
            )
            if (
                self.settings["open_position_side"] not in {side, "Both"}
                or order_price <= 0
                or not row.low <= order_price <= row.high
            ):
                continue
            if self._open(
                side,
                order_price,
                row.time,
                options=OpenOptions(allocation=self.cash, limit=True),
            ):
                _ = self._adverse(side, row, entry_bar=True)
            break

    def trades(self) -> list[LeveragedTrade]:
        """Handle trades.

        Returns:
            The resulting value.

        """
        return [
            LeveragedTrade(
                deposit_size=t["deposit_size"],
                side=t["side"],
                entry_price=t["entry_price"],
                exit_price=t["exit_price"],
                quantity=t["quantity"],
                pnl=t["pnl"],
                return_pct=t["return_pct"],
                exit_reason=t["exit_reason"],
                leverage=t["leverage"],
                margin=t["margin"],
                entry_notional=t["entry_notional"],
                entry_fee=t["entry_fee"],
                exit_fee=t["exit_fee"],
                funding_paid=t["funding_paid"],
                liquidation_price=t["liquidation_price"],
                gross_pnl=t["gross_pnl"],
                liquidation_adjustment=t["liquidation_adjustment"],
                entry_time=pd.Timestamp(t["entry_time"]),
                exit_time=pd.Timestamp(t["exit_time"]),
            )
            for t in self.history
        ]


def leveraged_backtest(
    data: pd.DataFrame,
    initial_capital: float,
    settings: MarginSettings,
    *,
    contract: Contract,
    scheduled_days: pd.Series[date] | None = None,
) -> tuple[pd.DataFrame, list[Trade]]:
    """Handle leveraged backtest.

    Returns:
        The resulting value.

    """
    engine = MarginEngine(initial_capital, settings, contract)
    rows: list[dict[str, object]] = []
    for i, row in enumerate(candle_rows(data)):
        session_end = scheduled_days is not None and (
            i == len(data) - 1
            or scheduled_days.iloc[i + 1] != scheduled_days.iloc[i]
        )
        vwap = float(
            row.anchored_vwap
            if settings["strategy_mode"] == "AVWAP crossover"
            else row.order_vwap
        )
        _ = engine.process(
            row, vwap, float(row.order_std), allow_new=not session_end
        )
        if session_end or i == len(data) - 1:
            engine.close_all(
                row.close,
                row.time,
                (
                    "End of scheduled session"
                    if session_end
                    else "End of strategy"
                ),
            )
            engine.touching = False
            engine.previous_close = None
            engine.previous_vwap = None
            engine.reentry_ready = True
        rows.append({
            "time": row.time,
            "equity": engine.equity(row.mark_close),
            "position": sum(
                p["quantity"] * (1 if side == "Long" else -1)
                for side, p in engine.positions.items()
            ),
            "close": row.close,
            "anchored_vwap": row.anchored_vwap,
            "margin": sum(p["margin"] for p in engine.positions.values()),
            "notional": sum(
                p["quantity"] * row.mark_close
                for p in engine.positions.values()
            ),
        })
    return pd.DataFrame(rows), list[Trade](engine.trades())


def settings_from_config(
    config: PaperBotConfig, fee_pct: float
) -> MarginSettings:
    """Handle settings from config.

    Returns:
        The resulting value.

    """
    return MarginSettings(
        config_json=config.to_json(),
        leverage=config.leverage,
        slippage_bps=config.slippage_bps,
        fee_pct=fee_pct,
        strategy_mode=config.strategy_mode,
        band_sigma=config.open_order_vwap_sigma,
        exit_band_sigma=config.close_order_vwap_sigma,
        trend_mode=config.trend,
        open_position_side=config.open_position_side,
        stop_loss_pct=config.stop_loss_pct,
        stop_loss_basis=config.stop_loss_basis,
        minimum_order_size=config.minimum_order_size,
        take_profit_pct=config.take_profit_pct,
        allow_reentry=config.allow_immediate_reentry,
        hedge_entry_timeout_bars=config.hedge_entry_timeout_bars,
    )


def process_margin_history(
    state: MarginState,
    candles: pd.DataFrame,
    config: PaperBotConfig,
    *,
    launched_at: pd.Timestamp,
    fee_pct: float,
) -> int:
    """Handle process margin history.

    Returns:
        The resulting value.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    settings = settings_from_config(config, fee_pct)
    old = state.margin_state
    if old and old["settings"] != settings:
        msg = (
            "Leveraged run settings changed. Stop and start a fresh paper run."
        )
        raise ValueError(msg)
    anchor = launched_at - pd.Timedelta(
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
    data = data[data["time"] >= launched_at]
    if old and old["last_time"]:
        data = data[data["time"] > pd.Timestamp(old["last_time"])]
    if data.empty:
        return 0
    if (
        old
        and old["last_time"]
        and data["time"].iloc[0]
        != pd.Timestamp(old["last_time"])
        + pd.Timedelta(minutes=INTERVAL_MINUTES[config.timeframe])
    ):
        msg = "Missing candles after the saved leveraged cursor."
        raise ValueError(msg)
    data, contract = load_leverage_data(
        data, config.ticker.replace("_", ""), config.timeframe
    )
    restored = deepcopy(old) if old else None
    if restored is not None:
        restored["contract"] = contract
    engine = (
        MarginEngine(**restored)
        if restored is not None
        else MarginEngine(config.initial_capital, settings, contract)
    )
    for row in candle_rows(data):
        vwap = float(
            row.anchored_vwap
            if config.strategy_mode == "AVWAP crossover"
            else row.order_vwap
        )
        _ = engine.process(row, vwap, float(row.order_std))
    state.margin_state = engine.snapshot()
    state.last_processed_candle = engine.last_time
    return len(data)
