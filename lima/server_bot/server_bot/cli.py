from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from real_bot.cli import ENV_FILE as REAL_ENV_FILE, run_real_command
from bybit_demo_bot.cli import ENV_FILE as BYBIT_DEMO_ENV_FILE, run_demo_command
from bybit_bot.cli import ENV_FILE as BYBIT_ENV_FILE, run_bybit_command

from live_paper_bot.cli import (
    STATE_FILE as LIVE_STATE_FILE,
    reset_command as reset_live,
    start_command as start_live,
    stats_command as stats_live,
    status_command as status_live,
    stop_command as stop_live,
)
from reference_bot.cli import (
    STATE_FILE as REFERENCE_STATE_FILE,
    reset_command as reset_reference,
    start_command as start_reference,
    stats_command as stats_reference,
    status_command as status_reference,
    stop_command as stop_reference,
)
from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3_600, "4h": 14_400, "1d": 86_400}
HEALTHY = 0
REFERENCE_STALE = 1
INDETERMINATE = 2


def start_both(config_path: Path, resume: bool = False) -> None:
    PaperBotConfig.load(config_path)
    start_reference(config_path, resume)
    start_live(config_path, resume)


def stop_both() -> None:
    print("Reference bot:")
    stop_reference()
    print("Live-paper bot:")
    stop_live()


def status_both(config_path: Path) -> None:
    print("Reference bot:")
    status_reference(config_path)
    print("\nLive-paper bot:")
    status_live(config_path)


def stats_both(config_path: Path) -> None:
    print("Reference bot:")
    stats_reference(config_path)
    print("\nLive-paper bot:")
    stats_live(config_path)


def reset_both() -> None:
    print("Reference bot:")
    reset_reference()
    print("Live-paper bot:")
    reset_live()


def _latest_cached_candle(path: Path) -> datetime | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("latest_cached_candle")
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def health_check(
    config_path: Path,
    *,
    stale_candles: int = 10,
    paper_grace_candles: int = 3,
    now: datetime | None = None,
) -> tuple[int, str]:
    """Compare peer candle progress without restarting or modifying either bot."""
    if stale_candles < 1 or paper_grace_candles < 1:
        raise ValueError("Health-check candle thresholds must be positive.")
    config = PaperBotConfig.load(config_path)
    interval_seconds = INTERVAL_SECONDS[config.timeframe]
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError("Health-check time must be timezone-aware.")
    live_candle = _latest_cached_candle(LIVE_STATE_FILE)
    reference_candle = _latest_cached_candle(REFERENCE_STATE_FILE)
    if live_candle is None:
        return INDETERMINATE, "Health indeterminate: live-paper has no valid latest cached candle."
    live_age = (checked_at - live_candle).total_seconds()
    if live_age > paper_grace_candles * interval_seconds:
        return INDETERMINATE, (
            f"Health indeterminate: live-paper data is {live_age / interval_seconds:.1f} candles old."
        )
    if reference_candle is None:
        return REFERENCE_STALE, "Reference unhealthy: no valid latest cached candle while live-paper is current."
    lag_candles = (live_candle - reference_candle).total_seconds() / interval_seconds
    if lag_candles >= stale_candles:
        return REFERENCE_STALE, f"Reference unhealthy: {lag_candles:.1f} candles behind live-paper."
    return HEALTHY, f"Healthy: reference lag is {lag_candles:.1f} candles."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control both server bot processes.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    start.add_argument("--resume", action="store_true")
    for command in ("status", "stats"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    subparsers.add_parser("stop")
    subparsers.add_parser("reset")
    health = subparsers.add_parser("health-check")
    health.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    health.add_argument("--stale-candles", type=int, default=10)
    health.add_argument("--paper-grace-candles", type=int, default=3)
    real = subparsers.add_parser("run-real", help="Run the foreground MEXC real-money consensus executor.")
    real.add_argument("--confirm-live", action="store_true", help="required acknowledgement that real funds are used")
    real.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    real.add_argument("--env", type=Path, default=REAL_ENV_FILE)
    real.add_argument("--poll-seconds", type=int, default=10)
    real.add_argument("--max-signal-age", type=int, default=180)
    real.add_argument("--order-lifetime-minutes", type=int, default=10)
    demo = subparsers.add_parser("run-bybit-demo", help="Run the foreground Bybit demo-account executor.")
    demo.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    demo.add_argument("--env", type=Path, default=BYBIT_DEMO_ENV_FILE)
    demo.add_argument("--resume", action="store_true")
    demo.add_argument("--poll-seconds", type=int, default=10)
    bybit = subparsers.add_parser("run-bybit-mainnet", help="Run the real-money Bybit mainnet executor.")
    bybit.add_argument("--confirm-live", action="store_true", help="required acknowledgement that real funds are used")
    bybit.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    bybit.add_argument("--env", type=Path, default=BYBIT_ENV_FILE)
    bybit.add_argument("--resume", action="store_true")
    bybit.add_argument("--poll-seconds", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "start":
        start_both(args.config, args.resume)
    elif args.command == "stop":
        stop_both()
    elif args.command == "status":
        status_both(args.config)
    elif args.command == "stats":
        stats_both(args.config)
    elif args.command == "reset":
        reset_both()
    elif args.command == "health-check":
        status, message = health_check(
            args.config,
            stale_candles=args.stale_candles,
            paper_grace_candles=args.paper_grace_candles,
        )
        print(message)
        raise SystemExit(status)
    elif args.command == "run-real":
        run_real_command(
            args.config,
            args.env,
            confirm_live=args.confirm_live,
            poll_seconds=args.poll_seconds,
            max_signal_age=args.max_signal_age,
            order_lifetime_minutes=args.order_lifetime_minutes,
        )
    elif args.command == "run-bybit-demo":
        run_demo_command(args.config, args.env, resume=args.resume, poll_seconds=args.poll_seconds)
    else:
        run_bybit_command(
            args.config,
            args.env,
            confirm_live=args.confirm_live,
            resume=args.resume,
            poll_seconds=args.poll_seconds,
        )


if __name__ == "__main__":
    main()
