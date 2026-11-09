# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of reference bot cli."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import requests

from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from reference_bot.market import (
    BybitKlineWebSocket,
    CandleCache,
    RestMarketFeed,
    fetch_completed_linear_klines,
    fetch_completed_mexc_klines,
    fetch_live_price,
    fetch_mexc_live_price,
    fetch_mexc_taker_fee_rate,
    reverse_candles,
)
from reference_bot.trading import (
    PAPER_ACCOUNT_FILE,
    LocalPaperTrader,
    LocalPosition,
    OrderLimits,
    PaperTradeTarget,
    fetch_bybit_order_limits,
    fetch_bybit_taker_fee_rate,
    latest_strategy_side,
    load_bybit_credentials,
)
from server_bot.tunnel_runtime import archive_while_running
from shared.candles import Candle, candle_rows
from shared.hedge_strategy import HEDGE_MODE, process_hedge_history
from shared.hedge_validation import optional_str, parse_hedge_snapshot
from shared.indicators import add_launch_weekly_anchored_vwap
from shared.leverage import MarginEngine, process_margin_history
from shared.margin_validation import parse_margin_snapshot
from shared.reference_target import publish_target
from shared.strategy import backtest
from shared.validation import (
    boolean,
    integer,
    json_object,
    numeric_string,
    object_list,
    object_mapping,
    string,
)

if TYPE_CHECKING:
    from decimal import Decimal

    from shared.hedge_types import HedgeSnapshot
    from shared.margin_types import MarginSnapshot
    from shared.strategy_config import StrategyConfig


ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "reference_state.json"
PID_FILE = ROOT / "reference_bot.pid"
LOG_FILE = ROOT / "reference_bot.log"
CANDLE_CACHE_FILE = ROOT / "reference_candles.sqlite"
INSTANCE_LOCK_FILE = ROOT / "reference_bot.instance.lock"
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
REST_RECONCILE_SECONDS = 30 * 60
MINIMUM_STRATEGY_CANDLES = 2


@dataclass(frozen=True)
class StrategyDecision:
    """Represent strategy decision."""

    side: str | None
    historical_price: float | None
    exit_reason: str | None = None


@dataclass
class BotState:
    """Represent bot state."""

    launched_at: str
    last_processed_candle: str | None = None
    hedge_state: HedgeSnapshot | None = None
    margin_state: MarginSnapshot | None = None
    last_successful_market_sync: str | None = None
    latest_cached_candle: str | None = None
    websocket_connected: bool = False
    last_websocket_message: str | None = None
    websocket_reconnect_count: int = 0
    last_websocket_error: str | None = None

    @classmethod
    def load_or_create(cls, path: Path = STATE_FILE) -> BotState:
        """Load or create.

        Returns:
            The load or create result.

        """
        if path.is_file():
            values = json_object(path.read_text(encoding="utf-8"))
            hedge = values.get("hedge_state")
            margin = values.get("margin_state")
            return cls(
                launched_at=string(values["launched_at"]),
                last_processed_candle=optional_str(
                    values.get("last_processed_candle")
                ),
                hedge_state=(
                    parse_hedge_snapshot(hedge) if hedge is not None else None
                ),
                margin_state=(
                    parse_margin_snapshot(margin)
                    if margin is not None
                    else None
                ),
                last_successful_market_sync=optional_str(
                    values.get("last_successful_market_sync")
                ),
                latest_cached_candle=optional_str(
                    values.get("latest_cached_candle")
                ),
                websocket_connected=boolean(
                    values.get("websocket_connected", False)
                ),
                last_websocket_message=optional_str(
                    values.get("last_websocket_message")
                ),
                websocket_reconnect_count=integer(
                    values.get("websocket_reconnect_count", 0)
                ),
                last_websocket_error=optional_str(
                    values.get("last_websocket_error")
                ),
            )
        state = cls(launched_at=datetime.now(UTC).isoformat())
        state.save(path)
        return state

    def save(self, path: Path = STATE_FILE) -> None:
        """Save."""
        temporary = path.with_suffix(path.suffix + ".tmp")
        _ = temporary.write_text(
            json.dumps(self.__dict__, indent=2) + "\n", encoding="utf-8"
        )
        _ = Path(temporary).replace(path)


