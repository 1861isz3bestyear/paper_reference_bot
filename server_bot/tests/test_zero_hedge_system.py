# Copyright 2026 anchored-vwam-backtest contributors
"""Actual reference ingestion/publication and two hedge-indexed limit orders over HTTP."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from bybit_demo_bot import hedge
from bybit_demo_bot.cli import DemoState
from reference_bot.config import PaperBotConfig
from shared.hedge_strategy import HEDGE_MODE

pytestmark = pytest.mark.integration


@pytest.fixture
def hedged(system, monkeypatch: pytest.MonkeyPatch):
    """Hedged."""
    path = system.state_path.with_name("config.json")
    cfg = replace(
        PaperBotConfig.load(path),
        strategy_mode=HEDGE_MODE,
        open_order_vwap_sigma=0,
        close_order_vwap_sigma=1.2,
        open_position_side="Both",
        stop_loss_pct=1,
        stop_loss_basis="Position",
    )
    cfg.save(path)
    system.clock.value += timedelta(minutes=1)
    monkeypatch.setattr(hedge, "datetime", system.clock)
    start = int(system.exchange.candles[0][0])
    system.exchange.candles = [
        [
            str(start + i * 60000),
            str(o),
            str(h),
            str(l),
            str(c),
            "10",
            str(c * 10),
        ]
        for i, (o, h, l, c) in enumerate(
            [(100, 100, 100, 100), (101, 102, 100, 102), (101, 101, 100, 100)]
        )
    ]
    system.exchange.hedge_mode = True
    system.producer().run_forever()
    system.cfg = cfg
    system.consumer = lambda: hedge.HedgeReplicaBot(
        cfg,
        system.client,
        DemoState.load_or_create(True),
        target_path=system.target,
    )
    return system


def test_two_same_vwap_limits_both_fill_and_restart_does_not_duplicate(
    hedged,
) -> None:
    """Verify two same vwap limits both fill and restart does not duplicate."""
    target = json.loads(hedged.target.read_text())
    if target["version"] != 2:
        pytest.fail("Expected target['version'] == 2")
    plan = target["hedge_plan"]
    if set(plan["legs"]) != {"Long", "Short"}:
        pytest.fail("Expected set(plan['legs']) == {'Long', 'Short'}")
    bot = hedged.consumer()
    action = bot.reconcile_once()
    if "submitted Long limit" not in (action):
        pytest.fail("Expected 'submitted Long limit' in action")
    if "submitted Short limit" not in (action):
        pytest.fail("Expected 'submitted Short limit' in action")
    if (bot.reconcile_once()) is not (None):
        pytest.fail("Expected bot.reconcile_once() is None")
    orders = hedged.exchange.submissions
    if len(orders) != 2:
        pytest.fail("Expected len(orders) == 2")
    if [o["orderType"] for o in orders] != ["Limit", "Limit"]:
        pytest.fail(
            "Expected [o['orderType'] for o in orders] == ['Limit', 'Limit']"
        )
    if [o["positionIdx"] for o in orders] != [1, 2]:
        pytest.fail("Expected [o['positionIdx'] for o in orders] == [1, 2]")
    if orders[0]["price"] != orders[1]["price"]:
        pytest.fail("Expected orders[0]['price'] == orders[1]['price']")
    if not (
        float(orders[0]["takeProfit"])
        > float(orders[0]["price"])
        > float(orders[1]["takeProfit"])
    ):
        pytest.fail(
            "Expected float(orders[0]['takeProfit']) >"
            " float(orders[0]['price']) > float(orders[1]['takeProfit'])"
        )
    if not (
        float(orders[0]["stopLoss"])
        < float(orders[0]["price"])
        < float(orders[1]["stopLoss"])
    ):
        pytest.fail(
            "Expected float(orders[0]['stopLoss']) < float(orders[0]['price'])"
            " < float(orders[1]['stopLoss'])"
        )
    if set(hedged.exchange.hedge_positions) != {1, 2}:
        pytest.fail("Expected set(hedged.exchange.hedge_positions) == {1, 2}")
    resumed = hedged.consumer()
    if (resumed.reconcile_once()) is not (None):
        pytest.fail("Expected resumed.reconcile_once() is None")
    if len(orders) != 2:
        pytest.fail("Expected len(orders) == 2")
    # One native TP fills; the other leg remains independently protected.
    hedged.exchange.hedge_positions.pop(1)
    resumed.reconcile_once()
    if set(hedged.exchange.hedge_positions) != {2}:
        pytest.fail("Expected set(hedged.exchange.hedge_positions) == {2}")
    hedged.exchange.hedge_positions.pop(2)
    resumed.reconcile_once()
    if not (resumed.state.hedge_state["complete"]):
        pytest.fail("Expected resumed.state.hedge_state['complete']")
    if (hedged.consumer().reconcile_once()) is not (None):
        pytest.fail("Expected hedged.consumer().reconcile_once() is None")
    if len(orders) != 2:
        pytest.fail("Expected len(orders) == 2")


def test_lost_first_response_is_reconciled_before_second_submission(
    hedged,
) -> None:
    """Verify lost first response is reconciled before second submission."""
    hedged.exchange.lose_next_response = True
    if "intent retained" not in (hedged.consumer().reconcile_once()):
        pytest.fail(
            "Expected 'intent retained' in hedged.consumer().reconcile_once()"
        )
    if len(hedged.exchange.submissions) != 1:
        pytest.fail("Expected len(hedged.exchange.submissions) == 1")
    resumed = hedged.consumer()
    if "submitted Short" not in (resumed.reconcile_once()):
        pytest.fail("Expected 'submitted Short' in resumed.reconcile_once()")
    resumed.reconcile_once()
    if len(hedged.exchange.submissions) != 2:
        pytest.fail("Expected len(hedged.exchange.submissions) == 2")


def test_stale_reference_closes_both_indices_and_recovery_does_not_replay(
    hedged,
) -> None:
    """Verify stale reference closes both indices and recovery does not replay."""
    from bybit_demo_bot.recover import recover

    bot = hedged.consumer()
    for _ in range(3):
        bot.reconcile_once()
    if len(hedged.exchange.hedge_positions) != 2:
        pytest.fail("Expected len(hedged.exchange.hedge_positions) == 2")
    original = hedged.clock.value
    hedged.clock.value += timedelta(minutes=3)
    if "trading halted" not in (bot.reconcile_once()):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once()")
    if hedged.exchange.hedge_positions:
        pytest.fail("Expected not hedged.exchange.hedge_positions")
    closes = hedged.exchange.submissions[2:]
    if {o["positionIdx"] for o in closes} != {1, 2}:
        pytest.fail("Expected {o['positionIdx'] for o in closes} == {1, 2}")
    if not (all(o["reduceOnly"] for o in closes)):
        pytest.fail("Expected all((o['reduceOnly'] for o in closes))")
    with pytest.raises(RuntimeError):
        _ = recover(bot)
    hedged.clock.value = original
    if "Hedge halt cleared" not in (recover(bot)):
        pytest.fail("Expected 'Hedge halt cleared' in recover(bot)")
    if (hedged.consumer().reconcile_once()) is not (None):
        pytest.fail("Expected hedged.consumer().reconcile_once() is None")
    if len(hedged.exchange.submissions) != 4:
        pytest.fail("Expected len(hedged.exchange.submissions) == 4")


def test_expired_pending_limits_are_cancelled_without_replacement(
    hedged,
) -> None:
    """Verify expired pending limits are cancelled without replacement."""
    hedged.exchange.defer_fills = True
    bot = hedged.consumer()
    bot.reconcile_once()
    bot.reconcile_once()
    if len(hedged.exchange.submissions) != 2:
        pytest.fail("Expected len(hedged.exchange.submissions) == 2")
    # Keep publisher/cursor fresh while the cycle's entry window expires.
    hedged.clock.value += timedelta(minutes=3)
    payload = json.loads(hedged.target.read_text())
    payload["published_at"] = hedged.clock.value.isoformat()
    payload["last_processed_candle"] = (
        hedged.clock.value - timedelta(minutes=1, seconds=5)
    ).isoformat()
    hedged.target.write_text(json.dumps(payload))
    bot.reconcile_once()
    bot.reconcile_once()
    if not (
        all(
            o["orderStatus"] == "Cancelled"
            for o in hedged.exchange.orders.values()
        )
    ):
        pytest.fail(
            "Expected all((o['orderStatus'] == 'Cancelled' for o in"
            " hedged.exchange.orders.values()))"
        )
    if len(hedged.exchange.submissions) != 2:
        pytest.fail("Expected len(hedged.exchange.submissions) == 2")


def test_unknown_order_response_never_resubmits_until_exchange_acknowledges(
    hedged,
) -> None:
    """Verify unknown order response never resubmits until exchange acknowledges."""
    bot = hedged.consumer()
    hedged.exchange.lose_next_response = True
    bot.reconcile_once()
    link = bot.state.hedge_state["orders"]["Long"]["link"]
    accepted = hedged.exchange.orders.pop(link)
    if "no resubmission" not in (hedged.consumer().reconcile_once()):
        pytest.fail(
            "Expected 'no resubmission' in hedged.consumer().reconcile_once()"
        )
    if len(hedged.exchange.submissions) != 1:
        pytest.fail("Expected len(hedged.exchange.submissions) == 1")
    hedged.exchange.orders[link] = accepted
    if "submitted Short" not in (hedged.consumer().reconcile_once()):
        pytest.fail(
            "Expected 'submitted Short' in hedged.consumer().reconcile_once()"
        )
    if len(hedged.exchange.submissions) != 2:
        pytest.fail("Expected len(hedged.exchange.submissions) == 2")


def test_rejected_second_order_flattens_the_first_leg(hedged) -> None:
    """Verify rejected second order flattens the first leg."""
    bot = hedged.consumer()
    hedged.exchange.defer_fills = True
    bot.reconcile_once()
    hedged.exchange.fill(bot.state.hedge_state["orders"]["Long"]["link"])
    link = bot.state.hedge_state["orders"]["Short"]["link"]
    hedged.exchange.orders[link]["orderStatus"] = "Rejected"
    hedged.exchange.defer_fills = False
    if "trading halted" not in (bot.reconcile_once()):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once()")
    if hedged.exchange.hedge_positions:
        pytest.fail("Expected not hedged.exchange.hedge_positions")
    if (hedged.exchange.submissions[-1]["reduceOnly"]) is not (True):
        pytest.fail(
            "Expected hedged.exchange.submissions[-1]['reduceOnly'] is True"
        )
    if hedged.exchange.submissions[-1]["positionIdx"] != 1:
        pytest.fail(
            "Expected hedged.exchange.submissions[-1]['positionIdx'] == 1"
        )


def test_cleanup_attempts_both_position_closes_even_if_cancel_fails(
    hedged, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify cleanup attempts both position closes even if cancel fails."""
    from bybit_demo_bot.client import BybitDemoError

    bot = hedged.consumer()
    for _ in range(3):
        bot.reconcile_once()
    link = bot.state.hedge_state["orders"]["Long"]["link"]
    hedged.exchange.orders[link]["orderStatus"] = "PartiallyFilled"

    def failed_cancel(*args) -> None:
        """Failed cancel."""
        message = "cancel unavailable"
        raise BybitDemoError(message)

    monkeypatch.setattr(hedged.client, "cancel_by_link", failed_cancel)
    hedged.clock.value += timedelta(minutes=3)
    with pytest.raises(BybitDemoError, match="cleanup incomplete"):
        bot.reconcile_once()
    if hedged.exchange.hedge_positions:
        pytest.fail("Expected not hedged.exchange.hedge_positions")
    if not (bot.state.halted_reason):
        pytest.fail("Expected bot.state.halted_reason")


