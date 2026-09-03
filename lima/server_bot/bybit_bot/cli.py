from __future__ import annotations

import argparse
import fcntl
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bybit_bot.client import BybitClient, BybitError
from bybit_demo_bot.cli import BybitDemoBot
from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from shared.env import load_env, setting


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "bybitrealapi.env"
STATE_FILE = ROOT / "bybit_state.json"
LOCK_FILE = ROOT / "bybit_bot.instance.lock"


@dataclass
class BybitState:
    launched_at: str
    last_processed_candle: str | None = None
    pending_protection_side: str | None = None
    pending_take_profit: str | None = None
    halted_reason: str | None = None

    @classmethod
    def load_or_create(cls, resume: bool) -> "BybitState":
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
    if not confirm_live:
        raise ValueError("Refusing to trade real funds without --confirm-live")
    if poll_seconds < 1:
        raise ValueError("poll-seconds must be positive")
    config, values = PaperBotConfig.load(config_path), load_env(env_path)
    client = BybitClient(setting(values, "BYBIT_API_KEY"), setting(values, "BYBIT_API_SECRET"))
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Bybit mainnet bot is running") from None
        BybitBot(config, client, BybitState.load_or_create(resume)).run(poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trade the strategy using real funds on Bybit mainnet")
    parser.add_argument("run", choices=("run",))
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--config", type=Path, default=PAPER_BOT_CONFIG_FILE)
    parser.add_argument("--env", type=Path, default=ENV_FILE)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=10)
    return parser.parse_args()


def main() -> None:
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
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    main()