def calculate_strategy_decision(
    candles: pd.DataFrame,
    config: StrategyConfig,
    launched_at: pd.Timestamp,
) -> StrategyDecision:
    """Calculate strategy decision.

    Returns:
        The side, execution price and exit reason for this candle.

    Raises:
        ValueError: If the requested operation cannot safely proceed.

    """
    if config.strategy_mode == HEDGE_MODE:
        message = (
            "VWAP zero hedge uses a two-leg plan, not a single target side."
        )
        raise ValueError(message)
    if len(candles) < MINIMUM_STRATEGY_CANDLES:
        return StrategyDecision(None, None)
    anchor_at = launched_at
    if config.anchor_before_strategy_start:
        anchor_at -= timedelta(days=config.anchor_before_days)
    data = add_launch_weekly_anchored_vwap(
        candles, anchor_at, config.vwap_anchor_reset_weeks
    )
    end = pd.to_datetime(data["time"], utc=True).iloc[-1]
    _, trades = backtest(
        data,
        strategy_start_at=launched_at,
        strategy_end_at=end,
        initial_capital=config.initial_capital,
        minimum_order_size=config.minimum_order_size,
        fee_pct=0.0,
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=config.take_profit_pct,
        allow_reentry=config.allow_immediate_reentry,
        strategy_mode=config.strategy_mode,
        band_sigma=config.open_order_vwap_sigma,
        exit_band_sigma=config.close_order_vwap_sigma,
        open_position_side=config.open_position_side,
        trend_mode=config.trend,
    )
    side = latest_strategy_side(trades, end)
    if side is not None:
        open_trade = trades[-1]
        price = (
            float(open_trade.entry_price)
            if pd.Timestamp(open_trade.entry_time) == end
            else None
        )
        return StrategyDecision(side, price)
    if trades:
        closed_trade = trades[-1]
        if (
            pd.Timestamp(closed_trade.exit_time) == end
            and closed_trade.exit_reason != "End of strategy"
        ):
            return StrategyDecision(
                None, float(closed_trade.exit_price), closed_trade.exit_reason
            )
    return StrategyDecision(None, None)


def calculate_target_side(
    candles: pd.DataFrame, config: StrategyConfig, launched_at: pd.Timestamp
) -> str | None:
    """Calculate target side.

    Returns:
        The action description, or None when no action is needed.

    """
    return calculate_strategy_decision(candles, config, launched_at).side


@dataclass(frozen=True, kw_only=True)
class CandleWindow:
    """The required range of completed candles for one market sync."""

    start_ms: int
    end_ms: int
    interval_ms: int

    @property
    def first_ms(self) -> int:
        """The first grid-aligned candle inside the requested range."""
        return (
            (self.start_ms + self.interval_ms - 1) // self.interval_ms
        ) * self.interval_ms

    @property
    def latest_ms(self) -> int:
        """The most recent completed candle before the cutoff."""
        return (
            self.end_ms // self.interval_ms * self.interval_ms
            - self.interval_ms
        )


@dataclass(frozen=True, kw_only=True)
class CandleCoverage:
    """Observed range and continuity of a cached candle frame."""

    first_ms: int | None
    latest_ms: int | None
    has_gap: bool

    @classmethod
    def from_frame(
        cls, frame: pd.DataFrame, interval_ms: int
    ) -> CandleCoverage:
        """Inspect UTC timestamps independently of pandas storage resolution.

        Returns:
            The observed boundaries and whether adjacent candles have gaps.

        """
        if frame.empty:
            return cls(first_ms=None, latest_ms=None, has_gap=False)
        times = (
            pd.to_datetime(frame["time"], utc=True)
            .dt.as_unit("ns")
            .astype("int64")
            // 1_000_000
        )
        return cls(
            first_ms=int(times.iloc[0]),
            latest_ms=int(times.iloc[-1]),
            has_gap=bool(not times.diff().iloc[1:].eq(interval_ms).all()),
        )

    def incomplete(self, window: CandleWindow, *, require_latest: bool) -> bool:
        """Check continuity and the requested completed-candle boundaries.

        Returns:
            Whether the required range is incomplete.

        """
        if window.first_ms > window.latest_ms:
            return False
        if self.first_ms is None or self.latest_ms is None:
            return True
        return (
            self.first_ms > window.first_ms
            or self.has_gap
            or (require_latest and self.latest_ms < window.latest_ms)
        )


