# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of server bot cli."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from bybit_bot.cli import ENV_FILE as BYBIT_ENV_FILE
from bybit_bot.cli import run_bybit_command
from bybit_demo_bot.cli import ENV_FILE as BYBIT_DEMO_ENV_FILE
from bybit_demo_bot.cli import run_demo_command
from live_paper_bot.cli import (
    STATE_FILE as LIVE_STATE_FILE,
)
from live_paper_bot.cli import (
    reset_command as reset_live,
)
from live_paper_bot.cli import (
    start_command as start_live,
)
from live_paper_bot.cli import (
    stats_command as stats_live,
)
from live_paper_bot.cli import (
    status_command as status_live,
)
from live_paper_bot.cli import (
    stop_command as stop_live,
)
from reference_bot.cli import (
    STATE_FILE as REFERENCE_STATE_FILE,
)
from reference_bot.cli import (
    reset_command as reset_reference,
)
from reference_bot.cli import (
    start_command as start_reference,
)
from reference_bot.cli import (
    stats_command as stats_reference,
)
from reference_bot.cli import (
    status_command as status_reference,
)
from reference_bot.cli import (
    stop_command as stop_reference,
)
from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from shared.validation import json_object

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3_600,
    "4h": 14_400,
    "1d": 86_400,
}
HEALTHY = 0
REFERENCE_STALE = 1
INDETERMINATE = 2
CONFIG_DIRECTORY = PAPER_BOT_CONFIG_FILE.parent / "configs"


def start_both(config_path: Path, *, resume: bool = False) -> None:
    """Start both."""
    _ = PaperBotConfig.load(config_path)
    start_reference(config_path, resume)
    start_live(config_path, resume)


def stop_both() -> None:
    """Stop both."""
    _ = sys.stdout.write("Reference bot:" + "\n")
    stop_reference()
    _ = sys.stdout.write("Live-paper bot:" + "\n")
    stop_live()


def status_both(config_path: Path) -> None:
    """Status both."""
    _ = sys.stdout.write("Reference bot:" + "\n")
    status_reference(config_path)
    _ = sys.stdout.write("\nLive-paper bot:" + "\n")
    status_live(config_path)


def stats_both(config_path: Path) -> None:
    """Stats both."""
    _ = sys.stdout.write("Reference bot:" + "\n")
    stats_reference(config_path)
    _ = sys.stdout.write("\nLive-paper bot:" + "\n")
    stats_live(config_path)


def reset_both() -> None:
    """Reset both."""
    _ = sys.stdout.write("Reference bot:" + "\n")
    reset_reference()
    _ = sys.stdout.write("Live-paper bot:" + "\n")
    reset_live()


def _latest_cached_candle(path: Path) -> datetime | None:
    try:
        value = json_object(path.read_text(encoding="utf-8")).get(
            "latest_cached_candle"
        )
        if not isinstance(value, str) or not value:
            return None
        candle = datetime.fromisoformat(value)
        return candle if candle.utcoffset() is not None else None
    except OSError, TypeError, ValueError:
        return None


def health_check(
    config_path: Path,
    *,
    stale_candles: int = 10,
    paper_grace_candles: int = 3,
    now: datetime | None = None,
) -> tuple[int, str]:
    """Compare the bots' progress against the current market candle.

    Returns:
        The exit status and a description of the observed health.

    Raises:
        ValueError: If thresholds or the check timestamp are invalid.

    """
    if stale_candles < 1 or paper_grace_candles < 1:
        message = "Health-check candle thresholds must be positive."
        raise ValueError(message)
    config = PaperBotConfig.load(config_path)
    interval_seconds = INTERVAL_SECONDS[config.timeframe]
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        message = "Health-check time must be timezone-aware."
        raise ValueError(message)
    live_candle = _latest_cached_candle(LIVE_STATE_FILE)
    reference_candle = _latest_cached_candle(REFERENCE_STATE_FILE)
    if live_candle is None:
        return (
            INDETERMINATE,
            (
                "Health indeterminate: live-paper has no valid latest cached"
                " candle."
            ),
        )
    live_age = (checked_at - live_candle).total_seconds()
    if live_age < 0:
        return (
            INDETERMINATE,
            "Health indeterminate: live-paper candle is in the future.",
        )
    if live_age > paper_grace_candles * interval_seconds:
        return (
            INDETERMINATE,
            (
                "Health indeterminate: live-paper data is"
                f" {live_age / interval_seconds:.1f} candles old."
            ),
        )
    if reference_candle is None:
        return (
            REFERENCE_STALE,
            (
                "Reference unhealthy: no valid latest cached candle while"
                " live-paper is current."
            ),
        )
    if reference_candle > checked_at:
        message = "Reference unhealthy: latest cached candle is in the future."
    else:
        lag_candles = (
            live_candle - reference_candle
        ).total_seconds() / interval_seconds
        if lag_candles < stale_candles:
            return (
                HEALTHY,
                f"Healthy: reference lag is {lag_candles:.1f} candles.",
            )
        message = (
            f"Reference unhealthy: {lag_candles:.1f} candles behind live-paper."
        )
    return REFERENCE_STALE, message


