from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from live_paper_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from live_paper_bot.market import (
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
from live_paper_bot.trading import (
    PAPER_ACCOUNT_FILE,
    LocalPaperTrader,
    fetch_bybit_taker_fee_rate,
    latest_strategy_side,
    load_bybit_credentials,
)
from shared.indicators import add_launch_weekly_anchored_vwap
from shared.strategy import backtest


ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "live_paper_state.json"
PID_FILE = ROOT / "live_paper_bot.pid"
LOG_FILE = ROOT / "live_paper_bot.log"
CANDLE_CACHE_FILE = ROOT / "live_paper_candles.sqlite"
INSTANCE_LOCK_FILE = ROOT / "live_paper_bot.instance.lock"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
REST_RECONCILE_SECONDS = 30 * 60


def _missing_candle_ranges(
    candle_times_ms: pd.Series,
    expected_first_ms: int,
    expected_latest_ms: int,
    interval_ms: int,
) -> list[tuple[int, int]]:
    """Return inclusive, contiguous ranges absent from the cached history."""
    present = {int(value) for value in candle_times_ms}
    ranges: list[tuple[int, int]] = []
    range_start: int | None = None
    previous = expected_first_ms
    for timestamp in range(expected_first_ms, expected_latest_ms + 1, interval_ms):
        if timestamp not in present:
            if range_start is None:
                range_start = timestamp
        elif range_start is not None:
            ranges.append((range_start, previous))
            range_start = None
        previous = timestamp
    if range_start is not None:
        ranges.append((range_start, expected_latest_ms))
    return ranges


@dataclass(frozen=True)
class StrategyDecision:
    side: str | None
    historical_price: float | None
    exit_reason: str | None = None


@dataclass
class BotState:
    launched_at: str
    last_processed_candle: str | None = None
    last_successful_market_sync: str | None = None
    latest_cached_candle: str | None = None
    websocket_connected: bool = False
    last_websocket_message: str | None = None
    websocket_reconnect_count: int = 0
    last_websocket_error: str | None = None

    @classmethod
    def load_or_create(cls, path: Path = STATE_FILE) -> "BotState":
        if path.is_file():
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        state = cls(launched_at=datetime.now(timezone.utc).isoformat())
        state.save(path)
        return state

    def save(self, path: Path = STATE_FILE) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.__dict__, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)