class PaperBot:
    """Represent paper bot."""

    def __init__(
        self,
        config_path: Path,
        state_path: Path = STATE_FILE,
        candle_cache_path: Path = CANDLE_CACHE_FILE,
    ) -> None:
        """Initialize the instance."""
        self.config: PaperBotConfig = PaperBotConfig.load(config_path)
        self.state_path: Path = state_path
        self.state: BotState = BotState.load_or_create(state_path)
        self.launched_at: pd.Timestamp = pd.Timestamp(self.state.launched_at)
        self.trader: LocalPaperTrader = LocalPaperTrader()
        self.candle_cache: CandleCache = CandleCache(candle_cache_path)
        self.market_symbol: str = self.config.ticker.replace("_", "")
        base, quote = self.config.ticker.split("_", maxsplit=1)
        self.trade_symbol: str = (
            f"{quote}{base}"
            if self.config.reverse_ticker
            else self.market_symbol
        )
        self.quote_currency: str = base if self.config.reverse_ticker else quote
        self.market_feed: BybitKlineWebSocket | RestMarketFeed = (
            BybitKlineWebSocket(
                self.market_symbol,
                self.config.timeframe,
                lambda candles: self.candle_cache.upsert(
                    self.market_symbol, self.config.timeframe, candles
                ),
            )
            if self.config.data_source == "Bybit REST"
            else RestMarketFeed()
        )
        self._hedge_fee_pct: float | None = None
        self._last_websocket_generation: int = 0
        self.running: bool = True

    def stop(self, *_: object) -> None:
        """Stop."""
        self.running = False

    def _download_history(
        self, window: CandleWindow, request_start_ms: int
    ) -> pd.DataFrame:
        fetcher = (
            fetch_completed_linear_klines
            if self.config.data_source == "Bybit REST"
            else fetch_completed_mexc_klines
        )
        downloaded = fetcher(
            self.market_symbol,
            self.config.timeframe,
            start_ms=request_start_ms,
            end_ms=window.end_ms,
        )
        self.candle_cache.upsert(
            self.market_symbol, self.config.timeframe, downloaded
        )
        return self.candle_cache.load(
            self.market_symbol,
            self.config.timeframe,
            start_ms=window.start_ms,
            end_ms=window.end_ms,
        )

    def load_completed_candles(
        self,
        history_start: pd.Timestamp,
        now: datetime,
        *,
        reconcile_rest: bool = True,
    ) -> pd.DataFrame:
        """Load complete, gap-free observations through the latest candle.

        Returns:
            The validated candle observations.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.
            ValueError: If the requested operation cannot safely proceed.
            RequestException: If the requested operation cannot safely proceed.

        """
        window = CandleWindow(
            start_ms=int(history_start.timestamp() * 1000),
            end_ms=int(now.timestamp() * 1000),
            interval_ms=INTERVAL_MS[self.config.timeframe],
        )
        cached = self.candle_cache.load(
            self.market_symbol,
            self.config.timeframe,
            start_ms=window.start_ms,
            end_ms=window.end_ms,
        )
        coverage = CandleCoverage.from_frame(cached, window.interval_ms)
        has_full_start = (
            coverage.first_ms is not None
            and coverage.first_ms <= window.first_ms
        )
        request_start_ms = window.start_ms
        if has_full_start and coverage.latest_ms is not None:
            request_start_ms = max(
                window.start_ms, coverage.latest_ms - window.interval_ms
            )
        reconcile_rest = (
            reconcile_rest or not has_full_start or coverage.has_gap
        )
        download_succeeded = False
        if reconcile_rest:
            try:
                cached = self._download_history(window, request_start_ms)
            except requests.RequestException, ValueError:
                if (
                    window.first_ms <= window.latest_ms
                    and coverage.latest_ms != window.latest_ms
                ):
                    raise
            else:
                download_succeeded = True
        if cached.empty:
            return cached
        coverage = CandleCoverage.from_frame(cached, window.interval_ms)
        incomplete = coverage.incomplete(window, require_latest=reconcile_rest)
        if incomplete and request_start_ms != window.start_ms:
            cached = self._download_history(window, window.start_ms)
            coverage = CandleCoverage.from_frame(cached, window.interval_ms)
            incomplete = coverage.incomplete(window, require_latest=True)
        if incomplete:
            message = (
                f"{self.config.data_source} candle history is incomplete;"
                " refusing to calculate or advance the cursor."
            )
            raise RuntimeError(message)
        if download_succeeded:
            self.state.last_successful_market_sync = datetime.now(
                UTC
            ).isoformat()
        self.state.latest_cached_candle = (
            pd.to_datetime(cached["time"], utc=True).iloc[-1].isoformat()
        )
        self.state.save(self.state_path)
        return reverse_candles(cached) if self.config.reverse_ticker else cached

    def _update_websocket_health(self) -> tuple[bool, int]:
        health = self.market_feed.health()
        self.state.websocket_connected = health.connected
        self.state.last_websocket_message = health.last_message
        self.state.websocket_reconnect_count = health.reconnect_count
        self.state.last_websocket_error = health.last_error
        return health.connected, health.connection_generation

    def _rest_reconciliation_required(self, now: datetime) -> tuple[bool, int]:
        connected, generation = self._update_websocket_health()
        self.state.save(self.state_path)
        message = (
            pd.Timestamp(self.state.last_websocket_message)
            if self.state.last_websocket_message
            else None
        )
        stale_after = timedelta(
            seconds=max(120, INTERVAL_MS[self.config.timeframe] // 500)
        )
        websocket_healthy = (
            connected
            and message is not None
            and now - message.to_pydatetime() <= stale_after
        )
        last_rest = (
            pd.Timestamp(self.state.last_successful_market_sync)
            if self.state.last_successful_market_sync
            else None
        )
        rest_overdue = (
            last_rest is None
            or now - last_rest.to_pydatetime()
            >= timedelta(seconds=REST_RECONCILE_SECONDS)
        )
        required = (
            self.config.data_source == "MEXC REST"
            or not websocket_healthy
            or rest_overdue
            or generation != self._last_websocket_generation
        )
        return required, generation

    def process_available_candles(self) -> int:
        """Synchronize market history and process new candles in order.

        Returns:
            The number of newly processed strategy candles.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        now = datetime.now(UTC)
        history_start = self.launched_at
        if self.config.anchor_before_strategy_start:
            history_start -= timedelta(days=self.config.anchor_before_days)
        reconcile_rest, generation = self._rest_reconciliation_required(now)
        candles = self.load_completed_candles(
            history_start, now, reconcile_rest=reconcile_rest
        )
        self._last_websocket_generation = generation
        if candles.empty:
            return 0
        if self.config.leverage > 1:
            return self._process_margin_candles(candles)
        if self.state.margin_state is not None:
            message = (
                "Start a fresh paper run before switching from leveraged to"
                " unleveraged accounting."
            )
            raise RuntimeError(message)
        if self.config.strategy_mode == HEDGE_MODE:
            return self._process_hedge_candles(candles)
        if self._hedge_account_active():
            message = "Finish the active hedge cycle before changing strategy."
            raise RuntimeError(message)
        return self._process_one_way_candles(candles)

    def _hedge_account_active(self) -> bool:
        state = self.state.hedge_state
        return bool(state and (state["positions"] or state["pending"]))

    def _process_margin_candles(self, candles: pd.DataFrame) -> int:
        if (
            self.trader.current_position(self.trade_symbol).side is not None
            or self._hedge_account_active()
        ):
            message = (
                "Close existing unleveraged positions before starting leveraged"
                " paper trading."
            )
            raise RuntimeError(message)
        key, secret = load_bybit_credentials()
        fee_pct = (
            float(fetch_bybit_taker_fee_rate(key, secret, self.market_symbol))
            * 100
        )
        count = process_margin_history(
            self.state,
            candles,
            self.config,
            launched_at=self.launched_at,
            fee_pct=fee_pct,
        )
        self.state.save(self.state_path)
        return count

    def _process_hedge_candles(self, candles: pd.DataFrame) -> int:
        if self.trader.current_position(self.trade_symbol).side is not None:
            message = (
                "Close the legacy one-way paper position before starting VWAP"
                " zero hedge."
            )
            raise RuntimeError(message)
        if self._hedge_fee_pct is None:
            key, secret = load_bybit_credentials()
            self._hedge_fee_pct = (
                float(
                    fetch_bybit_taker_fee_rate(key, secret, self.market_symbol)
                )
                * 100
            )
        count = process_hedge_history(
            self.state,
            candles,
            self.config,
            launched_at=self.launched_at,
            fee_pct=self._hedge_fee_pct,
        )
        self.state.save(self.state_path)
        return count

    def _process_one_way_candles(self, candles: pd.DataFrame) -> int:
        last_processed = (
            pd.Timestamp(self.state.last_processed_candle)
            if self.state.last_processed_candle
            else None
        )
        latest_time = pd.to_datetime(candles["time"], utc=True).iloc[-1]
        processed = 0
        for index, candle in enumerate(candle_rows(candles)):
            if candle.time < self.launched_at:
                continue
            if last_processed is not None and candle.time <= last_processed:
                continue
            self._execute_strategy_candle(
                candles.iloc[: index + 1],
                candle,
                catching_up=candle.time < latest_time,
            )
            # Commit the cursor only after the ledger transition succeeds.
            self.state.last_processed_candle = candle.time.isoformat()
            self.state.save(self.state_path)
            processed += 1
        return processed

    def _position_stopped(
        self, position: LocalPosition, candle: Candle
    ) -> bool:
        if (
            position.side is None
            or position.entry_price is None
            or self.config.stop_loss_pct <= 0
        ):
            return False
        if position.updated_at is None or candle.time < pd.Timestamp(
            position.updated_at
        ):
            return False
        loss = self.config.stop_loss_pct / 100
        if position.side == "Long":
            return candle.low <= position.entry_price * (1 - loss)
        return candle.high >= position.entry_price * (1 + loss)

    def _build_transition(
        self, side: str | None, price: float, fee_rate: Decimal
    ) -> PaperTradeTarget:
        allocation = self.config.initial_capital
        if side is not None:
            current = self.trader.current_position(self.trade_symbol)
            allocation = float(
                self.trader.available_capital(
                    self.trade_symbol,
                    self.config.initial_capital,
                    closing_price=price if current.side is not None else None,
                    taker_fee_rate=fee_rate,
                )
            )
            if allocation < self.config.minimum_order_size:
                side = None
        limits = OrderLimits()
        if (
            self.config.data_source == "Bybit REST"
            and self.market_symbol != "BTCUSDT"
        ):
            step, minimum, notional = fetch_bybit_order_limits(
                self.market_symbol
            )
            limits = OrderLimits(
                quantity_step=step,
                minimum_quantity=minimum,
                exchange_minimum_notional=notional,
            )
        return self.trader.build_target(
            self.trade_symbol, side, price, notional=allocation, limits=limits
        )

    def _execute_strategy_candle(
        self, candles: pd.DataFrame, candle: Candle, *, catching_up: bool
    ) -> None:
        decision = calculate_strategy_decision(
            candles, self.config, self.launched_at
        )
        current = self.trader.current_position(self.trade_symbol)
        stopped = not catching_up and self._position_stopped(current, candle)
        target_side = None if stopped else decision.side
        if current.side == target_side:
            return
        if catching_up and decision.historical_price is None:
            return
        execution_price = (
            decision.historical_price
            if catching_up and decision.historical_price is not None
            else self._fetch_execution_price()
        )
        if self.config.data_source == "Bybit REST":
            key, secret = load_bybit_credentials()
            fee_rate = fetch_bybit_taker_fee_rate(
                key, secret, self.market_symbol
            )
        else:
            fee_rate = fetch_mexc_taker_fee_rate(self.market_symbol)
        target = self._build_transition(target_side, execution_price, fee_rate)
        reason = (
            decision.exit_reason
            if catching_up
            else "Stop loss" if stopped else None
        )
        action = self.trader.reconcile(
            self.trade_symbol,
            target,
            taker_fee_rate=fee_rate,
            exit_reason=reason,
            executed_at=candle.time.to_pydatetime() if catching_up else None,
        )
        if action:
            _ = sys.stdout.write(
                f"{datetime.now(UTC).isoformat()} {candle.time.isoformat()}"
                f" {action}\n"
            )
            _ = sys.stdout.flush()

    def _fetch_execution_price(self) -> float:
        price = (
            fetch_live_price(self.market_symbol)
            if self.config.data_source == "Bybit REST"
            else fetch_mexc_live_price(self.market_symbol)
        )
        return 1 / price if self.config.reverse_ticker else price

    def run_forever(self) -> None:
        """Run forever."""
        _ = signal.signal(signal.SIGTERM, self.stop)
        _ = signal.signal(signal.SIGINT, self.stop)
        retry_seconds = 2
        poll_seconds = min(
            60, max(5, INTERVAL_MS[self.config.timeframe] // 4000)
        )
        capital_decimals = 2 if self.quote_currency == "USDT" else 8
        _ = sys.stdout.write(
            str(
                f"Paper bot launched at {self.state.launched_at};"
                f" source={self.config.data_source};"
                f" ticker={self.trade_symbol};"
                f" timeframe={self.config.timeframe}; initial"
                f" capital={self.config.initial_capital:.{capital_decimals}f}"
                f" {self.quote_currency}"
            )
            + "\n"
        )
        _ = sys.stdout.flush()
        self.market_feed.start()
        try:
            while self.running:
                try:
                    _ = self.process_available_candles()
                    # Publish only after all available candles and ledger
                    # changes
                    # have been reconciled; followers must not replay catch-up
                    # trades.
                    publish_target(
                        self.state_path.with_name("reference_target.json"),
                        self.config,
                        self.state,
                        position=self.trader.current_position(
                            self.trade_symbol
                        ),
                    )
                    retry_seconds = 2
                    self._wait(poll_seconds)
                except (
                    requests.RequestException,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    json.JSONDecodeError,
                ) as exc:
                    _ = sys.stdout.write(
                        str(
                            f"{datetime.now(UTC).isoformat()} retrying after"
                            f" error: {exc}"
                        )
                        + "\n"
                    )
                    _ = sys.stdout.flush()
                    self._wait(retry_seconds)
                    retry_seconds = min(retry_seconds * 2, 60)
        finally:
            self.market_feed.stop()

    def _wait(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while self.running and time.monotonic() < deadline:
            time.sleep(min(1, deadline - time.monotonic()))


def process_is_running(pid: int) -> bool:
    """Process is running.

    Returns:
        Whether the condition holds.

    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError, PermissionError:
        return False
    else:
        return True


def read_pid() -> int | None:
    """Read pid.

    Returns:
        The read pid result.

    """
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except FileNotFoundError, ValueError:
        return None


def run_command(config_path: Path, *, resume: bool = False) -> None:
    """Run command.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.

    """
    with INSTANCE_LOCK_FILE.open("a+", encoding="utf-8") as instance_lock:
        try:
            fcntl.flock(instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            message = "Another paper bot instance is already running."
            raise RuntimeError(message) from exc
        if not resume:
            STATE_FILE.unlink(missing_ok=True)
        _ = PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
        try:
            bot = PaperBot(config_path)
            with archive_while_running():
                bot.run_forever()
        finally:
            PID_FILE.unlink(missing_ok=True)


def start_command(config_path: Path, *, resume: bool = False) -> None:
    """Start command.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.

    """
    pid = read_pid()
    if pid and process_is_running(pid):
        message = f"Paper bot is already running with PID {pid}."
        raise RuntimeError(message)
    _ = PaperBotConfig.load(config_path)
    if not resume:
        STATE_FILE.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-m",
        "reference_bot.cli",
        "run",
        "--resume",
        "--config",
        str(config_path.resolve()),
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    with (
        LOG_FILE.open("a", encoding="utf-8") as log,
        Path(os.devnull).open("rb") as null_input,
    ):
        file_actions = [
            (os.POSIX_SPAWN_DUP2, null_input.fileno(), 0),
            (os.POSIX_SPAWN_DUP2, log.fileno(), 1),
            (os.POSIX_SPAWN_DUP2, log.fileno(), 2),
        ]
        _ = os.posix_spawn(
            sys.executable,
            command,
            environment,
            file_actions=file_actions,
            setsid=True,
        )
    for _ in range(50):
        time.sleep(0.1)
        pid = read_pid()
        if pid and process_is_running(pid):
            _ = sys.stdout.write(
                str(
                    f"Paper bot started in background with PID {pid}; log:"
                    f" {LOG_FILE}"
                )
                + "\n"
            )
            return
    message = f"Paper bot did not start. Check {LOG_FILE}."
    raise RuntimeError(message)


def stop_command() -> None:
    """Stop command."""
    pid = read_pid()
    if not pid or not process_is_running(pid):
        _ = sys.stdout.write("Paper bot is not running." + "\n")
        PID_FILE.unlink(missing_ok=True)
        return
    os.kill(pid, signal.SIGTERM)
    _ = sys.stdout.write(str(f"Stop requested for paper bot PID {pid}.") + "\n")


def reset_command() -> None:
    """Delete local trading history, positions, cursor, and cached candles.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.

    """
    pid = read_pid()
    if pid and process_is_running(pid):
        message = f"Stop the paper bot before resetting data (PID {pid})."
        raise RuntimeError(message)
    with INSTANCE_LOCK_FILE.open("a+", encoding="utf-8") as instance_lock:
        try:
            fcntl.flock(instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            message = "Stop the paper bot before resetting data."
            raise RuntimeError(message) from exc
        paths = (
            PAPER_ACCOUNT_FILE,
            PAPER_ACCOUNT_FILE.with_suffix(PAPER_ACCOUNT_FILE.suffix + ".lock"),
            PAPER_ACCOUNT_FILE.with_suffix(PAPER_ACCOUNT_FILE.suffix + ".tmp"),
            STATE_FILE,
            STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp"),
            CANDLE_CACHE_FILE,
            STATE_FILE.with_name("reference_target.json"),
            STATE_FILE.with_name("reference_target.json.tmp"),
            Path(f"{CANDLE_CACHE_FILE}-shm"),
            Path(f"{CANDLE_CACHE_FILE}-wal"),
            PID_FILE,
        )
        removed = [path.name for path in paths if path.is_file()]
        for path in paths:
            path.unlink(missing_ok=True)
    if removed:
        _ = sys.stdout.write(
            str(f"Deleted previous paper bot data: {', '.join(removed)}") + "\n"
        )
    else:
        _ = sys.stdout.write("No previous paper bot data found." + "\n")


def _configured_market(
    config_path: Path = PAPER_BOT_CONFIG_FILE,
) -> tuple[str, str, str]:
    config = PaperBotConfig.load(config_path)
    base, quote = config.ticker.split("_", maxsplit=1)
    return (
        (f"{quote}{base}", quote, base)
        if config.reverse_ticker
        else (f"{base}{quote}", base, quote)
    )


def status_command(config_path: Path = PAPER_BOT_CONFIG_FILE) -> None:
    """Status command."""
    pid = read_pid()
    status = (
        f"running (PID {pid})" if pid and process_is_running(pid) else "stopped"
    )
    _ = sys.stdout.write(str(f"Paper bot: {status}") + "\n")
    if STATE_FILE.is_file():
        _ = sys.stdout.write(
            str(STATE_FILE.read_text(encoding="utf-8").strip()) + "\n"
        )
    symbol, _, quote_currency = _configured_market(config_path)
    if STATE_FILE.is_file():
        saved = json_object(STATE_FILE.read_text())
        margin = saved.get("margin_state")
        if margin is not None:

            engine = MarginEngine(**parse_margin_snapshot(margin))
            _ = sys.stdout.write(
                str(
                    f"Isolated leverage: {engine.settings['leverage']}x; free"
                    f" cash: {engine.cash:.8f} {quote_currency}"
                )
                + "\n"
            )
            if engine.last_mark is not None:
                _ = sys.stdout.write(
                    str(
                        f"Mark equity: {engine.equity(engine.last_mark):.8f}"
                        f" {quote_currency}"
                    )
                    + "\n"
                )
            _ = sys.stdout.write(
                str(f"Positions: {json.dumps(engine.positions)}") + "\n"
            )
            _ = sys.stdout.write(
                str(f"Pending entries: {json.dumps(engine.pending)}") + "\n"
            )
            return
        raw_hedge = saved.get("hedge_state")
        hedge = object_mapping(raw_hedge) if raw_hedge is not None else None
        if hedge is not None:
            _ = sys.stdout.write(
                str(
                    f"Hedge cash: {numeric_string(hedge['cash']):.8f}"
                    f" {quote_currency}"
                )
                + "\n"
            )
            _ = sys.stdout.write(
                str(f"Hedge positions: {json.dumps(hedge['positions'])}") + "\n"
            )
            _ = sys.stdout.write(
                str(f"Pending hedge entries: {json.dumps(hedge['pending'])}")
                + "\n"
            )
            return
    position = LocalPaperTrader().current_position(symbol)
    _ = sys.stdout.write(
        str(
            f"Position: {position.side or 'Flat'} {position.quantity}"
            f" entry={position.entry_price or 0}"
        )
        + "\n"
    )
    deposit_size = float(position.quantity) * (position.entry_price or 0)
    decimals = 2 if quote_currency == "USDT" else 8
    _ = sys.stdout.write(
        str(f"Deposit size: {deposit_size:.{decimals}f} {quote_currency}")
        + "\n"
    )


def stats_command(config_path: Path = PAPER_BOT_CONFIG_FILE) -> None:
    """Stats command."""
    symbol, base_currency, quote_currency = _configured_market(config_path)
    decimals = 2 if quote_currency == "USDT" else 8
    hedge = (
        json_object(STATE_FILE.read_text()).get("hedge_state")
        if STATE_FILE.is_file()
        else None
    )
    margin = (
        json_object(STATE_FILE.read_text()).get("margin_state")
        if STATE_FILE.is_file()
        else None
    )
    ledger = margin if margin is not None else hedge
    history = (
        list(
            reversed([
                object_mapping(item)
                for item in object_list(object_mapping(ledger)["history"])
            ])
        )
        if ledger is not None
        else LocalPaperTrader().trade_history(symbol)
    )
    if not history:
        _ = sys.stdout.write("No completed local paper trades." + "\n")
        return
    width = max(
        40, min(shutil.get_terminal_size(fallback=(100, 24)).columns, 100)
    )

    def line(value: str) -> None:
        """Line."""
        _ = sys.stdout.write(
            str(textwrap.fill(value, width=width, subsequent_indent="    "))
            + "\n"
        )

    pnl = pd.to_numeric(
        pd.Series([trade.get("pnl", 0) for trade in history]), errors="coerce"
    ).fillna(0)
    line(
        f"Completed trades: {len(history)} | Net P&L: {pnl.sum():.8f}"
        f" {quote_currency} | Win rate: {(pnl.gt(0).mean() * 100):.2f}%"
    )
    _ = sys.stdout.write("Newest trade first." + "\n")
    for number, trade in enumerate(history, start=1):
        _ = sys.stdout.write("\n")
        line(f"Trade {number}: {trade.get('side', 'Unknown')}")
        line(
            f"  Entry: {trade.get('entry_time', '-')} | price"
            f" {numeric_string(trade.get('entry_price', 0)):.2f}"
        )
        line(
            f"  Exit:  {trade.get('exit_time', '-')} | price"
            f" {numeric_string(trade.get('exit_price', 0)):.2f}"
        )
        line(f"  Quantity: {trade.get('quantity', 0)} {base_currency}")
        line(
            "  Deposit size:"
            f" {numeric_string(trade.get('deposit_size', 0)):.{decimals}f}"
            f" {quote_currency}"
        )
        line(
            f"  Fees: entry {numeric_string(trade.get('entry_fee', 0)):.4f} |"
            f" exit {numeric_string(trade.get('exit_fee', 0)):.8f}"
            f" {quote_currency}"
        )
        line(
            f"  Result: {numeric_string(trade.get('pnl', 0)):.8f}"
            f" {quote_currency} | return"
            f" {numeric_string(trade.get('return_pct', 0)):.2f}%"
        )
        if numeric_string(trade.get("leverage", 1)) > 1:
            line(
                f"  Leverage: {trade['leverage']}x | Margin:"
                f" {trade['margin']:.8f} | Entry notional:"
                f" {trade['entry_notional']:.8f}"
            )
            line(
                f"  Funding paid: {trade['funding_paid']:.8f} | Liquidation"
                f" adjustment: {trade['liquidation_adjustment']:.8f}"
                f" {quote_currency}"
            )
        line(f"  Exit reason: {trade.get('exit_reason', '-')}")


class Arguments(argparse.Namespace):
    """Typed command options populated by the argument parser."""

    command: str = ""
    config: Path = PAPER_BOT_CONFIG_FILE
    resume: bool = False


def parse_args() -> Arguments:
    """Parse args.

    Returns:
        The parse args result.

    """
    parser = argparse.ArgumentParser(
        description="Persistent local-only Bybit-data paper trading bot"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "start"):
        subparser = subparsers.add_parser(command)
        _ = subparser.add_argument(
            "--config", type=Path, default=PAPER_BOT_CONFIG_FILE
        )
        _ = subparser.add_argument(
            "--resume",
            action="store_true",
            help="Resume the prior launch anchor and candle cursor.",
        )
    _ = subparsers.add_parser("stop")
    _ = subparsers.add_parser(
        "reset",
        help="Delete positions, trade history, state, and cached candles.",
    )
    for command in ("status", "stats"):
        subparser = subparsers.add_parser(command)
        _ = subparser.add_argument(
            "--config", type=Path, default=PAPER_BOT_CONFIG_FILE
        )
    return parser.parse_args(namespace=Arguments())


def _dispatch(args: Arguments) -> None:
    if args.command == "run":
        run_command(args.config, resume=args.resume)
    elif args.command == "start":
        start_command(args.config, resume=args.resume)
    elif args.command == "stop":
        stop_command()
    elif args.command == "reset":
        reset_command()
    elif args.command == "status":
        status_command(args.config)
    else:
        stats_command(args.config)


def main() -> None:
    """Run the selected paper bot command.

    Raises:
        SystemExit: If the requested operation cannot safely proceed.

    """
    args = parse_args()
    try:
        _dispatch(args)
    except (OSError, RuntimeError, ValueError) as exc:
        _ = sys.stderr.write(f"Error: {exc}\n")
        message = 1
        raise SystemExit(message) from exc


if __name__ == "__main__":
    main()
