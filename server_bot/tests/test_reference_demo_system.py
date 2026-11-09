# Copyright 2026 anchored-vwam-backtest contributors
"""Exercise reference ingestion, persistence and demo execution."""

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from bybit_demo_bot.recover import recover
from bybit_demo_bot.state import DemoState
from shared.validation import json_object, object_list, object_mapping, string

if TYPE_CHECKING:
    from tests.conftest import TradingSystem

REFERENCE_QUANTITY = 10
ENTRY_AND_CLOSE_COUNT = 2


pytestmark = pytest.mark.integration


def test_reference_entry_close_and_restart_are_idempotent(
    system: TradingSystem,
) -> None:
    """Verify reference entry close and restart are idempotent."""
    reference = system.producer()
    reference.run_forever()
    target = json_object(system.target.read_text())
    assert target["side"] == "Long"
    assert Decimal(string(target["quantity"])) == REFERENCE_QUANTITY
    assert target["last_processed_candle"] == "2026-01-01T00:01:00+00:00"
    demo = system.consumer()
    assert "submitted entry Buy" in (
        demo.reconcile_once(system.clock.value) or ""
    )
    assert "fill confirmed" in (demo.reconcile_once(system.clock.value) or "")
    assert system.exchange.position is not None
    assert system.exchange.position["size"] == target["quantity"]
    position_id = target["position_id"]
    # Recreate both services from their actual disk state, then repeat the
    # candle.
    system.producer().run_forever()
    restarted = system.consumer()
    assert restarted.reconcile_once(system.clock.value) is None
    assert json_object(system.target.read_text())["position_id"] == position_id
    assert len(system.exchange.submissions) == 1
    system.clock.value += timedelta(minutes=1)
    system.exchange.price = "102"
    system.producer().run_forever()
    assert json_object(system.target.read_text())["side"] is None
    assert "submitted reduce Sell" in (
        restarted.reconcile_once(system.clock.value) or ""
    )
    assert "fill confirmed" in (
        restarted.reconcile_once(system.clock.value) or ""
    )
    system.producer().run_forever()
    assert system.consumer().reconcile_once(system.clock.value) is None
    assert system.exchange.position is None
    assert len(system.exchange.submissions) == ENTRY_AND_CLOSE_COUNT
    assert system.exchange.submissions[1]["reduceOnly"] is True
    ledger = json_object(system.account.read_text())
    history = object_list(ledger["history"])
    assert len(history) == 1
    assert Decimal(string(object_mapping(history[0])["pnl"])) == Decimal(
        "17.9800"
    )
    assert any((
        path == "/v5/account/fee-rate" and signed
        for _, path, _, signed in system.exchange.requests
    ))


def test_accepted_order_with_lost_response_survives_restart_without_duplicate(
    system: TradingSystem,
) -> None:
    """Reconcile an accepted order after a lost response and restart."""
    system.producer().run_forever()
    system.exchange.lose_next_response = True
    demo = system.consumer()
    assert "intent retained" in (demo.reconcile_once(system.clock.value) or "")
    saved = DemoState.read_state(system.demo_path)
    assert saved.replica_order is not None
    link = saved.replica_order["link"]
    assert system.exchange.orders[link]["orderStatus"] == "Filled"
    restarted = system.consumer()
    assert "fill confirmed" in (
        restarted.reconcile_once(system.clock.value) or ""
    )
    assert restarted.reconcile_once(system.clock.value) is None
    assert len(system.exchange.submissions) == 1
    assert json_object(system.demo_path.read_text())["replica_order"] is None


def test_delayed_fill_after_restart_is_observed_before_new_orders(
    system: TradingSystem,
) -> None:
    """Verify delayed fill after restart is observed before new orders."""
    system.producer().run_forever()
    system.exchange.defer_fills = True
    demo = system.consumer()
    assert "submitted entry" in (demo.reconcile_once(system.clock.value) or "")
    restarted = system.consumer()
    assert "awaiting order/fill" in (
        restarted.reconcile_once(system.clock.value) or ""
    )
    saved = DemoState.read_state(system.demo_path)
    assert saved.replica_order is not None
    link = saved.replica_order["link"]
    assert len(system.exchange.submissions) == 1
    system.exchange.fill(link)
    assert "fill confirmed" in (
        restarted.reconcile_once(system.clock.value) or ""
    )
    assert restarted.reconcile_once(system.clock.value) is None
    assert len(system.exchange.submissions) == 1


def test_history_failure_stale_halt_and_checked_recovery_preserve_accounts(
    system: TradingSystem,
) -> None:
    """Preserve accounts through history failure, halt and recovery."""
    system.producer().run_forever()
    demo = system.consumer()
    _ = demo.reconcile_once(system.clock.value)
    _ = demo.reconcile_once(system.clock.value)
    last_good = system.target.read_bytes()
    system.clock.value += timedelta(minutes=3)
    # Missing completed candles prevent publication, even though the process
    # runs.
    system.producer().run_forever()
    assert system.target.read_bytes() == last_good
    assert "emergency close submitted" in (
        demo.reconcile_once(system.clock.value) or ""
    )
    assert system.exchange.position is None
    restarted = system.consumer()
    assert "current reference unavailable" in (
        restarted.reconcile_once(system.clock.value) or ""
    )
    before = system.demo_path.read_bytes()
    with pytest.raises(RuntimeError, match="reference target unavailable"):
        _ = recover(restarted)
    assert system.demo_path.read_bytes() == before
    start = int(system.exchange.candles[0][0])
    system.exchange.candles.extend([
        [str(start + i * 60000), "102", "102", "102", "102", "10", "1020"]
        for i in (3, 4)
    ])
    system.exchange.price = "102"
    system.producer().run_forever()
    assert system.target.read_bytes() != last_good
    assert "current reference healthy" in (
        restarted.reconcile_once(system.clock.value) or ""
    )
    assert restarted.state.halted_reason is not None
    orders = len(system.exchange.submissions)
    account_before = system.account.read_bytes()
    assert "Halt cleared after checks" in recover(restarted)
    assert len(system.exchange.submissions) == orders
    assert system.account.read_bytes() == account_before
    assert list(system.demo_path.parent.glob("*.recovery-*.bak"))
    assert system.consumer().reconcile_once(system.clock.value) is None
