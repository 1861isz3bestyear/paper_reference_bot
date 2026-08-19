from __future__ import annotations

import argparse
from pathlib import Path

from live_paper_bot.cli import (
    reset_command as reset_live,
    start_command as start_live,
    stats_command as stats_live,
    status_command as status_live,
    stop_command as stop_live,
)
from reference_bot.cli import (
    reset_command as reset_reference,
    start_command as start_reference,
    stats_command as stats_reference,
    status_command as status_reference,
    stop_command as stop_reference,
)
from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig


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
    else:
        reset_both()


if __name__ == "__main__":
    main()
