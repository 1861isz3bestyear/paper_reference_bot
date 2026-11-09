# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of bybit demo bot cli."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import signal
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pandas as pd
import requests

from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from live_paper_bot.cli import calculate_strategy_decision
from live_paper_bot.market import fetch_completed_linear_klines
from reference_bot.cli import calculate_strategy_decision as reference_decision
from reference_bot.config import (
    BYBIT_TICKERS,
    PAPER_BOT_CONFIG_FILE,
    PaperBotConfig,
)
from server_bot.tunnel_runtime import archive_while_running
from shared.env import load_env, setting
from shared.indicators import add_launch_weekly_anchored_vwap

LOGGER = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "bybitdemoapi.env"
if not ENV_FILE.is_file():
    ENV_FILE = ROOT / "bybitapidemo.env"
STATE_FILE = ROOT / "bybit_demo_state.json"
LOCK_FILE = ROOT / "bybit_demo_bot.instance.lock"
INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


class UnprofitableTakeProfit(RuntimeError):
    """The current VWAP target is on the loss side of the actual fill."""


@dataclass
class DemoState:
    """Represent demo state."""

    launched_at: str
    last_processed_candle: str | None = None
    hedge_state: dict | None = None
    pending_protection_side: str | None = None
    pending_take_profit: str | None = None
    halted_reason: str | None = None
    consumed_signal_side: str | None = None
    observed_position_side: str | None = None
    pending_strategy_close: bool = False
    replica_initialized: bool = False
    replica_position_id: str | None = None
    replica_order: dict | None = None

    @classmethod
    def load_or_create(cls, resume: bool) -> DemoState:
        """Load or create."""
        if resume and STATE_FILE.is_file():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                # Old state cannot tell whether the current signal already traded.
                if "consumed_signal_side" not in data and data.get(
                    "last_processed_candle"
                ):
                    data["consumed_signal_side"] = "legacy"
                return cls(**data)
            except (OSError, TypeError, json.JSONDecodeError) as exc:
                message = f"Cannot read {STATE_FILE.name}: {exc}"
                raise RuntimeError(message) from None
        state = cls(datetime.now(UTC).isoformat())
        state.save()
        return state

    def save(self) -> None:
        """Save."""
        temporary = STATE_FILE.with_suffix(".json.tmp")
        _ = temporary.write_text(
            json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8"
        )
        _ = Path(temporary).replace(STATE_FILE)