def calculate_strategy_decision(
    candles: pd.DataFrame,
    config: PaperBotConfig,
    launched_at: pd.Timestamp,
) -> StrategyDecision:
    if len(candles) < 2:
        return StrategyDecision(None, None)
    anchor_at = launched_at
    if config.anchor_before_strategy_start:
        anchor_at -= timedelta(days=config.anchor_before_days)
    data = add_launch_weekly_anchored_vwap(candles, anchor_at, config.vwap_anchor_reset_weeks)
    end = pd.Timestamp(data.iloc[-1]["time"])
    _, trades = backtest(
        data,
        strategy_start_at=launched_at,
        strategy_end_at=end,
        initial_capital=config.initial_capital,
        minimum_order_size=config.minimum_order_size,
        fee_pct=0.0,
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=0.0,
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
        price = float(open_trade.entry_price) if pd.Timestamp(open_trade.entry_time) == end else None
        return StrategyDecision(side, price)
    if trades:
        closed_trade = trades[-1]
        if pd.Timestamp(closed_trade.exit_time) == end and closed_trade.exit_reason != "End of strategy":
            return StrategyDecision(None, float(closed_trade.exit_price), closed_trade.exit_reason)
    return StrategyDecision(None, None)


def calculate_target_side(candles: pd.DataFrame, config: PaperBotConfig, launched_at: pd.Timestamp) -> str | None:
    return calculate_strategy_decision(candles, config, launched_at).side


class PaperBot:
    def __init__(
        self,
        config_path: Path,
        state_path: Path = STATE_FILE,
        candle_cache_path: Path = CANDLE_CACHE_FILE,
    ) -> None:
        self.config = PaperBotConfig.load(config_path)
        self.state_path = state_path
        self.state = BotState.load_or_create(state_path)
        self.launched_at = pd.Timestamp(self.state.launched_at)
        self.trader = LocalPaperTrader()
        self.candle_cache = CandleCache(candle_cache_path)
        self.market_symbol = self.config.ticker.replace("_", "")
        base, quote = self.config.ticker.split("_", maxsplit=1)
        self.trade_symbol = f"{quote}{base}" if self.config.reverse_ticker else self.market_symbol
        self.quote_currency = base if self.config.reverse_ticker else quote
        self.market_feed = (
            BybitKlineWebSocket(
                self.market_symbol,
                self.config.timeframe,
                lambda candles: self.candle_cache.upsert(self.market_symbol, self.config.timeframe, candles),
            )
            if self.config.data_source == "Bybit REST"
            else RestMarketFeed()
        )
        self._last_websocket_generation = 0
        self.running = True

    def stop(self, *_: object) -> None:
        self.running = False

    def _load_completed_candles(
        self,
        history_start: pd.Timestamp,
        now: datetime,
        reconcile_rest: bool = True,
    ) -> pd.DataFrame:
        interval_ms = INTERVAL_MS[self.config.timeframe]
        start_ms = int(history_start.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        expected_first_ms = ((start_ms + interval_ms - 1) // interval_ms) * interval_ms
        expected_latest_ms = (end_ms // interval_ms) * interval_ms - interval_ms
        cached = self.candle_cache.load(self.market_symbol, self.config.timeframe, start_ms, end_ms)
        has_full_start = not cached.empty and int(cached.iloc[0]["time"].timestamp() * 1000) <= expected_first_ms
        request_start_ms = start_ms
        if has_full_start:
            latest_ms = int(cached.iloc[-1]["time"].timestamp() * 1000)
            request_start_ms = max(start_ms, latest_ms - interval_ms)

        cached_times_ms = cached["time"].astype("int64") // 1_000_000 if not cached.empty else pd.Series(dtype="int64")
        cached_has_gap = len(cached_times_ms) > 1 and not cached_times_ms.diff().iloc[1:].eq(interval_ms).all()
        reconcile_rest = reconcile_rest or not has_full_start or cached_has_gap

        download_succeeded = False
        if reconcile_rest:
            try:
                fetcher = (
                    fetch_completed_linear_klines
                    if self.config.data_source == "Bybit REST"
                    else fetch_completed_mexc_klines
                )
                downloaded = fetcher(
                    self.market_symbol,
                    self.config.timeframe,
                    request_start_ms,
                    end_ms,
                )
                self.candle_cache.upsert(self.market_symbol, self.config.timeframe, downloaded)
                cached = self.candle_cache.load(self.market_symbol, self.config.timeframe, start_ms, end_ms)
                download_succeeded = True
            except (requests.RequestException, ValueError):
                cached_latest_ms = (
                    int(cached.iloc[-1]["time"].timestamp() * 1000) if not cached.empty else None
                )
                if expected_first_ms <= expected_latest_ms and cached_latest_ms != expected_latest_ms:
                    raise

        if cached.empty:
            return cached
        candle_times_ms = cached["time"].astype("int64") // 1_000_000
        has_gap = len(candle_times_ms) > 1 and not candle_times_ms.diff().iloc[1:].eq(interval_ms).all()
        first_ms = int(candle_times_ms.iloc[0])
        latest_ms = int(candle_times_ms.iloc[-1])
        incomplete_history = first_ms > expected_first_ms or has_gap
        missing_latest = latest_ms < expected_latest_ms
        incomplete = (
            expected_first_ms <= expected_latest_ms
            and (incomplete_history or (reconcile_rest and missing_latest))
        )
        if incomplete and request_start_ms != start_ms:
            fetcher = (
                fetch_completed_linear_klines
                if self.config.data_source == "Bybit REST"
                else fetch_completed_mexc_klines
            )
            downloaded = fetcher(
                self.market_symbol,
                self.config.timeframe,
                start_ms,
                end_ms,
            )
            self.candle_cache.upsert(self.market_symbol, self.config.timeframe, downloaded)
            cached = self.candle_cache.load(self.market_symbol, self.config.timeframe, start_ms, end_ms)
            candle_times_ms = cached["time"].astype("int64") // 1_000_000
            has_gap = len(candle_times_ms) > 1 and not candle_times_ms.diff().iloc[1:].eq(interval_ms).all()
            first_ms = int(candle_times_ms.iloc[0]) if not cached.empty else -1
            latest_ms = int(candle_times_ms.iloc[-1]) if not cached.empty else -1
            incomplete = first_ms > expected_first_ms or latest_ms < expected_latest_ms or has_gap
        # A broad paginated MEXC request can occasionally omit an interval. Ask
        # for every absent range explicitly before allowing strategy evaluation.
        if incomplete and self.config.data_source == "MEXC REST":
            missing_ranges = _missing_candle_ranges(
                candle_times_ms, expected_first_ms, expected_latest_ms, interval_ms
            )
            for missing_start_ms, missing_end_ms in missing_ranges:
                downloaded = fetch_completed_mexc_klines(
                    self.market_symbol,
                    self.config.timeframe,
                    missing_start_ms,
                    missing_end_ms + interval_ms,
                )
                self.candle_cache.upsert(self.market_symbol, self.config.timeframe, downloaded)
            cached = self.candle_cache.load(self.market_symbol, self.config.timeframe, start_ms, end_ms)
            candle_times_ms = cached["time"].astype("int64") // 1_000_000
            missing_ranges = _missing_candle_ranges(
                candle_times_ms, expected_first_ms, expected_latest_ms, interval_ms
            )
            incomplete = bool(missing_ranges)
        if incomplete:
            detail = ""
            if self.config.data_source == "MEXC REST" and missing_ranges:
                missing_start_ms, missing_end_ms = missing_ranges[0]
                detail = (
                    f" First missing range: "
                    f"{pd.to_datetime(missing_start_ms, unit='ms', utc=True).isoformat()} through "
                    f"{pd.to_datetime(missing_end_ms, unit='ms', utc=True).isoformat()}."
                )
            raise RuntimeError(
                f"{self.config.data_source} candle history is incomplete after targeted backfill; "
                f"refusing to calculate or advance the cursor.{detail}"
            )

        if download_succeeded:
            self.state.last_successful_market_sync = datetime.now(timezone.utc).isoformat()
        self.state.latest_cached_candle = pd.Timestamp(cached.iloc[-1]["time"]).isoformat()
        self.state.save(self.state_path)
        return reverse_candles(cached) if self.config.reverse_ticker else cached

    def _update_websocket_health(self) -> tuple[bool, int]:
        health = self.market_feed.health()
        self.state.websocket_connected = health.connected
        self.state.last_websocket_message = health.last_message
        self.state.websocket_reconnect_count = health.reconnect_count
        self.state.last_websocket_error = health.last_error
        return health.connected, health.connection_generation

    def process_available_candles(self) -> int:
        now = datetime.now(timezone.utc)
        history_start = self.launched_at
        if self.config.anchor_before_strategy_start:
            history_start -= timedelta(days=self.config.anchor_before_days)
        websocket_connected, generation = self._update_websocket_health()
        self.state.save(self.state_path)
        last_websocket_message = (
            pd.Timestamp(self.state.last_websocket_message) if self.state.last_websocket_message else None
        )
        websocket_stale_after = timedelta(seconds=max(120, INTERVAL_MS[self.config.timeframe] // 500))
        websocket_healthy = (
            websocket_connected
            and last_websocket_message is not None
            and now - last_websocket_message.to_pydatetime() <= websocket_stale_after
        )
        last_rest_sync = pd.Timestamp(self.state.last_successful_market_sync) if self.state.last_successful_market_sync else None
        rest_overdue = last_rest_sync is None or now - last_rest_sync.to_pydatetime() >= timedelta(
            seconds=REST_RECONCILE_SECONDS
        )
        reconnected = generation != self._last_websocket_generation
        reconcile_rest = self.config.data_source == "MEXC REST" or not websocket_healthy or rest_overdue or reconnected
        candles = self._load_completed_candles(history_start, now, reconcile_rest=reconcile_rest)
        self._last_websocket_generation = generation
        if candles.empty:
            return 0
        last_processed = pd.Timestamp(self.state.last_processed_candle) if self.state.last_processed_candle else None
        latest_candle_time = pd.Timestamp(candles.iloc[-1]["time"])
        processed = 0
        for index, candle in candles.iterrows():
            candle_time = pd.Timestamp(candle["time"])
            if candle_time < self.launched_at:
                continue
            if last_processed is not None and candle_time <= last_processed:
                continue
            prefix = candles.iloc[: index + 1]
            decision = calculate_strategy_decision(prefix, self.config, self.launched_at)
            target_side = decision.side
            catching_up = candle_time < latest_candle_time
            current = self.trader.current_position(self.trade_symbol)
            stop_triggered = False
            position_opened_at = pd.Timestamp(current.updated_at) if current.updated_at else None
            if (
                not catching_up
                and current.side is not None
                and current.entry_price is not None
                and self.config.stop_loss_pct > 0
                and position_opened_at is not None
                and candle_time >= position_opened_at
            ):
                if current.side == "Long":
                    stopped = float(candle["low"]) <= current.entry_price * (1 - self.config.stop_loss_pct / 100)
                else:
                    stopped = float(candle["high"]) >= current.entry_price * (1 + self.config.stop_loss_pct / 100)
                if stopped:
                    target_side = None
                    stop_triggered = True
            if current.side != target_side:
                if catching_up and decision.historical_price is None:
                    self.state.last_processed_candle = candle_time.isoformat()
                    self.state.save(self.state_path)
                    processed += 1
                    continue
                execution_price = (
                    float(decision.historical_price) if catching_up else self._fetch_execution_price()
                )
                if self.config.data_source == "Bybit REST":
                    api_key, api_secret = load_bybit_credentials()
                    fee_rate = fetch_bybit_taker_fee_rate(api_key, api_secret, self.market_symbol)
                else:
                    fee_rate = fetch_mexc_taker_fee_rate(self.market_symbol)
                allocation = self.config.initial_capital
                if target_side is not None:
                    allocation = float(
                        self.trader.available_capital(
                            self.trade_symbol,
                            self.config.initial_capital,
                            closing_price=execution_price if current.side is not None else None,
                            taker_fee_rate=fee_rate,
                        )
                    )
                    if allocation < self.config.minimum_order_size:
                        target_side = None
                target = self.trader.build_target(self.trade_symbol, target_side, execution_price, allocation)
                action = self.trader.reconcile(
                    self.trade_symbol,
                    target,
                    fee_rate,
                    exit_reason=(decision.exit_reason if catching_up else "Stop loss" if stop_triggered else None),
                    executed_at=candle_time.to_pydatetime() if catching_up else None,
                )
                if action:
                    print(f"{datetime.now(timezone.utc).isoformat()} {candle_time.isoformat()} {action}", flush=True)
            # This cursor is persisted only after the ledger transition above succeeds.
            self.state.last_processed_candle = candle_time.isoformat()
            self.state.save(self.state_path)
            processed += 1
        return processed

    def _fetch_execution_price(self) -> float:
        price = (
            fetch_live_price(self.market_symbol)
            if self.config.data_source == "Bybit REST"
            else fetch_mexc_live_price(self.market_symbol)
        )
        return 1 / price if self.config.reverse_ticker else price

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        retry_seconds = 2
        poll_seconds = min(60, max(5, INTERVAL_MS[self.config.timeframe] // 4000))
        capital_decimals = 2 if self.quote_currency == "USDT" else 8
        print(
            f"Paper bot launched at {self.state.launched_at}; source={self.config.data_source}; "
            f"ticker={self.trade_symbol}; timeframe={self.config.timeframe}; "
            f"initial capital={self.config.initial_capital:.{capital_decimals}f} {self.quote_currency}",
            flush=True,
        )
        self.market_feed.start()
        try:
            while self.running:
                try:
                    self.process_available_candles()
                    retry_seconds = 2
                    self._wait(poll_seconds)
                except (requests.RequestException, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    print(f"{datetime.now(timezone.utc).isoformat()} retrying after error: {exc}", flush=True)
                    self._wait(retry_seconds)
                    retry_seconds = min(retry_seconds * 2, 60)
        finally:
            self.market_feed.stop()

    def _wait(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while self.running and time.monotonic() < deadline:
            time.sleep(min(1, deadline - time.monotonic()))


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def run_command(config_path: Path, resume: bool = False) -> None:
    with INSTANCE_LOCK_FILE.open("a+", encoding="utf-8") as instance_lock:
        try:
            fcntl.flock(instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another paper bot instance is already running.") from exc
        if not resume:
            STATE_FILE.unlink(missing_ok=True)
        PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
        try:
            PaperBot(config_path).run_forever()
        finally:
            PID_FILE.unlink(missing_ok=True)


def start_command(config_path: Path, resume: bool = False) -> None:
    pid = read_pid()
    if pid and process_is_running(pid):
        raise RuntimeError(f"Paper bot is already running with PID {pid}.")
    PaperBotConfig.load(config_path)
    if not resume:
        STATE_FILE.unlink(missing_ok=True)
    log = LOG_FILE.open("a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, "-m", "live_paper_bot.cli", "run", "--resume", "--config", str(config_path)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    for _ in range(50):
        time.sleep(0.1)
        pid = read_pid()
        if pid and process_is_running(pid):
            print(f"Paper bot started in background with PID {pid}; log: {LOG_FILE}")
            return
    raise RuntimeError(f"Paper bot did not start. Check {LOG_FILE}.")


def stop_command() -> None:
    pid = read_pid()
    if not pid or not process_is_running(pid):
        print("Paper bot is not running.")
        PID_FILE.unlink(missing_ok=True)
        return
    os.kill(pid, signal.SIGTERM)
    print(f"Stop requested for paper bot PID {pid}.")


def reset_command() -> None:
    """Delete local trading history, positions, cursor, and cached candles."""
    pid = read_pid()
    if pid and process_is_running(pid):
        raise RuntimeError(f"Stop the paper bot before resetting data (PID {pid}).")
    with INSTANCE_LOCK_FILE.open("a+", encoding="utf-8") as instance_lock:
        try:
            fcntl.flock(instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Stop the paper bot before resetting data.") from exc
        paths = (
            PAPER_ACCOUNT_FILE,
            PAPER_ACCOUNT_FILE.with_suffix(PAPER_ACCOUNT_FILE.suffix + ".lock"),
            PAPER_ACCOUNT_FILE.with_suffix(PAPER_ACCOUNT_FILE.suffix + ".tmp"),
            STATE_FILE,
            STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp"),
            CANDLE_CACHE_FILE,
            Path(f"{CANDLE_CACHE_FILE}-shm"),
            Path(f"{CANDLE_CACHE_FILE}-wal"),
            PID_FILE,
        )
        removed = [path.name for path in paths if path.is_file()]
        for path in paths:
            path.unlink(missing_ok=True)
    if removed:
        print(f"Deleted previous paper bot data: {', '.join(removed)}")
    else:
        print("No previous paper bot data found.")


def _configured_market(config_path: Path = PAPER_BOT_CONFIG_FILE) -> tuple[str, str, str]:
    config = PaperBotConfig.load(config_path)
    base, quote = config.ticker.split("_", maxsplit=1)
    return (f"{quote}{base}", quote, base) if config.reverse_ticker else (f"{base}{quote}", base, quote)


def status_command(config_path: Path = PAPER_BOT_CONFIG_FILE) -> None:
    pid = read_pid()
    status = f"running (PID {pid})" if pid and process_is_running(pid) else "stopped"
    print(f"Paper bot: {status}")
    if STATE_FILE.is_file():
        print(STATE_FILE.read_text(encoding="utf-8").strip())
    symbol, _, quote_currency = _configured_market(config_path)
    position = LocalPaperTrader().current_position(symbol)
    print(f"Position: {position.side or 'Flat'} {position.quantity} entry={position.entry_price or 0}")
    deposit_size = float(position.quantity) * (position.entry_price or 0)
    decimals = 2 if quote_currency == "USDT" else 8
    print(f"Deposit size: {deposit_size:.{decimals}f} {quote_currency}")


def stats_command(config_path: Path = PAPER_BOT_CONFIG_FILE) -> None:
    symbol, base_currency, quote_currency = _configured_market(config_path)
    decimals = 2 if quote_currency == "USDT" else 8
    history = LocalPaperTrader().trade_history(symbol)
    if not history:
        print("No completed local paper trades.")
        return
    width = max(40, min(shutil.get_terminal_size(fallback=(100, 24)).columns, 100))

    def line(value: str) -> None:
        print(textwrap.fill(value, width=width, subsequent_indent="    "))

    pnl = pd.to_numeric(pd.Series([trade.get("pnl", 0) for trade in history]), errors="coerce").fillna(0)
    line(
        f"Completed trades: {len(history)} | Net P&L: {pnl.sum():.8f} {quote_currency} | "
        f"Win rate: {(pnl.gt(0).mean() * 100):.2f}%"
    )
    print("Newest trade first.")
    for number, trade in enumerate(history, start=1):
        print()
        line(f"Trade {number}: {trade.get('side', 'Unknown')}")
        line(f"  Entry: {trade.get('entry_time', '-')} | price {float(trade.get('entry_price', 0)):.2f}")
        line(f"  Exit:  {trade.get('exit_time', '-')} | price {float(trade.get('exit_price', 0)):.2f}")
        line(f"  Quantity: {trade.get('quantity', 0)} {base_currency}")
        line(f"  Deposit size: {float(trade.get('deposit_size', 0)):.{decimals}f} {quote_currency}")
        line(
            f"  Fees: entry {float(trade.get('entry_fee', 0)):.4f} | "
            f"exit {float(trade.get('exit_fee', 0)):.8f} {quote_currency}"
        )
        line(
            f"  Result: {float(trade.get('pnl', 0)):.8f} {quote_currency} | "
            f"return {float(trade.get('return_pct', 0)):.2f}%"
        )
        line(f"  Exit reason: {trade.get('exit_reason', '-')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persistent local-only Bybit-data paper trading bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "start"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
        subparser.add_argument("--resume", action="store_true", help="Resume the prior launch anchor and candle cursor.")
    subparsers.add_parser("stop")
    subparsers.add_parser("reset", help="Delete positions, trade history, state, and cached candles.")
    for command in ("status", "stats"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if args.command == "run":
            run_command(args.config, args.resume)
        elif args.command == "start":
            start_command(args.config, args.resume)
        elif args.command == "stop":
            stop_command()
        elif args.command == "reset":
            reset_command()
        elif args.command == "status":
            status_command(args.config)
        else:
            stats_command(args.config)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
