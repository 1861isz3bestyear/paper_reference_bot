# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of bybit demo bot cli."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from bybit_demo_bot.executor import BybitDemoBot as BybitDemoBot
from bybit_demo_bot.executor import (
    ReferenceAlignedDemoBot as ReferenceAlignedDemoBot,
)
from bybit_demo_bot.state import DemoState as DemoState
from bybit_demo_bot.tunnel_executor import TunnelDemoBot
from bybit_demo_bot.tunnel_journal import TunnelJournal
from server_bot.tunnel_runtime import archive_while_running
from shared.env import load_env, setting
from shared.paper_config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from shared.tunnel_config import TUNNEL_MODE

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / (
    "bybitdemoapi.env"
    if (ROOT / "bybitdemoapi.env").is_file()
    else "bybitapidemo.env"
)
STATE_FILE = ROOT / "bybit_demo_state.json"
LOCK_FILE = ROOT / "bybit_demo_bot.instance.lock"


@dataclass(frozen=True, kw_only=True)
class DemoPaths:
    """Locate the archive and dedicated execution journal."""

    archive: Path = ROOT / "tunnel_inputs.sqlite"
    state: Path = ROOT / "bybit_demo_tunnel.sqlite"


def run_demo_command(
    config_path: Path,
    env_path: Path,
    *,
    resume: bool = False,
    poll_seconds: int = 10,
    paths: DemoPaths | None = None,
) -> None:
    """Run demo command.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.
        ValueError: If the requested operation cannot safely proceed.

    """
    if poll_seconds < 1:
        message = "poll-seconds must be positive"
        raise ValueError(message)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = PaperBotConfig.load(config_path)
    if config.strategy_mode != TUNNEL_MODE:
        message = "Bybit demo supports only Snapshot tunnel configs."
        raise ValueError(message)
    selected = paths or DemoPaths()
    if selected.state.exists() and not resume:
        message = (
            "Demo journal exists; use --resume to preserve order recovery."
        )
        raise RuntimeError(message)
    values = load_env(env_path)
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
        journal = TunnelJournal(
            path=selected.state,
            fingerprint=hashlib.sha256(config.to_json().encode()).hexdigest(),
        )
        with archive_while_running():
            TunnelDemoBot(
                config=config,
                client=client,
                journal=journal,
                archive=selected.archive,
            ).run(min(poll_seconds, 2))


class CommandArguments(argparse.Namespace):
    """Arguments validated and populated by the command parser."""

    config: Path = PAPER_BOT_CONFIG_FILE
    env: Path = ENV_FILE
    resume: bool = False
    poll_seconds: int = 10
    archive: Path = DemoPaths().archive
    state: Path = DemoPaths().state


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
    _ = parser.add_argument("--archive", type=Path, default=DemoPaths().archive)
    _ = parser.add_argument("--state", type=Path, default=DemoPaths().state)
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
            paths=DemoPaths(archive=args.archive, state=args.state),
        )
    except (BybitDemoError, OSError, RuntimeError, ValueError) as exc:
        message = f"Error: {exc}"
        raise SystemExit(message) from exc


if __name__ == "__main__":
    main()
