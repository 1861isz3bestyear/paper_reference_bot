from __future__ import annotations

import argparse, fcntl, json, os, signal, time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
import pandas as pd

from real_bot.client import MEXCError, MEXCFuturesClient
from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from reference_bot.market import CandleCache
from shared.indicators import add_launch_weekly_anchored_vwap

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE, STATE_FILE, LOCK_FILE = ROOT / ".env", ROOT / "real_bot_state.json", ROOT / "real_bot.instance.lock"
REFERENCE_STATE, PAPER_STATE = ROOT / "reference_state.json", ROOT / "live_paper_state.json"
REFERENCE_CANDLES = ROOT / "reference_candles.sqlite"
MINIMUM_NOTIONAL = Decimal("0.01")
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


@dataclass
class RealState:
    last_calculated_candle: str | None = None
    order_ids: list[str] | None = None
    orders_placed_candle: str | None = None
    orders_placed_at: str | None = None
    protections: dict[str, dict[str, str]] | None = None
    protected_position_id: str | None = None

    @classmethod
    def load(cls) -> "RealState":
        if not STATE_FILE.is_file():
            return cls(order_ids=[])
        try:
            state = cls(**json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read {STATE_FILE.name}: {exc}") from None
        state.order_ids = state.order_ids or []
        state.protections = state.protections or {}
        return state

    def save(self) -> None:
        temporary = STATE_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, STATE_FILE)


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"Environment file not found: {path}")
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"): continue
        if "=" not in line: raise ValueError(f"Invalid .env entry on line {number}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def setting(values: dict[str, str], name: str, default: str | None = None) -> str:
    value = os.getenv(name) or values.get(name) or default
    if value is None or not value.strip(): raise ValueError(f"Missing required setting {name}")
    return value.strip()


def read_peer_state(path: Path) -> dict[str, Any]:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise RuntimeError(f"Cannot read {path.name}: {exc}") from None
    return value if isinstance(value, dict) else {}


def response_order_id(response: Any) -> str:
    if isinstance(response, dict): response = response.get("orderId") or response.get("id")
    if response is None or not str(response).strip(): raise RuntimeError("MEXC returned no order ID")
    return str(response)


class RealBot:
    def __init__(self, config: PaperBotConfig, client: MEXCFuturesClient, *, leverage: int, open_type: int) -> None:
        if config.data_source != "MEXC REST" or config.reverse_ticker:
            raise ValueError("Real trading requires a non-reversed MEXC REST configuration")
        if config.strategy_mode != "VWAP band mean reversion" or not config.trend:
            raise ValueError("Two-band real orders require trend-mode VWAP band mean reversion")
        if config.stop_loss_pct <= 0:
            raise ValueError("Real trading requires a positive stop_loss_pct")
        self.config, self.client, self.leverage, self.open_type = config, client, leverage, open_type
        self.symbol, self.market_symbol = config.ticker, config.ticker.replace("_", "")
        self.state, self.cache, self.running = RealState.load(), CandleCache(REFERENCE_CANDLES), True

    def stop(self, *_: object) -> None: self.running = False

    def _latest_candle(self, max_age_seconds: int) -> tuple[pd.Timestamp, pd.Timestamp]:
        reference, paper = read_peer_state(REFERENCE_STATE), read_peer_state(PAPER_STATE)
        reference_latest, paper_latest = reference.get("latest_cached_candle"), paper.get("latest_cached_candle")
        if not reference_latest or not paper_latest: raise RuntimeError("both paper bots must have a completed candle")
        latest = pd.Timestamp(reference_latest)
        if latest != pd.Timestamp(paper_latest): raise RuntimeError("paper bots have not reached the same completed candle")
        age = (datetime.now(timezone.utc) - latest.to_pydatetime()).total_seconds()
        if age > max_age_seconds: raise RuntimeError(f"latest paper candle is stale ({age:.0f}s old)")
        return latest, pd.Timestamp(reference["launched_at"])

    def _bands(self, latest: pd.Timestamp, launched: pd.Timestamp) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        anchor = launched - timedelta(days=self.config.anchor_before_days) if self.config.anchor_before_strategy_start else launched
        end_ms = int(latest.timestamp() * 1000) + INTERVAL_SECONDS[self.config.timeframe] * 1000
        candles = self.cache.load(self.market_symbol, self.config.timeframe, int(anchor.timestamp() * 1000), end_ms)
        if candles.empty or pd.Timestamp(candles.iloc[-1]["time"]) != latest: raise RuntimeError("reference candle cache is not ready")
        row = add_launch_weekly_anchored_vwap(candles, anchor, self.config.vwap_anchor_reset_weeks).iloc[-1]
        if pd.isna(row["anchored_vwap"]) or pd.isna(row["anchored_std"]): raise RuntimeError("AVWAP bands are unavailable")
        vwap, std = Decimal(str(row["anchored_vwap"])), Decimal(str(row["anchored_std"]))
        values = (
            self.config.open_sigma_1 if self.config.open_sigma_1 is not None else self.config.open_order_vwap_sigma,
            self.config.close_sigma_1 if self.config.close_sigma_1 is not None else self.config.close_order_vwap_sigma,
            self.config.open_sigma_2 if self.config.open_sigma_2 is not None else -self.config.open_order_vwap_sigma,
            self.config.close_sigma_2 if self.config.close_sigma_2 is not None else -self.config.close_order_vwap_sigma,
        )
        return tuple(vwap + std * Decimal(str(value)) for value in values)  # type: ignore[return-value]

    def _cancel_tracked(self, open_ids: set[str] | None = None) -> None:
        tracked = self.state.order_ids or []
        ids = tracked if open_ids is None else [value for value in tracked if value in open_ids]
        if ids: self.client.cancel_orders(self.symbol, ids)
        self.state.order_ids, self.state.orders_placed_candle, self.state.orders_placed_at = [], None, None
        self.state.protections = {}
        self.state.save()

    @staticmethod
    def _tick(value: Decimal, tick: Decimal) -> Decimal:
        return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick

    def _sizing(self, entries: tuple[Decimal, Decimal]) -> tuple[int, Decimal, Decimal]:
        detail = self.client.contract(self.symbol)
        size, tick = Decimal(str(detail["contractSize"])), Decimal(str(detail["priceUnit"]))
        allocation = min(Decimal(str(self.config.initial_capital)), self.client.available_usdt())
        required = max(MINIMUM_NOTIONAL, Decimal(str(self.config.minimum_order_size)))
        if allocation < required: raise RuntimeError(f"allocation {allocation} USDT is below {required} USDT")
        volume = int((allocation / (max(entries) * size)).to_integral_value(rounding=ROUND_DOWN))
        step = int(detail.get("volUnit", 1)); volume = volume // step * step
        if volume < int(detail.get("minVol", 1)) or Decimal(volume) * size * min(entries) < required:
            raise RuntimeError("exchange-sized order is below the 0.01 USDT minimum")
        return volume, tick, size

    def reconcile_once(self, max_age_seconds: int, order_lifetime_minutes: int = 10) -> str | None:
        open_ids = {str(item.get("orderId")) for item in self.client.open_orders(self.symbol)}
        positions = [item for item in self.client.positions(self.symbol) if Decimal(str(item.get("holdVol", 0))) > 0]
        if len(positions) > 1:
            self._cancel_tracked(open_ids); raise RuntimeError("multiple positions found; entries canceled")
        if positions:
            position = positions[0]
            raw_position_id = position.get("positionId") or position.get("id")
            if raw_position_id is None: raise RuntimeError("MEXC position has no position ID; cannot install protection")
            position_id = str(raw_position_id)
            side = "Long" if int(position.get("positionType", 0)) == 1 else "Short" if int(position.get("positionType", 0)) == 2 else None
            if side is None: raise RuntimeError("MEXC position has an unknown side; cannot install protection")
            sibling_ids = [value for value in (self.state.order_ids or []) if value in open_ids]
            if sibling_ids: self.client.cancel_orders(self.symbol, sibling_ids)
            if self.state.protected_position_id != position_id:
                plans = self.state.protections or {}
                plan = next((value for value in plans.values() if value.get("side") == side), None)
                if not plan: raise RuntimeError(f"no saved SL/TP plan for filled {side} position")
                self.client.set_position_stop_loss(raw_position_id, Decimal(plan["stop_loss"]))
                self.client.set_position_take_profit(raw_position_id, Decimal(plan["take_profit"]))
                self.state.protected_position_id = position_id
                self.state.order_ids, self.state.protections = [], {}
                self.state.orders_placed_candle, self.state.orders_placed_at = None, None
                self.state.save()
                return f"fill detected; sibling canceled and {side} SL/TP installed"
            return None
        tracked = set(self.state.order_ids or [])
        if tracked - open_ids:
            self._cancel_tracked(open_ids)
            return "entry disappeared; canceled sibling and waiting for next candle"
        if self.state.orders_placed_at:
            placed_at = datetime.fromisoformat(self.state.orders_placed_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - placed_at >= timedelta(minutes=order_lifetime_minutes):
                self._cancel_tracked(open_ids)
                return f"canceled unfilled pair after {order_lifetime_minutes} minutes"

        latest, launched = self._latest_candle(max_age_seconds)
        if self.state.last_calculated_candle == latest.isoformat(): return None

        entry1, exit1, entry2, exit2 = self._bands(latest, launched)
        volume, tick, contract_size = self._sizing((entry1, entry2))
        entry1, exit1, entry2, exit2 = (self._tick(value, tick) for value in (entry1, exit1, entry2, exit2))
        if not (exit1 > entry1 and exit2 < entry2): raise RuntimeError("exits must be beyond their entry bands")
        loss_pct = Decimal(str(self.config.stop_loss_pct)) / 100
        # Deposit is the actual isolated/cross margin implied by notional and leverage.
        # Converting its loss budget back to price makes SL independent of token price/contract size.
        long_deposit = Decimal(volume) * contract_size * entry1 / Decimal(self.leverage)
        short_deposit = Decimal(volume) * contract_size * entry2 / Decimal(self.leverage)
        long_delta = long_deposit * loss_pct / (Decimal(volume) * contract_size) if loss_pct else Decimal(0)
        short_delta = short_deposit * loss_pct / (Decimal(volume) * contract_size) if loss_pct else Decimal(0)
        stop1 = self._tick(entry1 - long_delta, tick) if loss_pct else None
        stop2 = self._tick(entry2 + short_delta, tick) if loss_pct else None
        # Required ordering: calculate successfully, cancel the old pair, then submit both replacements.
        if self.state.order_ids: self._cancel_tracked(open_ids)
        stamp = int(latest.timestamp())
        first = response_order_id(self.client.submit_limit_order(symbol=self.symbol, side=1, volume=volume, price=entry1,
            take_profit_price=None, stop_loss_price=None, leverage=self.leverage, open_type=self.open_type,
            external_oid=f"avwap-plus-{stamp}"))
        try:
            second = response_order_id(self.client.submit_limit_order(symbol=self.symbol, side=3, volume=volume, price=entry2,
                take_profit_price=None, stop_loss_price=None, leverage=self.leverage, open_type=self.open_type,
                external_oid=f"avwap-minus-{stamp}"))
        except Exception:
            self.client.cancel_orders(self.symbol, [first]); raise
        self.state.last_calculated_candle, self.state.order_ids = latest.isoformat(), [first, second]
        self.state.protections = {
            first: {"side": "Long", "stop_loss": format(stop1, "f"), "take_profit": format(exit1, "f")},  # type: ignore[arg-type]
            second: {"side": "Short", "stop_loss": format(stop2, "f"), "take_profit": format(exit2, "f")},  # type: ignore[arg-type]
        }
        self.state.protected_position_id = None
        self.state.orders_placed_candle = latest.isoformat()
        self.state.orders_placed_at = datetime.now(timezone.utc).isoformat()
        self.state.save()
        return f"placed refreshed +sigma/-sigma pair for {latest.isoformat()} ({volume} contracts each)"

    def run(self, *, poll_seconds: int, max_age_seconds: int, order_lifetime_minutes: int) -> None:
        signal.signal(signal.SIGTERM, self.stop); signal.signal(signal.SIGINT, self.stop)
        print(f"REAL MONEY two-band executor started: {self.symbol}", flush=True)
        while self.running:
            try:
                action = self.reconcile_once(max_age_seconds, order_lifetime_minutes)
                if action: print(f"{datetime.now(timezone.utc).isoformat()} {action}", flush=True)
            except (MEXCError, RuntimeError, ValueError, KeyError) as exc:
                print(f"{datetime.now(timezone.utc).isoformat()} no new orders: {exc}", flush=True)
            deadline = time.monotonic() + poll_seconds
            while self.running and time.monotonic() < deadline: time.sleep(min(1, deadline - time.monotonic()))


def run_real_command(config_path: Path, env_path: Path, *, confirm_live: bool, poll_seconds: int = 10,
                     max_signal_age: int = 180, order_lifetime_minutes: int = 10) -> None:
    if not confirm_live: raise SystemExit("Error: refusing real-money execution without --confirm-live")
    config, values = PaperBotConfig.load(config_path), load_env(env_path)
    margin, leverage = setting(values, "MEXC_MARGIN_MODE", "isolated").lower(), int(setting(values, "MEXC_LEVERAGE", "1"))
    if margin not in {"isolated", "cross"}: raise ValueError("MEXC_MARGIN_MODE must be isolated or cross")
    if min(leverage, poll_seconds, max_signal_age, order_lifetime_minutes) < 1: raise ValueError("numeric settings must be positive")
    client = MEXCFuturesClient(setting(values, "MEXC_API_KEY"), setting(values, "MEXC_API_SECRET"))
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("Another real-money executor is running") from None
        RealBot(config, client, leverage=leverage, open_type=1 if margin == "isolated" else 2).run(
            poll_seconds=poll_seconds, max_age_seconds=max_signal_age, order_lifetime_minutes=order_lifetime_minutes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trade refreshed MEXC AVWAP limit pairs")
    parser.add_argument("run-real", choices=("run-real",)); parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE); parser.add_argument("--env", type=Path, default=ENV_FILE)
    parser.add_argument("--poll-seconds", type=int, default=10); parser.add_argument("--max-signal-age", type=int, default=180)
    parser.add_argument("--order-lifetime-minutes", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try: run_real_command(args.config, args.env, confirm_live=args.confirm_live, poll_seconds=args.poll_seconds,
                          max_signal_age=args.max_signal_age, order_lifetime_minutes=args.order_lifetime_minutes)
    except (MEXCError, OSError, RuntimeError, ValueError) as exc: raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__": main()
