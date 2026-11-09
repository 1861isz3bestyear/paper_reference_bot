# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of bybit demo bot cli."""

from __future__ import annotations

import argparse
import fcntl
from pathlib import Path

from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from bybit_demo_bot.executor import BybitDemoBot as BybitDemoBot
from bybit_demo_bot.executor import (
    ReferenceAlignedDemoBot as ReferenceAlignedDemoBot,
)
from bybit_demo_bot.hedge import HedgeReplicaBot
from bybit_demo_bot.replica import ReferenceReplicaBot
from bybit_demo_bot.state import DemoState as DemoState
from reference_bot.config import (
    PAPER_BOT_CONFIG_FILE,
    PaperBotConfig,
)
from server_bot.tunnel_runtime import archive_while_running
from shared.env import load_env, setting
from shared.hedge_strategy import HEDGE_MODE

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / (
    "bybitdemoapi.env"
    if (ROOT / "bybitdemoapi.env").is_file()
    else "bybitapidemo.env"
)
STATE_FILE = ROOT / "bybit_demo_state.json"
LOCK_FILE = ROOT / "bybit_demo_bot.instance.lock"


def run_demo_command(
    config_path: Path,
    env_path: Path,
    *,
    resume: bool = False,
    poll_seconds: int = 10,
) -> None:
    """Run demo command.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.
        ValueError: If the requested operation cannot safely proceed.

    """
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
        state = DemoState.load_or_create(resume=resume)

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
    """Parse args.

    Returns:
        The parse args result.

    """
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
    """Dispatch the requested command.

    Raises:
        SystemExit: If the requested operation cannot safely proceed.

    """
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