class CommandArguments(argparse.Namespace):
    """Arguments validated and populated by the command parser."""

    command: str = ""
    directory: Path = CONFIG_DIRECTORY
    config: Path = PAPER_BOT_CONFIG_FILE
    env: Path = BYBIT_DEMO_ENV_FILE
    resume: bool = False
    confirm_live: bool = False
    poll_seconds: int = 10
    stale_candles: int = 10
    paper_grace_candles: int = 3


def parse_args() -> CommandArguments:
    """Parse and validate server command arguments.

    Returns:
        Arguments with their command-specific defaults.

    """
    parser = argparse.ArgumentParser(
        description="Control both server bot processes."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    configs = subparsers.add_parser(
        "configs", help="List named backtest exports."
    )
    _ = configs.add_argument("--directory", type=Path, default=CONFIG_DIRECTORY)
    start = subparsers.add_parser("start")
    _ = start.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    _ = start.add_argument("--resume", action="store_true")
    for command in ("status", "stats"):
        subparser = subparsers.add_parser(command)
        _ = subparser.add_argument(
            "--config", type=Path, default=PAPER_BOT_CONFIG_FILE
        )
    _ = subparsers.add_parser("stop")
    _ = subparsers.add_parser("reset")
    health = subparsers.add_parser("health-check")
    _ = health.add_argument(
        "--config", type=Path, default=PAPER_BOT_CONFIG_FILE
    )
    _ = health.add_argument("--stale-candles", type=int, default=10)
    _ = health.add_argument("--paper-grace-candles", type=int, default=3)
    demo = subparsers.add_parser(
        "run-bybit-demo", help="Run the foreground Bybit demo-account executor."
    )
    _ = demo.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    _ = demo.add_argument("--env", type=Path, default=BYBIT_DEMO_ENV_FILE)
    _ = demo.add_argument("--resume", action="store_true")
    _ = demo.add_argument("--poll-seconds", type=int, default=10)
    bybit = subparsers.add_parser(
        "run-bybit-mainnet", help="Run the real-money Bybit mainnet executor."
    )
    _ = bybit.add_argument(
        "--confirm-live",
        action="store_true",
        help="required acknowledgement that real funds are used",
    )
    _ = bybit.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    _ = bybit.add_argument("--env", type=Path, default=BYBIT_ENV_FILE)
    _ = bybit.add_argument("--resume", action="store_true")
    _ = bybit.add_argument("--poll-seconds", type=int, default=10)
    return parser.parse_args(namespace=CommandArguments())


def main() -> None:
    """Dispatch the selected server command.

    Raises:
        SystemExit: When a health check reports its process exit status.

    """
    args = parse_args()
    if args.command == "configs":
        for path in sorted(args.directory.glob("*.json")):
            _ = sys.stdout.write(str(path.resolve()) + "\n")
    elif args.command == "start":
        start_both(args.config, resume=args.resume)
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
        _ = sys.stdout.write(str(message) + "\n")
        raise SystemExit(status)
    elif args.command == "run-bybit-demo":
        run_demo_command(
            args.config,
            args.env,
            resume=args.resume,
            poll_seconds=args.poll_seconds,
        )
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
