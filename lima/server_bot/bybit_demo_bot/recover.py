"""Clear a saved halt only after read-only exchange and reference checks."""
from __future__ import annotations

import argparse
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from bybit_demo_bot import cli
from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from bybit_demo_bot.replica import ReferenceReplicaBot, TERMINAL
from reference_bot.config import PaperBotConfig
from shared.env import load_env, setting


def recover(bot: ReferenceReplicaBot) -> str:
    state = bot.state
    if not state.halted_reason:
        return "Demo is not halted; no state changed."
    if state.pending_protection_side:
        raise RuntimeError("Legacy entry is unresolved; recovery refused.")
    if state.replica_order:
        order = bot.client.order_by_link(bot.symbol, state.replica_order["link"])
        if not order or order.get("orderStatus") not in TERMINAL:
            raise RuntimeError("Replica order is unresolved; recovery refused.")
    if bot.client.has_open_orders(bot.symbol):
        raise RuntimeError("Exchange has open orders for this symbol; recovery refused.")
    if bot.client.position(bot.symbol) is not None:
        raise RuntimeError("Exchange position must be flat before recovery.")
    # Validate after network calls, using the time after snapshot read.
    target = bot._target()
    backup = cli.STATE_FILE.with_name(
        f"{cli.STATE_FILE.name}.recovery-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.bak"
    )
    backup.write_bytes(cli.STATE_FILE.read_bytes())
    state.halted_reason = None
    state.observed_position_side = None
    state.replica_position_id = None
    state.replica_order = None
    state.pending_protection_side = None
    state.pending_take_profit = None
    state.pending_strategy_close = False
    state.consumed_signal_side = None
    state.replica_initialized = True
    state.launched_at = target["reference_run"]
    state.last_processed_candle = target["last_processed_candle"]
    state.save()
    return f"Halt cleared after checks; no orders submitted. Backup: {backup.name}. Start bybit-demo.service to follow reference's current target."


def recover_command(config_path: Path, env_path: Path) -> str:
    with cli.LOCK_FILE.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Stop bybit-demo.service before recovery.") from None
        if not cli.STATE_FILE.is_file():
            raise RuntimeError("Demo state is missing; recovery refused.")
        config = PaperBotConfig.load(config_path)
        values = load_env(env_path)
        client = BybitDemoClient(setting(values, "BYBIT_API_KEY"), setting(values, "BYBIT_API_SECRET"))
        return recover(ReferenceReplicaBot(config, client, cli.DemoState.load_or_create(True)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=cli.PAPER_BOT_CONFIG_FILE)
    parser.add_argument("--env", type=Path, default=cli.ENV_FILE)
    args = parser.parse_args()
    try:
        print(recover_command(args.config, args.env))
    except (OSError, RuntimeError, ValueError, BybitDemoError) as exc:
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    main()
