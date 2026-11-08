# Copyright 2026 anchored-vwam-backtest contributors
"""Reference HTTP ingestion -> SQLite/ledger -> target file -> real demo HTTP client."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration


def test_reference_entry_close_and_restart_are_idempotent(system) -> None:
    """Verify reference entry close and restart are idempotent."""
    reference = system.producer()
    reference.run_forever()
    target = json.loads(system.target.read_text())
    if target["side"] != "Long":
        pytest.fail("Expected target['side'] == 'Long'")
    if Decimal(target["quantity"]) != 10:
        pytest.fail("Expected Decimal(target['quantity']) == 10")
    if target["last_processed_candle"] != "2026-01-01T00:01:00+00:00":
        pytest.fail(
            "Expected target['last_processed_candle'] =="
            " '2026-01-01T00:01:00+00:00'"
        )
    demo = system.consumer()
    if "submitted entry Buy" not in (demo.reconcile_once(system.clock.value)):
        pytest.fail(
            "Expected 'submitted entry Buy' in"
            " demo.reconcile_once(system.clock.value)"
        )
    if "fill confirmed" not in (demo.reconcile_once(system.clock.value)):
        pytest.fail(
            "Expected 'fill confirmed' in"
            " demo.reconcile_once(system.clock.value)"
        )
    if system.exchange.position["size"] != target["quantity"]:
        pytest.fail(
            "Expected system.exchange.position['size'] == target['quantity']"
        )
    position_id = target["position_id"]
    # Recreate both services from their actual disk state, then repeat the candle.
    system.producer().run_forever()
    restarted = system.consumer()
    if (restarted.reconcile_once(system.clock.value)) is not (None):
        pytest.fail(
            "Expected restarted.reconcile_once(system.clock.value) is None"
        )
    if json.loads(system.target.read_text())["position_id"] != position_id:
        pytest.fail(
            "Expected json.loads(system.target.read_text())['position_id'] =="
            " position_id"
        )
    if len(system.exchange.submissions) != 1:
        pytest.fail("Expected len(system.exchange.submissions) == 1")
    system.clock.value += timedelta(minutes=1)
    system.exchange.price = "102"
    system.producer().run_forever()
    if (json.loads(system.target.read_text())["side"]) is not (None):
        pytest.fail(
            "Expected json.loads(system.target.read_text())['side'] is None"
        )
    if "submitted reduce Sell" not in (
        restarted.reconcile_once(system.clock.value)
    ):
        pytest.fail(
            "Expected 'submitted reduce Sell' in"
            " restarted.reconcile_once(system.clock.value)"
        )
    if "fill confirmed" not in (restarted.reconcile_once(system.clock.value)):
        pytest.fail(
            "Expected 'fill confirmed' in"
            " restarted.reconcile_once(system.clock.value)"
        )
    system.producer().run_forever()
    if (system.consumer().reconcile_once(system.clock.value)) is not (None):
        pytest.fail(
            "Expected system.consumer().reconcile_once(system.clock.value) is"
            " None"
        )
    if (system.exchange.position) is not (None):
        pytest.fail("Expected system.exchange.position is None")
    if len(system.exchange.submissions) != 2:
        pytest.fail("Expected len(system.exchange.submissions) == 2")
    if (system.exchange.submissions[1]["reduceOnly"]) is not (True):
        pytest.fail(
            "Expected system.exchange.submissions[1]['reduceOnly'] is True"
        )
    ledger = json.loads(system.account.read_text())
    if len(ledger["history"]) != 1:
        pytest.fail("Expected len(ledger['history']) == 1")
    if Decimal(ledger["history"][0]["pnl"]) != Decimal("17.9800"):
        pytest.fail(
            "Expected Decimal(ledger['history'][0]['pnl']) =="
            " Decimal('17.9800')"
        )
    if not (
        any(
            path == "/v5/account/fee-rate" and signed
            for _, path, _, signed in system.exchange.requests
        )
    ):
        pytest.fail(
            "Expected any((path == '/v5/account/fee-rate' and signed for _,"
            " path, _, signed in system.exchange.requests))"
        )


def test_accepted_order_with_lost_response_survives_restart_without_duplicate(
    system,
) -> None:
    """Verify accepted order with lost response survives restart without duplicate."""
    system.producer().run_forever()
    system.exchange.lose_next_response = True
    demo = system.consumer()
    if "intent retained" not in (demo.reconcile_once(system.clock.value)):
        pytest.fail(
            "Expected 'intent retained' in"
            " demo.reconcile_once(system.clock.value)"
        )
    saved = json.loads(system.demo_path.read_text())
    link = saved["replica_order"]["link"]
    if system.exchange.orders[link]["orderStatus"] != "Filled":
        pytest.fail(
            "Expected system.exchange.orders[link]['orderStatus'] == 'Filled'"
        )
    restarted = system.consumer()
    if "fill confirmed" not in (restarted.reconcile_once(system.clock.value)):
        pytest.fail(
            "Expected 'fill confirmed' in"
            " restarted.reconcile_once(system.clock.value)"
        )
    if (restarted.reconcile_once(system.clock.value)) is not (None):
        pytest.fail(
            "Expected restarted.reconcile_once(system.clock.value) is None"
        )
    if len(system.exchange.submissions) != 1:
        pytest.fail("Expected len(system.exchange.submissions) == 1")
    if (json.loads(system.demo_path.read_text())["replica_order"]) is not (
        None
    ):
        pytest.fail(
            "Expected json.loads(system.demo_path.read_text())['replica_order']"
            " is None"
        )


def test_delayed_fill_after_restart_is_observed_before_new_orders(
    system,
) -> None:
    """Verify delayed fill after restart is observed before new orders."""
    system.producer().run_forever()
    system.exchange.defer_fills = True
    demo = system.consumer()
    if "submitted entry" not in (demo.reconcile_once(system.clock.value)):
        pytest.fail(
            "Expected 'submitted entry' in"
            " demo.reconcile_once(system.clock.value)"
        )
    restarted = system.consumer()
    if "awaiting order/fill" not in (
        restarted.reconcile_once(system.clock.value)
    ):
        pytest.fail(
            "Expected 'awaiting order/fill' in"
            " restarted.reconcile_once(system.clock.value)"
        )
    link = json.loads(system.demo_path.read_text())["replica_order"]["link"]
    if len(system.exchange.submissions) != 1:
        pytest.fail("Expected len(system.exchange.submissions) == 1")
    system.exchange.fill(link)
    if "fill confirmed" not in (restarted.reconcile_once(system.clock.value)):
        pytest.fail(
            "Expected 'fill confirmed' in"
            " restarted.reconcile_once(system.clock.value)"
        )
    if (restarted.reconcile_once(system.clock.value)) is not (None):
        pytest.fail(
            "Expected restarted.reconcile_once(system.clock.value) is None"
        )
    if len(system.exchange.submissions) != 1:
        pytest.fail("Expected len(system.exchange.submissions) == 1")


def test_history_failure_stale_halt_and_checked_recovery_preserve_accounts(
    system,
) -> None:
    """Verify history failure stale halt and checked recovery preserve accounts."""
    from bybit_demo_bot.recover import recover

    system.producer().run_forever()
    demo = system.consumer()
    demo.reconcile_once(system.clock.value)
    demo.reconcile_once(system.clock.value)
    last_good = system.target.read_bytes()
    system.clock.value += timedelta(minutes=3)
    # Missing completed candles prevent publication, even though the process runs.
    system.producer().run_forever()
    if system.target.read_bytes() != last_good:
        pytest.fail("Expected system.target.read_bytes() == last_good")
    if "emergency close submitted" not in (
        demo.reconcile_once(system.clock.value)
    ):
        pytest.fail(
            "Expected 'emergency close submitted' in"
            " demo.reconcile_once(system.clock.value)"
        )
    if (system.exchange.position) is not (None):
        pytest.fail("Expected system.exchange.position is None")
    restarted = system.consumer()
    if "current reference unavailable" not in (
        restarted.reconcile_once(system.clock.value)
    ):
        pytest.fail(
            "Expected 'current reference unavailable' in"
            " restarted.reconcile_once(system.clock.value)"
        )
    before = system.demo_path.read_bytes()
    with pytest.raises(RuntimeError, match="reference target unavailable"):
        _ = recover(restarted)
    if system.demo_path.read_bytes() != before:
        pytest.fail("Expected system.demo_path.read_bytes() == before")
    start = int(system.exchange.candles[0][0])
    system.exchange.candles.extend([
        [str(start + i * 60000), "102", "102", "102", "102", "10", "1020"]
        for i in (3, 4)
    ])
    system.exchange.price = "102"
    system.producer().run_forever()
    if system.target.read_bytes() == last_good:
        pytest.fail("Expected system.target.read_bytes() != last_good")
    if "current reference healthy" not in (
        restarted.reconcile_once(system.clock.value)
    ):
        pytest.fail(
            "Expected 'current reference healthy' in"
            " restarted.reconcile_once(system.clock.value)"
        )
    if not ((restarted.state.halted_reason) is not (None)):
        pytest.fail("Expected restarted.state.halted_reason is not None")
    orders = len(system.exchange.submissions)
    account_before = system.account.read_bytes()
    if "Halt cleared after checks" not in (recover(restarted)):
        pytest.fail(
            "Expected 'Halt cleared after checks' in recover(restarted)"
        )
    if len(system.exchange.submissions) != orders:
        pytest.fail("Expected len(system.exchange.submissions) == orders")
    if system.account.read_bytes() != account_before:
        pytest.fail("Expected system.account.read_bytes() == account_before")
    if not (list(system.demo_path.parent.glob("*.recovery-*.bak"))):
        pytest.fail(
            "Expected list(system.demo_path.parent.glob('*.recovery-*.bak'))"
        )
    if (system.consumer().reconcile_once(system.clock.value)) is not (None):
        pytest.fail(
            "Expected system.consumer().reconcile_once(system.clock.value) is"
            " None"
        )