class BybitDemoBot:
    """Represent bybit demo bot."""

    MAX_RETRY_SECONDS = 300
    REFERENCE_ALIGNED = False
    EXECUTOR_NAME = "Bybit DEMO"
    ACCOUNT_NAME = "Bybit demo"

    def __init__(
        self, config: PaperBotConfig, client: BybitDemoClient, state: DemoState
    ) -> None:
        """Initialize the instance."""
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
        self.config, self.client, self.state = config, client, state
        self.symbol, self.running = config.ticker.replace("_", ""), True
        self._candles = pd.DataFrame()

    def stop(self, *_: object) -> None:
        """Stop."""
        self.running = False

    @staticmethod
    def _floor(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    def _quantity(self, price: Decimal) -> Decimal:
        info = self.client.instruments(self.symbol)
        limits = info["lotSizeFilter"]
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

    def _exit_band(
        self, candles: pd.DataFrame, launched: pd.Timestamp, side: str
    ) -> Decimal:
        anchor = (
            launched - timedelta(days=self.config.anchor_before_days)
            if self.config.anchor_before_strategy_start
            else launched
        )
        row = add_launch_weekly_anchored_vwap(
            candles, anchor, self.config.vwap_anchor_reset_weeks
        ).iloc[-1]
        vwap, std = Decimal(str(row["anchored_vwap"])), Decimal(
            str(row["anchored_std"])
        )
        sigma = Decimal(str(self.config.close_order_vwap_sigma))
        if pd.isna(row["anchored_vwap"]) or pd.isna(row["anchored_std"]):
            message = "VWAP close band is unavailable for take profit"
            raise RuntimeError(message)
        if self.config.trend:
            return vwap + std * sigma if side == "Buy" else vwap - std * sigma
        return vwap - std * sigma if side == "Buy" else vwap + std * sigma

    def _protect(
        self, position: dict[str, object], take_profit: Decimal
    ) -> str:
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
            raise UnprofitableTakeProfit(message)
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
        """Reconcile once."""
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
            try:
                if self.REFERENCE_ALIGNED:
                    return self._protect(position, Decimal(0))
                if self.state.pending_take_profit is None:
                    message = "pending VWAP take-profit price is missing"
                    raise RuntimeError(message)
                return self._protect(
                    position, Decimal(self.state.pending_take_profit)
                )
            except UnprofitableTakeProfit as exc:
                try:
                    _ = self.client.market_order(
                        self.symbol,
                        "Sell" if position["side"] == "Buy" else "Buy",
                        Decimal(str(position["size"])),
                        reduce_only=True,
                    )
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
                    _ = self.client.market_order(
                        self.symbol,
                        "Sell" if position["side"] == "Buy" else "Buy",
                        Decimal(str(position["size"])),
                        reduce_only=True,
                    )
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
            # A fresh reference start can be later than the latest closed candle.
            # Wait normally instead of backing off on an invalid backtest range.
            return None
        start = (
            launched - timedelta(days=self.config.anchor_before_days)
            if self.config.anchor_before_strategy_start
            else launched
        )
        start_ms = int(start.timestamp() * 1000)
        request_start_ms = start_ms
        if not self._candles.empty:
            cached_latest_ms = int(
                pd.Timestamp(self._candles.iloc[-1]["time"]).timestamp() * 1000
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
        if candles.empty or pd.Timestamp(candles.iloc[-1]["time"]) != latest:
            message = "completed Bybit candle history is not current"
            raise RuntimeError(message)
        self._candles = candles
        if position is None:
            position = self.client.position(self.symbol)
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
            sys.stdout.flush()
        self.state.observed_position_side = observed
        if self.REFERENCE_ALIGNED:
            if exchange_closed and not self.state.pending_strategy_close:
                # Reference spends this candle closing its local position. Do not
                # use that same candle to enter again after an exchange-side exit.
                # Save the observation and cursor together so resume keeps the wait.
                self.state.last_processed_candle = latest.isoformat()
                self.state.consumed_signal_side = None
                self.state.save()
                return (
                    "exchange closure observed; waiting for next completed"
                    " candle before entry"
                )
            if observed is None:
                self.state.pending_strategy_close = False
        self.state.save()
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
        existing = str(position["side"]) if position else None
        if self.REFERENCE_ALIGNED:
            return self.reconcile_reference_position(position, desired, latest)
        if self.state.consumed_signal_side == "legacy":
            self.state.consumed_signal_side = desired
        elif self.state.consumed_signal_side != desired:
            self.state.consumed_signal_side = None
        if position and existing == desired:
            self.state.consumed_signal_side = desired
        self.state.save()
        actions = []
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
            take_profit = self._exit_band(candles, launched, desired)
            if not self._take_profit_is_profitable(desired, price, take_profit):
                actions.append(
                    f"skipped {desired} entry: VWAP close-band take profit"
                    f" {take_profit} is not profitable from current price"
                    f" {price}"
                )
                desired = None
            else:
                quantity = self._quantity(price)
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
        elif position and desired == existing:
            take_profit = self._exit_band(candles, launched, desired)
            entry = Decimal(str(position["avgPrice"]))
            if self._take_profit_is_profitable(desired, entry, take_profit):
                _ = self._protect(position, take_profit)
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

    def run(self, poll_seconds: int) -> None:
        """Run."""
        _ = signal.signal(signal.SIGTERM, self.stop)
        _ = signal.signal(signal.SIGINT, self.stop)
        _ = sys.stdout.write(
            str(f"{self.EXECUTOR_NAME} executor started: {self.symbol}") + "\n"
        )
        sys.stdout.flush()
        retry_seconds = poll_seconds
        last_action = None
        last_action_log = float("-inf")
        while self.running:
            try:
                action = self.reconcile_once()
                log_now = time.monotonic()
                repeated_flat_halt = (
                    action == last_action
                    and action is not None
                    and action.startswith("trading halted:")
                    and "exchange position flat" in action
                )
                if action and (
                    not repeated_flat_halt or log_now - last_action_log >= 60
                ):
                    _ = sys.stdout.write(
                        str(f"{datetime.now(UTC).isoformat()} {action}") + "\n"
                    )
                    sys.stdout.flush()
                    last_action_log = log_now
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
                sys.stdout.flush()
                wait_seconds = retry_seconds
                retry_seconds = min(retry_seconds * 2, self.MAX_RETRY_SECONDS)
            else:
                wait_seconds = (
                    1 if self.state.pending_protection_side else poll_seconds
                )
            deadline = time.monotonic() + wait_seconds
            while self.running and time.monotonic() < deadline:
                time.sleep(min(1, deadline - time.monotonic()))


class ReferenceAlignedDemoBot(BybitDemoBot):
    """Reference candle decisions with exchange SL; mainnet keeps legacy behavior."""

    REFERENCE_ALIGNED = True

    def _protect(
        self, position: dict[str, object], take_profit: Decimal
    ) -> str:
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

    def reconcile_reference_position(
        self, position, desired, latest
    ) -> str | None:
        """Reconcile reference position."""
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
            # Confirm closure before sizing a reversal; retry on the same candle.
            return (
                f"submitted close {existing}; awaiting exchange reconciliation"
            )
        if position:
            self.state.pending_strategy_close = False
            result = self._protect(position, Decimal(0))
        elif desired:
            quantity = self._quantity(self.client.last_price(self.symbol))
            # Persist intent before submission to prevent duplicate ambiguous orders.
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


def run_demo_command(
    config_path: Path,
    env_path: Path,
    *,
    resume: bool = False,
    poll_seconds: int = 10,
) -> None:
    """Run demo command."""
    if poll_seconds < 1:
        message = "poll-seconds must be positive"
        raise ValueError(message)
    config, values = PaperBotConfig.load(config_path), load_env(env_path)
    client = BybitDemoClient(
        setting(values, "BYBIT_API_KEY", values.get("API_KEY")),
        setting(values, "BYBIT_API_SECRET", values.get("SECRET_API_KEY")),
    )
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            message = "Another Bybit demo bot is running"
            raise RuntimeError(message) from None
        state = DemoState.load_or_create(resume)
        from bybit_demo_bot.hedge import HedgeReplicaBot
        from bybit_demo_bot.replica import ReferenceReplicaBot
        from shared.hedge_strategy import HEDGE_MODE

        executor = (
            HedgeReplicaBot
            if config.strategy_mode == HEDGE_MODE
            else ReferenceReplicaBot
        )
        with archive_while_running():
            executor(config, client, state).run(min(poll_seconds, 2))


class CommandArguments(argparse.Namespace):
    """Arguments validated and populated by the command parser."""

    config: Path = PAPER_BOT_CONFIG_FILE
    env: Path = ENV_FILE
    resume: bool = False
    poll_seconds: int = 10


def parse_args() -> CommandArguments:
    """Parse args."""
    parser = argparse.ArgumentParser(
        description="Trade the strategy on a Bybit demo account"
    )
    _ = parser.add_argument("run", choices=("run",))
    _ = parser.add_argument(
        "--config", type=Path, default=PAPER_BOT_CONFIG_FILE
    )
    _ = parser.add_argument("--env", type=Path, default=ENV_FILE)
    _ = parser.add_argument("--resume", action="store_true")
    _ = parser.add_argument("--poll-seconds", type=int, default=10)
    return parser.parse_args(namespace=CommandArguments())


def main() -> None:
    """Main."""
    args = parse_args()
    try:
        run_demo_command(
            args.config,
            args.env,
            resume=args.resume,
            poll_seconds=args.poll_seconds,
        )
    except (BybitDemoError, OSError, RuntimeError, ValueError) as exc:
        message = f"Error: {exc}"
        raise SystemExit(message) from exc


if __name__ == "__main__":
    main()
