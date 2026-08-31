from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pandas as pd
import requests

from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from live_paper_bot.cli import calculate_strategy_decision
from live_paper_bot.market import fetch_completed_linear_klines
from reference_bot.config import BYBIT_TICKERS, PAPER_BOT_CONFIG_FILE, PaperBotConfig
from real_bot.cli import load_env, setting
from shared.indicators import add_launch_weekly_anchored_vwap

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "bybitapidemo.env"
STATE_FILE = ROOT / "bybit_demo_state.json"
LOCK_FILE = ROOT / "bybit_demo_bot.instance.lock"
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


@dataclass
class DemoState:
    launched_at: str
    last_processed_candle: str | None = None
    pending_protection_side: str | None = None
    pending_take_profit: str | None = None
    halted_reason: str | None = None

    @classmethod
    def load_or_create(cls, resume: bool) -> "DemoState":
        if resume and STATE_FILE.is_file():
            try:
                return cls(**json.loads(STATE_FILE.read_text(encoding="utf-8")))
            except (OSError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Cannot read {STATE_FILE.name}: {exc}") from None
        state = cls(datetime.now(timezone.utc).isoformat())
        state.save()
        return state

    def save(self) -> None:
        temporary = STATE_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, STATE_FILE)


class BybitDemoBot:
    def __init__(self, config: PaperBotConfig, client: BybitDemoClient, state: DemoState) -> None:
        if config.data_source != "Bybit REST" or config.reverse_ticker or config.ticker not in BYBIT_TICKERS:
            raise ValueError(f"Bybit demo requires a non-reversed Bybit REST ticker from {sorted(BYBIT_TICKERS)}")
        self.config, self.client, self.state = config, client, state
        self.symbol, self.running = config.ticker.replace("_", ""), True

    def stop(self, *_: object) -> None:
        self.running = False

    @staticmethod
    def _floor(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    def _quantity(self, price: Decimal) -> Decimal:
        info = self.client.instruments(self.symbol)
        limits = info["lotSizeFilter"]
        step, minimum = Decimal(str(limits["qtyStep"])), Decimal(str(limits["minOrderQty"]))
        minimum_notional = Decimal(str(limits.get("minNotionalValue", "0")))
        allocation = min(Decimal(str(self.config.initial_capital)), self.client.available_usdt() * Decimal("0.98"))
        quantity = self._floor(allocation / price, step)
        required_notional = max(minimum_notional, Decimal(str(self.config.minimum_order_size)))
        if quantity < minimum or quantity * price < required_notional:
            raise RuntimeError("Bybit demo order is below configured or exchange minimum")
        return quantity

    def _exit_band(self, candles: pd.DataFrame, launched: pd.Timestamp, side: str) -> Decimal:
        anchor = launched - timedelta(days=self.config.anchor_before_days) if self.config.anchor_before_strategy_start else launched
        row = add_launch_weekly_anchored_vwap(candles, anchor, self.config.vwap_anchor_reset_weeks).iloc[-1]
        vwap, std = Decimal(str(row["anchored_vwap"])), Decimal(str(row["anchored_std"]))
        sigma = Decimal(str(self.config.close_order_vwap_sigma))
        if pd.isna(row["anchored_vwap"]) or pd.isna(row["anchored_std"]):
            raise RuntimeError("VWAP close band is unavailable for take profit")
        if self.config.trend:
            return vwap + std * sigma if side == "Buy" else vwap - std * sigma
        return vwap - std * sigma if side == "Buy" else vwap + std * sigma

    def _protect(self, position: dict[str, object], take_profit: Decimal) -> str:
        side = str(position["side"])
        entry = Decimal(str(position["avgPrice"]))
        loss = Decimal(str(self.config.stop_loss_pct)) / 100
        stop = entry * (1 - loss if side == "Buy" else 1 + loss)
        if (side == "Buy" and take_profit <= entry) or (side == "Sell" and take_profit >= entry):
            raise RuntimeError(f"VWAP close-band take profit {take_profit} is not profitable from {side} entry {entry}")
        self.client.set_protection(self.symbol, stop, take_profit)
        self.state.pending_protection_side = None
        self.state.pending_take_profit = None
        self.state.save()
        return f"{side} position protected"

    def reconcile_once(self, now: datetime | None = None) -> str | None:
        if self.state.halted_reason:
            raise RuntimeError(f"trading halted: {self.state.halted_reason}")
        position = self.client.position(self.symbol)
        if self.state.pending_protection_side and position:
            try:
                if self.state.pending_take_profit is None:
                    raise RuntimeError("pending VWAP take-profit price is missing")
                return self._protect(position, Decimal(self.state.pending_take_profit))
            except Exception as exc:
                try:
                    self.client.market_order(self.symbol, "Sell" if position["side"] == "Buy" else "Buy", Decimal(str(position["size"])), reduce_only=True)
                except Exception as close_exc:
                    self.state.halted_reason = f"protection failed ({exc}); emergency close failed ({close_exc})"
                    self.state.save()
                    raise RuntimeError(self.state.halted_reason) from exc
                self.state.halted_reason = f"protection failed ({exc}); emergency close submitted"
                self.state.save()
                raise RuntimeError(self.state.halted_reason) from exc
        current = pd.Timestamp(now or datetime.now(timezone.utc))
        interval = INTERVAL_SECONDS[self.config.timeframe]
        end_ms = int(current.timestamp() * 1000)
        latest_ms = end_ms // (interval * 1000) * interval * 1000 - interval * 1000
        latest = pd.Timestamp(latest_ms, unit="ms", tz="UTC")
        if self.state.last_processed_candle == latest.isoformat():
            return None
        launched = pd.Timestamp(self.state.launched_at)
        start = launched - timedelta(days=self.config.anchor_before_days) if self.config.anchor_before_strategy_start else launched
        candles = fetch_completed_linear_klines(self.symbol, self.config.timeframe, int(start.timestamp() * 1000), end_ms)
        if candles.empty or pd.Timestamp(candles.iloc[-1]["time"]) != latest:
            raise RuntimeError("completed Bybit candle history is not current")
        decision = calculate_strategy_decision(candles, self.config, launched)
        desired = "Buy" if decision.side == "Long" else "Sell" if decision.side == "Short" else None
        existing = str(position["side"]) if position else None
        actions = []
        if position and existing != desired:
            self.client.market_order(self.symbol, "Sell" if existing == "Buy" else "Buy", Decimal(str(position["size"])), reduce_only=True)
            actions.append(f"closed {existing}")
            position = None
        if desired and not position:
            price = self.client.last_price(self.symbol)
            self.state.pending_protection_side = desired
            self.state.pending_take_profit = format(self._exit_band(candles, launched, desired), "f")
            self.state.save()
            self.client.market_order(self.symbol, desired, self._quantity(price))
            actions.append(f"opened {desired}; awaiting fill for protection")
        elif position and desired == existing:
            self._protect(position, self._exit_band(candles, launched, desired))
            actions.append(f"updated {desired} protection to current VWAP close band")
        self.state.last_processed_candle = latest.isoformat()
        self.state.save()
        return "; ".join(actions) or None

    def run(self, poll_seconds: int) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        print(f"Bybit DEMO executor started: {self.symbol}", flush=True)
        while self.running:
            try:
                action = self.reconcile_once()
                if action:
                    print(f"{datetime.now(timezone.utc).isoformat()} {action}", flush=True)
            except (BybitDemoError, requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                print(f"{datetime.now(timezone.utc).isoformat()} no action: {exc}", flush=True)
            deadline = time.monotonic() + poll_seconds
            while self.running and time.monotonic() < deadline:
                time.sleep(min(1, deadline - time.monotonic()))


def run_demo_command(config_path: Path, env_path: Path, *, resume: bool = False, poll_seconds: int = 10) -> None:
    if poll_seconds < 1:
        raise ValueError("poll-seconds must be positive")
    config, values = PaperBotConfig.load(config_path), load_env(env_path)
    client = BybitDemoClient(setting(values, "BYBIT_API_KEY"), setting(values, "BYBIT_API_SECRET"))
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Bybit demo bot is running") from None
        BybitDemoBot(config, client, DemoState.load_or_create(resume)).run(poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trade the strategy on a Bybit demo account")
    parser.add_argument("run", choices=("run",))
    parser.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    parser.add_argument("--env", type=Path, default=ENV_FILE)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        run_demo_command(args.config, args.env, resume=args.resume, poll_seconds=args.poll_seconds)
    except (BybitDemoError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    main()
