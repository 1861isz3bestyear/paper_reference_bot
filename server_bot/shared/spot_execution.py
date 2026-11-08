# Copyright 2026 anchored-vwam-backtest contributors
"""Unleveraged execution with explicit decisions and account state."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, override

from shared.models import Trade

if TYPE_CHECKING:
    import pandas as pd

    from shared.candles import Candle

Side = Literal["Long", "Short"]
AllowedSide = Literal["Long", "Short", "Both"]
ExitReason = Literal[
    "Stop loss",
    "Take profit",
    "Close below AVWAP",
    "Exit band",
    "End of scheduled session",
    "End of strategy",
    "Options GEX reversal",
]


@dataclass(frozen=True, kw_only=True)
class SpotRisk:
    """Configure position sizing, fees, and price-based protective exits."""

    fee_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    minimum_order_size: float
    slippage_bps: float = 0.0


@dataclass(frozen=True, kw_only=True)
class EntryDecision:
    """Describe an executable entry before changing the account."""

    side: Side
    price: float
    stop: float = math.nan


@dataclass(frozen=True, kw_only=True)
class ExitDecision:
    """Describe an executable exit and optional band annotation."""

    price: float
    reason: ExitReason
    sigma: float = 0.0

    def description(self) -> str:
        """Format the reason for the public trade record.

        Returns:
            The existing human-readable exit label.

        """
        if self.reason == "Exit band":
            return (
                f"Reached {self.sigma:+g}\N{GREEK SMALL LETTER SIGMA} exit band"
            )
        return self.reason


@dataclass(frozen=True, kw_only=True)
class SpotPosition:
    """Keep all state belonging to an open position together."""

    side: Side
    price: float
    time: pd.Timestamp
    capital: float
    quantity: float
    stop: float = math.nan


@dataclass
class SignalHistory:
    """Track crossover observations within a scheduled session."""

    close: float = math.nan
    vwap: float = math.nan
    reentry_ready: bool = True


@dataclass(frozen=True, kw_only=True)
class CrossoverSignals:
    """Decide entries and exits from closing-price AVWAP crossings."""

    allow_reentry: bool
    entry_bar_stop: bool = field(default=False, init=False)

    @staticmethod
    def reference(row: Candle) -> float:
        """Select the contemporaneous crossover indicator.

        Returns:
            The candle's anchored VWAP.

        """
        return row.anchored_vwap

    def entry(
        self, row: Candle, history: SignalHistory
    ) -> EntryDecision | None:
        """Decide whether the close crosses above AVWAP.

        Returns:
            A long entry when the crossing and reentry gate permit it.

        """
        crossed_up = row.close > row.anchored_vwap and (
            math.isnan(history.vwap) or history.close <= history.vwap
        )
        if crossed_up and (self.allow_reentry or history.reentry_ready):
            return EntryDecision(side="Long", price=row.close)
        return None

    @staticmethod
    def exit(
        row: Candle, position: SpotPosition, history: SignalHistory
    ) -> ExitDecision | None:
        """Decide whether a held long crosses below AVWAP.

        Returns:
            A closing-price exit on a downward crossing.

        """
        if (
            position.side == "Long"
            and row.close < row.anchored_vwap
            and history.close >= history.vwap
        ):
            return ExitDecision(price=row.close, reason="Close below AVWAP")
        return None


@dataclass(frozen=True, kw_only=True)
class BandSignals:
    """Decide band fills using indicators known before the candle opened."""

    entry_sigma: float
    exit_sigma: float
    side: AllowedSide
    trend: bool
    entry_bar_stop: bool = field(default=True, init=False)

    @staticmethod
    def reference(row: Candle) -> float:
        """Select the causal band indicator.

        Returns:
            The preceding candle's anchored VWAP.

        """
        return row.order_vwap

    def entry(
        self, row: Candle, history: SignalHistory
    ) -> EntryDecision | None:
        """Select a touched entry band, giving the short order precedence.

        Returns:
            The first eligible band order, if any.

        """
        del history  # Band orders do not depend on crossover history.
        short_sigma = -self.entry_sigma if self.trend else self.entry_sigma
        short_price = row.order_vwap + row.order_std * short_sigma
        long_price = row.order_vwap - row.order_std * short_sigma
        if (
            self.side in {"Short", "Both"}
            and row.low <= short_price <= row.high
        ):
            return EntryDecision(side="Short", price=short_price)
        if self.side in {"Long", "Both"} and row.low <= long_price <= row.high:
            return EntryDecision(side="Long", price=long_price)
        return None

    def exit(
        self, row: Candle, position: SpotPosition, history: SignalHistory
    ) -> ExitDecision | None:
        """Decide a band exit, accounting for gaps through resting orders.

        Returns:
            An executable band exit, if reached.

        """
        del history  # Band exits depend only on the resting order and candle.
        direction = 1 if self.trend else -1
        if position.side == "Short":
            direction = -direction
        sigma = direction * self.exit_sigma
        level = row.order_vwap + row.order_std * sigma
        price = band_fill(
            row, level, upward=sigma >= direction * self.entry_sigma
        )
        if price is None:
            return None
        return ExitDecision(price=price, reason="Exit band", sigma=sigma)


@dataclass(frozen=True, kw_only=True)
class OptionsGexSignals:
    """Experimental opening-price versus flip rule without a fallback."""

    side: AllowedSide = "Both"
    entry_bar_stop: bool = field(default=True, init=False)

    @staticmethod
    def reference(row: Candle) -> float:
        """Return the current gamma flip.

        Returns:
            The gamma-flip price, or unavailable.

        """
        return row.options_gamma_flip

    @staticmethod
    def _direction(row: Candle) -> float:
        """Return the directional distance from the gamma-flip reference.

        Returns:
            The signed distance used to choose the position side.

        """
        if math.isfinite(row.options_gamma_flip):
            return row.open - row.options_gamma_flip
        return math.nan

    def entry(
        self, row: Candle, history: SignalHistory
    ) -> EntryDecision | None:
        """Enter at the open in the permitted flip or fallback direction.

        Returns:
            An entry decision when a directional signal is available.

        """
        del history
        direction = self._direction(row)
        if not math.isfinite(direction) or direction == 0:
            return None
        side: Side = "Long" if direction > 0 else "Short"
        if self.side not in {"Both", side}:
            return None
        return EntryDecision(side=side, price=row.open)

    def exit(
        self,
        row: Candle,
        position: SpotPosition,
        history: SignalHistory,
    ) -> ExitDecision | None:
        """Close at the open when the available direction opposes the position.

        Returns:
            An exit decision when the gamma direction reverses.

        """
        del history
        direction = self._direction(row)
        if not math.isfinite(direction) or direction == 0:
            return None
        opposing = (position.side == "Long" and direction < 0) or (
            position.side == "Short" and direction > 0
        )
        if opposing:
            return ExitDecision(price=row.open, reason="Options GEX reversal")
        return None


@dataclass(frozen=True, kw_only=True)
class GexRegimeSignals(OptionsGexSignals):
    """Trade only explicitly confirmed, completed-candle regime signals."""

    @staticmethod
    @override
    def _direction(row: Candle) -> float:
        """Read the published price confirmation.

        Returns:
            Signed direction, or unavailable.

        """
        return row.options_direction


@dataclass(frozen=True, kw_only=True)
class SignedGexSignals(OptionsGexSignals):
    """Isolate the experimental sign-only hypothesis."""

    @staticmethod
    @override
    def _direction(row: Candle) -> float:
        """Read the experimental sign-only signal.

        Returns:
            Signed model exposure.

        """
        return row.options_gex


@dataclass(frozen=True, kw_only=True)
class DailyGexSignals(GexRegimeSignals):
    """Execute confirmed daily orders and preserve their original protection."""

    @override
    def entry(
        self, row: Candle, history: SignalHistory
    ) -> EntryDecision | None:
        """Use the replayed causal fill instead of the signal candle close.

        Returns:
            The executable daily order, if its side is permitted.

        """
        decision = super().entry(row, history)
        if decision is None or not math.isfinite(row.options_entry):
            return None
        return replace(decision, price=row.options_entry, stop=row.options_stop)

    @override
    def exit(
        self, row: Candle, position: SpotPosition, history: SignalHistory
    ) -> ExitDecision | None:
        """Keep an existing position until protection or scheduled liquidation.

        Returns:
            No signal-driven reversal exit.

        """
        del row, position, history
        return None


def band_fill(row: Candle, level: float, *, upward: bool) -> float | None:
    """Resolve an existing order against a gap or intrabar touch.

    Returns:
        The opening price after a gap, the touched level, or no fill.

    """
    if math.isnan(level):
        return None
    if (upward and row.open >= level) or (not upward and row.open <= level):
        return row.open
    if row.low <= level <= row.high:
        return level
    return None


def protective_exit(
    row: Candle,
    position: SpotPosition,
    risk: SpotRisk,
    *,
    entry_bar: bool = False,
) -> ExitDecision | None:
    """Prioritize stops over profits and forbid ambiguous entry-bar profits.

    Returns:
        The first executable protective exit, if any.

    """
    direction = 1 if position.side == "Long" else -1
    stop = position.price * (1 - direction * risk.stop_loss_pct / 100)
    if math.isfinite(position.stop):
        stop = position.stop
    target = position.price * (1 + direction * risk.take_profit_pct / 100)
    adverse = min if position.side == "Long" else max
    favorable = max if position.side == "Long" else min
    stop_touched = (
        row.low <= stop if position.side == "Long" else row.high >= stop
    )
    target_touched = (
        row.high >= target if position.side == "Long" else row.low <= target
    )
    if (
        risk.stop_loss_pct > 0 or math.isfinite(position.stop)
    ) and stop_touched:
        return ExitDecision(
            price=stop if entry_bar else adverse(stop, row.open),
            reason="Stop loss",
        )
    if not entry_bar and risk.take_profit_pct > 0 and target_touched:
        return ExitDecision(
            price=favorable(target, row.open), reason="Take profit"
        )
    return None


@dataclass(kw_only=True)
class SpotAccount:
    """Own cash, the optional position, and completed trade records."""

    cash: float
    fee_pct: float
    slippage_bps: float = 0.0
    position: SpotPosition | None = None
    trades: list[Trade] = field(default_factory=list)

    def open(self, decision: EntryDecision, timestamp: pd.Timestamp) -> None:
        """Allocate cash to the chosen entry after charging its fee."""
        direction = 1 if decision.side == "Long" else -1
        price = decision.price * (1 + direction * self.slippage_bps / 10000)
        self.position = SpotPosition(
            side=decision.side,
            price=price,
            stop=decision.stop,
            time=timestamp,
            capital=self.cash,
            quantity=(self.cash - self.cash * self.fee_pct / 100) / price,
        )
        self.cash = 0.0

    def close(self, decision: ExitDecision, timestamp: pd.Timestamp) -> None:
        """Settle the open position and record its realized result."""
        position = self.position
        if position is None:
            return
        direction = -1 if position.side == "Long" else 1
        decision = replace(
            decision,
            price=decision.price * (1 + direction * self.slippage_bps / 10000),
        )
        exposure = position.quantity * decision.price
        exit_fee = exposure * self.fee_pct / 100
        if position.side == "Long":
            self.cash = exposure - exit_fee
            pnl = self.cash - position.capital
        else:
            pnl = (
                position.quantity * (position.price - decision.price)
                - position.capital * self.fee_pct / 100
                - exit_fee
            )
            self.cash = position.capital + pnl
        self.trades.append(
            Trade(
                deposit_size=position.capital,
                side=position.side,
                entry_time=position.time,
                exit_time=timestamp,
                entry_price=position.price,
                exit_price=decision.price,
                quantity=position.quantity,
                pnl=pnl,
                return_pct=pnl / position.capital * 100,
                exit_reason=decision.description(),
            )
        )
        self.position = None

    def mark(self, price: float) -> AccountMark:
        """Value the account at the current closing price.

        Returns:
            Equity and signed position quantity before any future exit fee.

        """
        position = self.position
        if position is None:
            return AccountMark(equity=self.cash, quantity=0.0)
        if position.side == "Long":
            return AccountMark(
                equity=position.quantity * price, quantity=position.quantity
            )
        return AccountMark(
            equity=position.capital * (1 - self.fee_pct / 100)
            + position.quantity * (position.price - price),
            quantity=-position.quantity,
        )


@dataclass(frozen=True, kw_only=True)
class AccountMark:
    """Keep account valuation and signed exposure distinct."""

    equity: float
    quantity: float


@dataclass(kw_only=True)
class SpotEngine:
    """Apply trading decisions in candle order with explicit action priority."""

    account: SpotAccount
    risk: SpotRisk
    signals: CrossoverSignals | BandSignals | OptionsGexSignals
    history: SignalHistory = field(default_factory=SignalHistory)

    def process(self, row: Candle, *, session_start: bool) -> None:
        """Apply exits, entries, and protective checks in execution order."""
        if session_start:
            self.history = SignalHistory()
        closed = self._exit_existing(row)
        if self.account.position is None and not closed:
            self._enter(row)
        self.history.close = row.close
        self.history.vwap = self.signals.reference(row)

    def _exit_existing(self, row: Candle) -> bool:
        position = self.account.position
        if position is None:
            return False
        decision = None
        if isinstance(self.signals, OptionsGexSignals):
            opening = replace(row, high=row.open, low=row.open, close=row.open)
            decision = protective_exit(opening, position, self.risk)
            if decision is None:
                decision = self.signals.exit(opening, position, self.history)
        if decision is None:
            decision = protective_exit(row, position, self.risk)
        if decision is None:
            decision = self.signals.exit(row, position, self.history)
        if decision is None:
            return False
        self.account.close(decision, row.time)
        self.history.reentry_ready = True
        return True

    def _enter(self, row: Candle) -> None:
        reference = self.signals.reference(row)
        if math.isnan(reference) and not isinstance(
            self.signals, OptionsGexSignals
        ):
            return
        if row.close < reference:
            self.history.reentry_ready = True
        if (
            self.account.cash < self.risk.minimum_order_size
            or self.account.cash <= 0
        ):
            return
        decision = self.signals.entry(row, self.history)
        if decision is None:
            return
        self.account.open(decision, row.time)
        self.history.reentry_ready = False
        position = self.account.position
        if self.signals.entry_bar_stop and position is not None:
            stop = protective_exit(
                row,
                position,
                self.risk,
                entry_bar=(
                    not isinstance(self.signals, OptionsGexSignals)
                    or row.options_intrabar == 1
                ),
            )
            if stop is not None:
                self.account.close(stop, row.time)
