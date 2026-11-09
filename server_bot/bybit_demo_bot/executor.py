# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of bybit demo bot cli."""

from __future__ import annotations

import logging
import signal
import sys
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, ClassVar, override

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bybit_demo_bot.state import DemoState
from time import monotonic, sleep

import pandas as pd
import requests

from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from bybit_demo_bot.settings import INTERVAL_SECONDS
from live_paper_bot.cli import calculate_strategy_decision
from live_paper_bot.market import fetch_completed_linear_klines
from reference_bot.cli import calculate_strategy_decision as reference_decision
from reference_bot.config import (
    BYBIT_TICKERS,
    PaperBotConfig,
)
from shared.indicators import add_launch_weekly_anchored_vwap
from shared.validation import object_mapping

HALT_LOG_INTERVAL_SECONDS = 60


LOGGER = logging.getLogger(__name__)


class UnprofitableTakeProfitError(RuntimeError):
    """The current VWAP target is on the loss side of the actual fill."""


class BybitDemoBot:
    """Represent bybit demo bot."""

    MAX_RETRY_SECONDS: ClassVar[int] = 300
    REFERENCE_ALIGNED: ClassVar[bool] = False
    EXECUTOR_NAME: ClassVar[str] = "Bybit DEMO"
    ACCOUNT_NAME: ClassVar[str] = "Bybit demo"

    def __init__(
        self, config: PaperBotConfig, client: BybitDemoClient, state: DemoState
    ) -> None:
        """Initialize the instance.

        Raises:
            ValueError: If the requested operation cannot safely proceed.

        """
        if (
            config.data_source != "Bybit REST"
            or config.reverse_ticker
            or config.ticker not in BYBIT_TICKERS
        ):
            message = (
                "Bybit demo requires a non-reversed Bybit REST ticker from"
                f" {sorted(BYBIT_TICKERS)}"
            )
            raise ValueError(message)
        if config.leverage > 1:
            message = (
                "Leveraged configs currently support local Lima paper"
                " simulation only; exchange demo/mainnet execution is not"
                " supported."
            )
            raise ValueError(message)
        self.config: PaperBotConfig = config
        self.client: BybitDemoClient = client
        self.state: DemoState = state
        self.symbol: str = config.ticker.replace("_", "")
        self.running: bool = True
        self._candles: pd.DataFrame = pd.DataFrame()

    def stop(self, *_: object) -> None:
        """Stop."""
        self.running = False

    @staticmethod
    def _floor(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    def order_quantity(self, price: Decimal) -> Decimal:
        """Calculate entry quantity on the exchange quantity grid.

        Returns:
            The calculated decimal amount.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        info = self.client.instruments(self.symbol)
        limits = object_mapping(info["lotSizeFilter"])
        step, minimum = Decimal(str(limits["qtyStep"])), Decimal(
            str(limits["minOrderQty"])
        )
        minimum_notional = Decimal(str(limits.get("minNotionalValue", "0")))
        # Size the funded demo account from its live available USDT instead of
        # the paper strategy's initial capital. Keep 10% in reserve for fees,
        # funding, losses, and exchange rounding.
        allocation = self.client.available_usdt() * Decimal("0.90")
        quantity = self._floor(allocation / price, step)
        required_notional = max(
            minimum_notional, Decimal(str(self.config.minimum_order_size))
        )
        if quantity < minimum or quantity * price < required_notional:
            message = (
                f"{self.ACCOUNT_NAME} order is below configured or exchange"
                " minimum"
            )
            raise RuntimeError(message)
        return quantity

    def exit_band(
        self, candles: pd.DataFrame, launched: pd.Timestamp, side: str
    ) -> Decimal:
        """Calculate the current VWAP exit band for a position side.

        Returns:
            The calculated decimal amount.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        anchor = (
            launched - timedelta(days=self.config.anchor_before_days)
            if self.config.anchor_before_strategy_start
            else launched
        )
        enriched = add_launch_weekly_anchored_vwap(
            candles, anchor, self.config.vwap_anchor_reset_weeks
        )
        vwap_value = (
            pd.to_numeric(enriched["anchored_vwap"]).astype(float).tolist()[-1]
        )
        std_value = (
            pd.to_numeric(enriched["anchored_std"]).astype(float).tolist()[-1]
        )
        if pd.isna(vwap_value) or pd.isna(std_value):
            message = "VWAP close band is unavailable for take profit"
            raise RuntimeError(message)
        vwap, std = Decimal(str(vwap_value)), Decimal(str(std_value))
        sigma = Decimal(str(self.config.close_order_vwap_sigma))
        if self.config.trend:
            return vwap + std * sigma if side == "Buy" else vwap - std * sigma
        return vwap - std * sigma if side == "Buy" else vwap + std * sigma

    def protect_position(
        self, position: Mapping[str, object], take_profit: Decimal
    ) -> str:
        """Apply exchange protection to the confirmed position.

        Returns:
            The protect position result.

        Raises:
            UnprofitableTakeProfitError: If the target would lock in a loss.

        """
        side = str(position["side"])
        self.state.observed_position_side = side
        entry = Decimal(str(position["avgPrice"]))
        loss = Decimal(str(self.config.stop_loss_pct)) / 100
        stop = entry * (1 - loss if side == "Buy" else 1 + loss)
        if not self._take_profit_is_profitable(side, entry, take_profit):
            message = (
                f"VWAP close-band take profit {take_profit} is not profitable"
                f" from {side} entry {entry}"
            )
            raise UnprofitableTakeProfitError(message)
        self.client.set_protection(self.symbol, stop, take_profit)
        self.state.pending_protection_side = None
        self.state.pending_take_profit = None
        self.state.save()
        return f"{side} position protected"

    @staticmethod
    def _take_profit_is_profitable(
        side: str, entry: Decimal, take_profit: Decimal
    ) -> bool:
        return take_profit > entry if side == "Buy" else take_profit < entry

    def reconcile_once(self, now: datetime | None = None) -> str | None:
        """Reconcile once.

        Returns:
            The action description, or None when no action is needed.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        if self.state.halted_reason:
            message = f"trading halted: {self.state.halted_reason}"
            raise RuntimeError(message)
        # A newly submitted order must be reconciled immediately, without
        # waiting for another completed strategy candle.
        if self.state.pending_protection_side:
            position = self.client.position(self.symbol)
        else:
            position = None
        if self.state.pending_protection_side and position:
            return self._reconcile_pending_position(position)
        if self.REFERENCE_ALIGNED and self.state.pending_protection_side:
            return (
                "awaiting entry reconciliation; no additional order submitted"
            )
        current = pd.Timestamp(now or datetime.now(UTC))
        interval = INTERVAL_SECONDS[self.config.timeframe]
        end_ms = int(current.timestamp() * 1000)
        latest_ms = (
            end_ms // (interval * 1000) * interval * 1000 - interval * 1000
        )
        latest = pd.Timestamp(latest_ms, unit="ms", tz="UTC")
        if self.state.last_processed_candle == latest.isoformat():
            return None
        launched = pd.Timestamp(self.state.launched_at)
        if self.REFERENCE_ALIGNED and latest <= launched:
            # A fresh reference start can be later than the latest closed
            # candle.
            # Wait normally instead of backing off on an invalid backtest range.
            return None
        candles = self._load_strategy_history(latest, end_ms, launched)
        if position is None:
            position = self.client.position(self.symbol)
        if self._observe_exchange_position(position, latest):
            return (
                "exchange closure observed; waiting for next completed"
                " candle before entry"
            )
        decision = (
            reference_decision
            if self.REFERENCE_ALIGNED
            else calculate_strategy_decision
        )(candles, self.config, launched)
        desired = (
            "Buy"
            if decision.side == "Long"
            else "Sell" if decision.side == "Short" else None
        )
        if self.REFERENCE_ALIGNED:
            result = self.reconcile_reference_position(
                position, desired, latest
            )
        else:
            result = self._reconcile_legacy_position(
                position,
                desired,
                candles=candles,
                launched=launched,
                latest=latest,
            )
        return result

    def _close_unprotected_position(
        self, position: Mapping[str, object]
    ) -> None:
        """Submit a reducing close after entry protection fails."""
        _ = self.client.market_order(
            self.symbol,
            "Sell" if position["side"] == "Buy" else "Buy",
            Decimal(str(position["size"])),
            reduce_only=True,
        )

    def _entry_protection_target(self) -> Decimal:
        """Validate the saved target before protecting a reconciled fill.

        Returns:
            The legacy VWAP target, or zero for reference-controlled exits.

        Raises:
            RuntimeError: If a legacy entry has no saved VWAP target.

        """
        if self.REFERENCE_ALIGNED:
            return Decimal(0)
        if self.state.pending_take_profit is None:
            message = "pending VWAP take-profit price is missing"
            raise RuntimeError(message)
        return Decimal(self.state.pending_take_profit)

    def _reconcile_pending_position(
        self, position: Mapping[str, object]
    ) -> str:
        """Protect a filled entry or persist the emergency-close outcome.

        Returns:
            The reconcile pending position result.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        try:
            return self.protect_position(
                position, self._entry_protection_target()
            )
        except UnprofitableTakeProfitError as exc:
            try:
                self._close_unprotected_position(position)
            except Exception as close_exc:
                LOGGER.exception("Emergency position close failed")
                self.state.halted_reason = (
                    f"invalid entry protection ({exc}); emergency close"
                    f" failed ({close_exc})"
                )
                self.state.save()
                raise RuntimeError(self.state.halted_reason) from exc
            self.state.pending_protection_side = None
            self.state.pending_take_profit = None
            self.state.save()
            return (
                f"invalid entry protection ({exc}); emergency close"
                " submitted; waiting for next candle"
            )
        except Exception as exc:
            try:
                self._close_unprotected_position(position)
            except Exception as close_exc:
                LOGGER.exception("Emergency position close failed")
                self.state.halted_reason = (
                    f"protection failed ({exc}); emergency close failed"
                    f" ({close_exc})"
                )
                self.state.save()
                raise RuntimeError(self.state.halted_reason) from exc
            self.state.halted_reason = (
                f"protection failed ({exc}); emergency close submitted"
            )
            self.state.save()
            raise RuntimeError(self.state.halted_reason) from exc

    def _load_strategy_history(
        self, latest: pd.Timestamp, end_ms: int, launched: pd.Timestamp
    ) -> pd.DataFrame:
        """Refresh cached candles and require the latest completed candle.

        Returns:
            The validated candle observations.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        interval = INTERVAL_SECONDS[self.config.timeframe]
        start = (
            launched - timedelta(days=self.config.anchor_before_days)
            if self.config.anchor_before_strategy_start
            else launched
        )
        start_ms = int(start.timestamp() * 1000)
        request_start_ms = start_ms
        if not self._candles.empty:
            cached_latest_ms = int(
                pd.to_datetime(self._candles["time"], utc=True)
                .iloc[-1]
                .timestamp()
                * 1000
            )
            # Re-fetch one candle at the boundary so an earlier provisional
            # response can be corrected, while avoiding a full-history burst.
            request_start_ms = max(start_ms, cached_latest_ms - interval * 1000)
        downloaded = fetch_completed_linear_klines(
            self.symbol,
            self.config.timeframe,
            start_ms=request_start_ms,
            end_ms=end_ms,
        )
        if self._candles.empty:
            candles = downloaded
        else:
            candles = pd.concat((self._candles, downloaded), ignore_index=True)
            candles = (
                candles.drop_duplicates(subset="time", keep="last")
                .sort_values("time")
                .reset_index(drop=True)
            )
            candles = candles[
                pd.to_datetime(candles["time"], utc=True) >= start
            ].reset_index(drop=True)
        if (
            candles.empty
            or pd.to_datetime(candles["time"], utc=True).iloc[-1] != latest
        ):
            message = "completed Bybit candle history is not current"
            raise RuntimeError(message)
        self._candles = candles
        return candles

    def _observe_exchange_position(
        self, position: Mapping[str, object] | None, latest: pd.Timestamp
    ) -> bool:
        """Persist exchange closures and consume a reference exit candle.

        Returns:
            Whether the condition holds.

        """
        observed = str(position["side"]) if position else None
        exchange_closed = bool(
            self.state.observed_position_side and observed is None
        )
        if exchange_closed:
            _ = sys.stdout.write(
                str(
                    f"{datetime.now(UTC).isoformat()} observed"
                    f" {self.state.observed_position_side} position closed on"
                    " exchange; consult execution history for fill and exit"
                    " reason"
                )
                + "\n"
            )
            _ = sys.stdout.flush()
        self.state.observed_position_side = observed
        if self.REFERENCE_ALIGNED:
            if exchange_closed and not self.state.pending_strategy_close:
                # Reference spends this candle closing its local position. Do
                # not
                # use that same candle to enter again after an exchange-side
                # exit.
                # Save the observation and cursor together so resume keeps the
                # wait.
                self.state.last_processed_candle = latest.isoformat()
                self.state.consumed_signal_side = None
                self.state.save()
                return True
            if observed is None:
                self.state.pending_strategy_close = False
        self.state.save()
        return False

    def _reconcile_legacy_position(
        self,
        position: Mapping[str, object] | None,
        desired: str | None,
        *,
        candles: pd.DataFrame,
        launched: pd.Timestamp,
        latest: pd.Timestamp,
    ) -> str | None:
        """Apply legacy signal consumption, exits and VWAP protection.

        Returns:
            The action description, or None when no action is needed.

        """
        existing = str(position["side"]) if position else None
        if self.state.consumed_signal_side == "legacy":
            self.state.consumed_signal_side = desired
        elif self.state.consumed_signal_side != desired:
            self.state.consumed_signal_side = None
        if position and existing == desired:
            self.state.consumed_signal_side = desired
        self.state.save()
        actions: list[str] = []
        if position and existing != desired:
            _ = self.client.market_order(
                self.symbol,
                "Sell" if existing == "Buy" else "Buy",
                Decimal(str(position["size"])),
                reduce_only=True,
            )
            actions.append(f"closed {existing}")
            position = None
            self.state.observed_position_side = None
            self.state.save()
        if (
            desired
            and not position
            and self.state.consumed_signal_side == desired
        ):
            actions.append(
                f"skipped {desired} re-entry: waiting for strategy signal"
                " change"
            )
        elif desired and not position:
            price = self.client.last_price(self.symbol)
            take_profit = self.exit_band(candles, launched, desired)
            if not self._take_profit_is_profitable(desired, price, take_profit):
                actions.append(
                    f"skipped {desired} entry: VWAP close-band take profit"
                    f" {take_profit} is not profitable from current price"
                    f" {price}"
                )
                desired = None
            else:
                quantity = self.order_quantity(price)
                # Persist before submission: an ambiguous API failure must not
                # result in a duplicate entry on the same signal.
                self.state.consumed_signal_side = desired
                self.state.pending_protection_side = desired
                self.state.pending_take_profit = format(take_profit, "f")
                self.state.save()
                _ = self.client.market_order(self.symbol, desired, quantity)
                actions.append(
                    f"opened {desired}; awaiting fill for protection"
                )
        elif position and desired is not None and desired == existing:
            take_profit = self.exit_band(candles, launched, desired)
            entry = Decimal(str(position["avgPrice"]))
            if self._take_profit_is_profitable(desired, entry, take_profit):
                _ = self.protect_position(position, take_profit)
                actions.append(
                    f"updated {desired} protection to current VWAP close band"
                )
            else:
                actions.append(
                    f"kept existing {desired} protection: current VWAP close"
                    f" band {take_profit} is not profitable from entry {entry}"
                )
        self.state.last_processed_candle = latest.isoformat()
        self.state.save()
        return "; ".join(actions) or None

    def reconcile_reference_position(
        self,
        position: Mapping[str, object] | None,
        desired: str | None,
        latest: pd.Timestamp,
    ) -> str | None:
        """Reject reference reconciliation for a legacy executor.

        Raises:
            RuntimeError: If reference mode is selected on a legacy executor.

        """
        del position, desired, latest
        message = (
            f"{self.EXECUTOR_NAME}: reference position reconciliation requires"
            " a reference executor"
        )
        raise RuntimeError(message)

    @staticmethod
    def _log_reconciliation_action(
        action: str | None, previous_action: str | None, last_logged: float
    ) -> float:
        """Throttle repeated flat halts while keeping other actions visible.

        Returns:
            The timestamp of the latest emitted action log.

        """
        log_now = monotonic()
        repeated_flat_halt = (
            action == previous_action
            and action is not None
            and action.startswith("trading halted:")
            and "exchange position flat" in action
        )
        if action and (
            not repeated_flat_halt
            or log_now - last_logged >= HALT_LOG_INTERVAL_SECONDS
        ):
            _ = sys.stdout.write(
                str(f"{datetime.now(UTC).isoformat()} {action}") + "\n"
            )
            _ = sys.stdout.flush()
            last_logged = log_now
        return last_logged

    def run(self, poll_seconds: int) -> None:
        """Run."""
        _ = signal.signal(signal.SIGTERM, self.stop)
        _ = signal.signal(signal.SIGINT, self.stop)
        _ = sys.stdout.write(
            str(f"{self.EXECUTOR_NAME} executor started: {self.symbol}") + "\n"
        )
        _ = sys.stdout.flush()
        retry_seconds = poll_seconds
        last_action = None
        last_action_log = float("-inf")
        while self.running:
            try:
                action = self.reconcile_once()
                last_action_log = self._log_reconciliation_action(
                    action, last_action, last_action_log
                )
                last_action = action
                retry_seconds = poll_seconds
            except (
                BybitDemoError,
                requests.RequestException,
                RuntimeError,
                ValueError,
                KeyError,
            ) as exc:
                if "rate limit" in str(exc).lower() or "HTTP 429" in str(exc):

                    retry_seconds = max(retry_seconds, 60)
                _ = sys.stdout.write(
                    str(
                        f"{datetime.now(UTC).isoformat()} reconciliation"
                        f" error; retrying in {retry_seconds}s: {exc}"
                    )
                    + "\n"
                )
                _ = sys.stdout.flush()
                wait_seconds = retry_seconds
                retry_seconds = min(retry_seconds * 2, self.MAX_RETRY_SECONDS)
            else:
                wait_seconds = (
                    1 if self.state.pending_protection_side else poll_seconds
                )
            deadline = monotonic() + wait_seconds
            while self.running and monotonic() < deadline:
                sleep(min(1, deadline - monotonic()))


class ReferenceAlignedDemoBot(BybitDemoBot):
    """Follow reference candle decisions with exchange stop protection."""

    REFERENCE_ALIGNED: ClassVar[bool] = True

    @override
    def protect_position(
        self, position: Mapping[str, object], take_profit: Decimal
    ) -> str:
        del take_profit
        entry = Decimal(str(position["avgPrice"]))
        loss = Decimal(str(self.config.stop_loss_pct)) / 100
        stop = (
            entry * (1 - loss if position["side"] == "Buy" else 1 + loss)
            if loss > 0
            else Decimal(0)
        )
        # Zero removes an exchange TP left by the previous executor.
        self.client.set_protection(self.symbol, stop, Decimal(0))
        self.state.observed_position_side = str(position["side"])
        self.state.pending_protection_side = None
        self.state.pending_take_profit = None
        self.state.save()
        return "position protected with configured SL; exchange TP disabled"

    @override
    def reconcile_reference_position(
        self,
        position: Mapping[str, object] | None,
        desired: str | None,
        latest: pd.Timestamp,
    ) -> str | None:
        """Reconcile reference position.

        Returns:
            The action description, or None when no action is needed.

        """
        existing = str(position["side"]) if position else None
        if position and existing != desired:
            # Distinguish our own strategy exits/reversals from exchange stops.
            # Persist before submission because the response may be ambiguous.
            self.state.pending_strategy_close = True
            self.state.save()
            _ = self.client.market_order(
                self.symbol,
                "Sell" if existing == "Buy" else "Buy",
                Decimal(str(position["size"])),
                reduce_only=True,
            )
            # Confirm closure before sizing a reversal; retry on the same
            # candle.
            return (
                f"submitted close {existing}; awaiting exchange reconciliation"
            )
        if position:
            self.state.pending_strategy_close = False
            result = self.protect_position(position, Decimal(0))
        elif desired:
            quantity = self.order_quantity(self.client.last_price(self.symbol))
            # Persist intent before submission to prevent duplicate ambiguous
            # orders.
            self.state.pending_protection_side = desired
            self.state.pending_take_profit = "0"
            self.state.last_processed_candle = latest.isoformat()
            self.state.save()
            _ = self.client.market_order(self.symbol, desired, quantity)
            return f"opened {desired}; awaiting fill for protection"
        else:
            result = None
        self.state.consumed_signal_side = None
        self.state.last_processed_candle = latest.isoformat()
        self.state.save()
        return result
