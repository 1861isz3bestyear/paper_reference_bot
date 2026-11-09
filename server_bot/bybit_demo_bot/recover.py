# Copyright 2026 anchored-vwam-backtest contributors
"""Clear a saved halt only after read-only exchange and reference checks."""

from __future__ import annotations

import argparse
import fcntl
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bybit_demo_bot import cli
from bybit_demo_bot import state as execution_state
from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from bybit_demo_bot.hedge import HedgeReplicaBot
from bybit_demo_bot.replica import TERMINAL, ReferenceReplicaBot
from reference_bot.config import PAPER_BOT_CONFIG_FILE, PaperBotConfig
from shared.env import load_env, setting
from shared.hedge_strategy import HEDGE_MODE


def recover(bot: ReferenceReplicaBot | HedgeReplicaBot) -> str:
    """Recover.

    Returns:
        The recover result.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.

    """
    state = bot.state
    if not state.halted_reason:
        return "Demo is not halted; no state changed."
    if state.pending_protection_side:
        message = "Legacy entry is unresolved; recovery refused."
        raise RuntimeError(message)
    if isinstance(bot, HedgeReplicaBot):
        return _recover_hedge(bot)
    if state.replica_order:
        order = bot.client.order_by_link(
            bot.symbol, state.replica_order["link"]
        )
        if not order or order.get("orderStatus") not in TERMINAL:
            message = "Replica order is unresolved; recovery refused."
            raise RuntimeError(message)
    if bot.client.has_open_orders(bot.symbol):
        message = "Exchange has open orders for this symbol; recovery refused."
        raise RuntimeError(message)
    if bot.client.position(bot.symbol) is not None:
        message = "Exchange position must be flat before recovery."
        raise RuntimeError(message)
    # Validate after network calls, using the time after snapshot read.
    target = bot.load_reference_target()
    backup = execution_state.STATE_FILE.with_name(
        f"{execution_state.STATE_FILE.name}.recovery-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.bak"
    )
    _ = backup.write_bytes(execution_state.STATE_FILE.read_bytes())
    state.halted_reason = None
    state.observed_position_side = None
    state.replica_position_id = None
    state.replica_order = None
    state.pending_protection_side = None
    state.pending_take_profit = None
    state.pending_strategy_close = False
    state.consumed_signal_side = None
    state.replica_initialized = True
    state.launched_at = target.reference_run
    state.last_processed_candle = target.last_processed_candle
    state.save()
    return (
        "Halt cleared after checks; no orders submitted. Backup:"
        f" {backup.name}. Start bybit-demo.service to follow reference's"
        " current target."
    )


def _recover_hedge(bot: HedgeReplicaBot) -> str:
    state = bot.state
    orders = state.hedge_state["orders"] if state.hedge_state else {}
    for order in orders.values():
        if order["submitted_at"] is not None:
            observed = bot.client.order_by_link(bot.symbol, order["link"])
            if not observed or observed["orderStatus"] not in TERMINAL:
                message = "Hedge order is unresolved; recovery refused."
                raise RuntimeError(message)
    if bot.client.hedge_positions(bot.symbol) or bot.client.has_open_orders(
        bot.symbol
    ):
        message = "Both hedge legs and orders must be flat before recovery."
        raise RuntimeError(message)
    _ = bot.load_reference_target()
    backup = execution_state.STATE_FILE.with_name(
        f"{execution_state.STATE_FILE.name}.recovery-{uuid4().hex}.bak"
    )
    _ = backup.write_bytes(execution_state.STATE_FILE.read_bytes())
    if state.hedge_state:
        state.hedge_state["complete"] = True
    state.halted_reason = None
    state.save()
    return (
        "Hedge halt cleared; completed cycle will not replay. Backup:"
        f" {backup.name}"
    )


def recover_command(config_path: Path, env_path: Path) -> str:
    """Recover command.

    Returns:
        The recover command result.

    Raises:
        RuntimeError: If the requested operation cannot safely proceed.

    """
    with cli.LOCK_FILE.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            message = "Stop bybit-demo.service before recovery."
            raise RuntimeError(message) from None
        if not execution_state.STATE_FILE.is_file():
            message = "Demo state is missing; recovery refused."
            raise RuntimeError(message)
        config = PaperBotConfig.load(config_path)
        values = load_env(env_path)
        client = BybitDemoClient(
            setting(values, "BYBIT_API_KEY"),
            setting(values, "BYBIT_API_SECRET"),
        )

        executor = (
            HedgeReplicaBot
            if config.strategy_mode == HEDGE_MODE
            else ReferenceReplicaBot
        )
        return recover(
            executor(config, client, cli.DemoState.load_or_create(resume=True))
        )


class Arguments(argparse.Namespace):
    """Recovery command arguments validated by argparse."""

    config: Path = PAPER_BOT_CONFIG_FILE
    env: Path = cli.ENV_FILE


def main() -> None:
    """Dispatch the requested command.

    Raises:
        SystemExit: If the requested operation cannot safely proceed.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--config", type=Path, default=PAPER_BOT_CONFIG_FILE
    )
    _ = parser.add_argument("--env", type=Path, default=cli.ENV_FILE)
    args = parser.parse_args(namespace=Arguments())
    try:
        _ = sys.stdout.write(str(recover_command(args.config, args.env)) + "\n")
    except (OSError, RuntimeError, ValueError, BybitDemoError) as exc:
        message = f"Error: {exc}"
        raise SystemExit(message) from exc


if __name__ == "__main__":
    main()
