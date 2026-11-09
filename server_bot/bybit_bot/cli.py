# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of bybit bot cli."""

from __future__ import annotations

import argparse
import fcntl
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, override

from bybit_bot.client import BybitClient, BybitError
from bybit_demo_bot.cli import BybitDemoBot, DemoState
from bybit_demo_bot.hedge import HedgeReplicaBot
from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from server_bot.tunnel_runtime import archive_while_running
from shared.env import load_env, setting
from shared.hedge_strategy import HEDGE_MODE

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "bybitrealapi.env"
STATE_FILE = ROOT / "bybit_state.json"
LOCK_FILE = ROOT / "bybit_bot.instance.lock"


class BybitState(DemoState):
    """Keep mainnet execution state in its own persistence file."""

    @classmethod
    @override
    def load_or_create(cls, *, resume: bool) -> BybitState:
        """Load or create.

        Returns:
            The load or create result.

        Raises:
            RuntimeError: If the requested operation cannot safely proceed.

        """
        if resume and STATE_FILE.is_file():
            try:
                return cls.read_state(STATE_FILE)
            except (OSError, TypeError, json.JSONDecodeError) as exc:
                message = f"Cannot read {STATE_FILE.name}: {exc}"
                raise RuntimeError(message) from None
        state = cls(datetime.now(UTC).isoformat())
        state.save()
        return state

    @override
    def save(self) -> None:
        """Save."""
        temporary = STATE_FILE.with_suffix(".json.tmp")
        _ = temporary.write_text(
            json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8"
        )
        _ = Path(temporary).replace(STATE_FILE)


class BybitBot(BybitDemoBot):
    """Run the demo-tested strategy mechanics against Bybit mainnet."""

    EXECUTOR_NAME: ClassVar[str] = "Bybit MAINNET"
    ACCOUNT_NAME: ClassVar[str] = "Bybit mainnet"


def run_bybit_command(
    config_path: Path,
    env_path: Path,
    *,
    confirm_live: bool = False,
    resume: bool = False,
    poll_seconds: int = 10,
) -> None:
    """Run bybit command.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.
        ValueError: If the requested operation cannot safely proceed.

    """
    if not confirm_live:
        message = "Refusing to trade real funds without --confirm-live"
        raise ValueError(message)
    if poll_seconds < 1:
        message = "poll-seconds must be positive"
        raise ValueError(message)
    config, values = PaperBotConfig.load(config_path), load_env(env_path)
    client = BybitClient(
        setting(values, "BYBIT_API_KEY"), setting(values, "BYBIT_API_SECRET")
    )
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            message = "Another Bybit mainnet bot is running"
            raise RuntimeError(message) from None

        executor = (
            HedgeReplicaBot if config.strategy_mode == HEDGE_MODE else BybitBot
        )
        with archive_while_running():
            executor(
                config, client, BybitState.load_or_create(resume=resume)
            ).run(poll_seconds)


class CommandArguments(argparse.Namespace):
    """Arguments validated and populated by the command parser."""

    config: Path = PAPER_BOT_CONFIG_FILE
    env: Path = ENV_FILE
    confirm_live: bool = False
    resume: bool = False
    poll_seconds: int = 10


def parse_args() -> CommandArguments:
    """Parse args.

    Returns:
        The parse args result.

    """
    parser = argparse.ArgumentParser(
        description="Trade the strategy using real funds on Bybit mainnet"
    )
    _ = parser.add_argument("run", choices=("run",))
    _ = parser.add_argument("--confirm-live", action="store_true")
    _ = parser.add_argument(
        "--config", type=Path, default=PAPER_BOT_CONFIG_FILE
    )
    _ = parser.add_argument("--env", type=Path, default=ENV_FILE)
    _ = parser.add_argument("--resume", action="store_true")
    _ = parser.add_argument("--poll-seconds", type=int, default=10)
    return parser.parse_args(namespace=CommandArguments())


def main() -> None:
    """Dispatch the requested command.

    Raises:
        SystemExit: If the requested operation cannot safely proceed.

    """
    args = parse_args()
    try:
        run_bybit_command(
            args.config,
            args.env,
            confirm_live=args.confirm_live,
            resume=args.resume,
            poll_seconds=args.poll_seconds,
        )
    except (BybitError, OSError, RuntimeError, ValueError) as exc:
        message = f"Error: {exc}"
        raise SystemExit(message) from exc


if __name__ == "__main__":
    main()
