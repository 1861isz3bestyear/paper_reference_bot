# Copyright 2026 anchored-vwam-backtest contributors
"""Implementation of bybit bot cli."""

from __future__ import annotations

import argparse
import fcntl
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from bybit_bot.client import BybitClient, BybitError
from bybit_demo_bot.cli import BybitDemoBot
from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from server_bot.tunnel_runtime import archive_while_running
from shared.env import load_env, setting

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "bybitrealapi.env"
STATE_FILE = ROOT / "bybit_state.json"
LOCK_FILE = ROOT / "bybit_bot.instance.lock"


@dataclass
class BybitState:
    """Represent bybit state."""

    launched_at: str
    last_processed_candle: str | None = None
    hedge_state: dict | None = None
    pending_protection_side: str | None = None
    pending_take_profit: str | None = None
    halted_reason: str | None = None
    consumed_signal_side: str | None = None
    observed_position_side: str | None = None

    @classmethod
    def load_or_create(cls, resume: bool) -> BybitState:
        """Load or create."""
        if resume and STATE_FILE.is_file():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                # Old state cannot tell whether the current signal already traded.
                if "consumed_signal_side" not in data and data.get(
                    "last_processed_candle"
                ):
                    data["consumed_signal_side"] = "legacy"
                return cls(**data)
            except (OSError, TypeError, json.JSONDecodeError) as exc:
                message = f"Cannot read {STATE_FILE.name}: {exc}"
                raise RuntimeError(message) from None
        state = cls(datetime.now(UTC).isoformat())
        state.save()
        return state

    def save(self) -> None:
        """Save."""
        temporary = STATE_FILE.with_suffix(".json.tmp")
        _ = temporary.write_text(
            json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8"
        )
        _ = Path(temporary).replace(STATE_FILE)


class BybitBot(BybitDemoBot):
    """Run the demo-tested strategy mechanics against Bybit mainnet."""

    EXECUTOR_NAME = "Bybit MAINNET"
    ACCOUNT_NAME = "Bybit mainnet"


def run_bybit_command(
    config_path: Path,
    env_path: Path,
    *,
    confirm_live: bool = False,
    resume: bool = False,
    poll_seconds: int = 10,
) -> None:
    """Run bybit command."""
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
        from bybit_demo_bot.hedge import HedgeReplicaBot
        from shared.hedge_strategy import HEDGE_MODE

        executor = (
            HedgeReplicaBot if config.strategy_mode == HEDGE_MODE else BybitBot
        )
        with archive_while_running():
            executor(config, client, BybitState.load_or_create(resume)).run(
                poll_seconds
            )


class CommandArguments(argparse.Namespace):
    """Arguments validated and populated by the command parser."""

    config: Path = PAPER_BOT_CONFIG_FILE
    env: Path = ENV_FILE
    confirm_live: bool = False
    resume: bool = False
    poll_seconds: int = 10


def parse_args() -> CommandArguments:
    """Parse args."""
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
    """Main."""
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
