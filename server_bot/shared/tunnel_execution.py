# Copyright 2026 anchored-vwam-backtest contributors
"""Execute funded tunnel and insurance legs using existing SL/TP accounting."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pandas as pd

from shared.candles import Candle, candle_rows
from shared.spot_execution import (
    EntryDecision,
    ExitDecision,
    SpotAccount,
    SpotRisk,
    band_fill,
    protective_exit,
)
from shared.strategy_runner import validate_trade_timeline

if TYPE_CHECKING:
    from shared.models import Trade
    from shared.spot_execution import AllowedSide
    from shared.strategy_data import PreparedBacktest


def tunnel_entry_decision(row: Candle, risk: SpotRisk) -> EntryDecision | None:
    """Freeze a negative-GEX long target below its entry-time gamma flip.

    Returns:
        An eligible entry with its target, or none if no profitable room exists.

    """
    if row.options_direction not in {-1, 1}:
        return None
    side = "Long" if row.options_direction == 1 else "Short"
    target = math.nan
    if side == "Long" and math.isfinite(row.options_target_ceiling):
        entry = row.open * (1 + risk.slippage_bps / 10000)
        target = row.options_target_ceiling
        if risk.take_profit_pct > 0:
            target = min(target, entry * (1 + risk.take_profit_pct / 100))
        net_exit = target * (1 - risk.slippage_bps / 10000)
        if net_exit * (1 - risk.fee_pct / 100) ** 2 <= entry:
            return None
    return EntryDecision(side=side, price=row.open, target=target)


@dataclass(frozen=True, kw_only=True)
class TunnelExecutionSettings:
    """Use standard risk settings and explicitly fund both unleveraged legs."""

    risk: SpotRisk
    side: AllowedSide = "Both"
    allow_reentry: bool = True
    use_insurance: bool = True


@dataclass(kw_only=True)
class TunnelEngine:
    """Own two funded positions and a fixed, single-use insurance trigger."""

    main: SpotAccount
    insurance: SpotAccount
    settings: TunnelExecutionSettings
    trigger: float = math.nan
    entered: bool = False

    def process(self, row: Candle) -> None:
        """Apply gap exits, entries, insurance activation and protection."""
        was_open = (
            self.main.position is not None
            or self.insurance.position is not None
        )
        opening = replace(row, high=row.open, low=row.open, close=row.open)
        self._protect(self.main, opening)
        self._protect(self.insurance, opening)
        if self.main.position is None:
            self.trigger = math.nan
        if not was_open:
            self._enter(row)
        self._activate_insurance(row)
        self._protect(self.main, row)
        if (
            self.insurance.position is not None
            and self.insurance.position.time < row.time
        ):
            self._protect(self.insurance, row)
        if self.main.position is None:
            self.trigger = math.nan

    def _activate_insurance(self, row: Candle) -> None:
        """Apply downward exit precedence before triggering insurance."""
        position = self.main.position
        if position is None or not math.isfinite(self.trigger):
            return
        falling = replace(row, high=row.open, close=min(row.open, row.close))
        earlier_exit = protective_exit(falling, position, self.settings.risk)
        if earlier_exit is not None and earlier_exit.price >= self.trigger:
            self.main.close(earlier_exit, row.time)
            self.trigger = math.nan
            return
        price = band_fill(row, self.trigger, upward=False)
        if (
            price is None
            or self.insurance.cash < self.settings.risk.minimum_order_size
        ):
            return
        self.insurance.open(EntryDecision(side="Short", price=price), row.time)
        self.trigger = math.nan
        # A candle cannot establish a profit after a stop entry.
        self._protect(self.insurance, row, entry_bar=True)

    def _enter(self, row: Candle) -> None:
        if self.entered and not self.settings.allow_reentry:
            return
        if row.options_direction not in {-1, 1}:
            return
        if self.settings.use_insurance and not math.isfinite(
            row.options_insurance
        ):
            return
        decision = tunnel_entry_decision(row, self.settings.risk)
        if decision is None or self.settings.side not in {
            "Both",
            decision.side,
        }:
            return
        total = self.main.cash + self.insurance.cash
        capital = total / 2 if self.settings.use_insurance else total
        if capital <= 0 or capital < self.settings.risk.minimum_order_size:
            return
        self.main.cash = capital
        self.insurance.cash = total - capital
        self.main.open(decision, row.time)
        self.trigger = (
            row.options_insurance if self.settings.use_insurance else math.nan
        )
        self.entered = True

    def _protect(
        self, account: SpotAccount, row: Candle, *, entry_bar: bool = False
    ) -> None:
        position = account.position
        if position is None:
            return
        decision = protective_exit(
            row, position, self.settings.risk, entry_bar=entry_bar
        )
        if decision is not None:
            account.close(decision, row.time)

    def close(self, row: Candle, *, session: bool) -> None:
        """Liquidate both legs and cancel an unfilled insurance trigger."""
        decision = ExitDecision(
            price=row.close,
            reason="End of scheduled session" if session else "End of strategy",
        )
        self.main.close(decision, row.time)
        self.insurance.close(decision, row.time)
        self.trigger = math.nan


def run_tunnel_backtest(
    prepared: PreparedBacktest,
    capital: float,
    *,
    settings: TunnelExecutionSettings,
) -> tuple[pd.DataFrame, list[Trade]]:
    """Replay two separately funded legs and combine their marked equity.

    Returns:
        Standard equity rows and labeled completed trades for both legs.

    """
    risk = settings.risk
    engine = TunnelEngine(
        main=SpotAccount(
            cash=capital / 2 if settings.use_insurance else capital,
            fee_pct=risk.fee_pct,
            slippage_bps=risk.slippage_bps,
        ),
        insurance=SpotAccount(
            cash=capital / 2 if settings.use_insurance else 0.0,
            fee_pct=risk.fee_pct,
            slippage_bps=risk.slippage_bps,
        ),
        settings=settings,
    )
    candles = list(candle_rows(prepared.data))
    days = (
        prepared.scheduled_days.tolist()
        if prepared.scheduled_days is not None
        else [None] * len(candles)
    )
    records: list[dict[str, object]] = []
    for index, row in enumerate(candles):
        engine.process(row)
        last = index == len(candles) - 1
        session = days[index] is not None and (
            last or days[index] != days[index + 1]
        )
        if last or session:
            engine.close(row, session=session)
        main = engine.main.mark(row.close)
        insurance = engine.insurance.mark(row.close)
        records.append({
            "time": row.time,
            "equity": main.equity + insurance.equity,
            "position": main.quantity + insurance.quantity,
            "close": row.close,
            "anchored_vwap": row.anchored_vwap,
        })
    trades = [
        *engine.main.trades,
        *(
            replace(trade, exit_reason=f"Insurance: {trade.exit_reason}")
            for trade in engine.insurance.trades
        ),
    ]
    trades.sort(key=lambda item: (item.exit_time, item.entry_time))
    validate_trade_timeline(trades, prepared.window)
    return pd.DataFrame(records), trades