@pytest.mark.parametrize(
    "change",
    [
        {"version": 1},
        {"config_fingerprint": "wrong"},
        {"hedge_plan": {"legs": {}}},
    ],
)
def test_invalid_hedge_targets_submit_no_entry(hedged, change) -> None:
    """Verify invalid hedge targets submit no entry."""
    payload = json.loads(hedged.target.read_text())
    payload.update(change)
    hedged.target.write_text(json.dumps(payload))
    if "trading halted" not in (hedged.consumer().reconcile_once()):
        pytest.fail(
            "Expected 'trading halted' in hedged.consumer().reconcile_once()"
        )
    if hedged.exchange.submissions:
        pytest.fail("Expected not hedged.exchange.submissions")


def test_zero_hedge_requires_exchange_hedge_mode(hedged) -> None:
    """Verify zero hedge requires exchange hedge mode."""
    from bybit_demo_bot.client import BybitDemoError

    hedged.exchange.hedge_mode = False
    hedged.exchange.position = {
        "symbol": "BTCUSDT",
        "positionIdx": 0,
        "side": "",
        "size": "0",
    }
    with pytest.raises(BybitDemoError, match="hedge position mode"):
        hedged.consumer().reconcile_once()
    if hedged.exchange.submissions:
        pytest.fail("Expected not hedged.exchange.submissions")
