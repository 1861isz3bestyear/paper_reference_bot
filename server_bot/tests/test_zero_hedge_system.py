# Copyright 2026 anchored-vwam-backtest contributors
"""Execute both reference hedge legs over real HTTP boundaries."""

import json
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from bybit_demo_bot import hedge
from bybit_demo_bot.cli import DemoState
from bybit_demo_bot.client import BybitDemoError
from bybit_demo_bot.recover import recover
from reference_bot.config import PaperBotConfig
from shared.hedge_strategy import HEDGE_MODE
from shared.hedge_validation import parse_hedge_plan
from shared.validation import json_object, string

if TYPE_CHECKING:
    from tests.conftest import TradingSystem

HEDGE_LEG_COUNT = 2
ENTRY_AND_CLOSE_COUNT = 4


pytestmark = pytest.mark.integration


@pytest.fixture
def hedged(
    system: TradingSystem, monkeypatch: pytest.MonkeyPatch
) -> TradingSystem:
    """Hedged.

    Returns:
        The hedged result.

    """
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
            str(low),
            str(c),
            "10",
            str(c * 10),
        ]
        for i, (o, h, low, c) in enumerate(
            [(100, 100, 100, 100), (101, 102, 100, 102), (101, 101, 100, 100)]
        )
    ]
    system.exchange.hedge_mode = True
    system.producer().run_forever()
    system.cfg = cfg
    system.consumer = lambda: hedge.HedgeReplicaBot(
        cfg,
        system.client,
        DemoState.load_or_create(resume=True),
        target_path=system.target,
    )
    return system


def test_two_same_vwap_limits_both_fill_and_restart_does_not_duplicate(
    hedged: TradingSystem,
) -> None:
    """Verify two same vwap limits both fill and restart does not duplicate."""
    target = json_object(hedged.target.read_text())
    assert target["version"] == HEDGE_LEG_COUNT
    plan = parse_hedge_plan(target["hedge_plan"])
    assert set(plan["legs"]) == {"Long", "Short"}
    bot = hedged.consumer()
    action = bot.reconcile_once()
    assert "submitted Long limit" in (action or "")
    assert "submitted Short limit" in (action or "")
    assert bot.reconcile_once() is None
    orders = hedged.exchange.submissions
    assert len(orders) == HEDGE_LEG_COUNT
    assert [o["orderType"] for o in orders] == ["Limit", "Limit"]
    assert [o["positionIdx"] for o in orders] == [1, 2]
    assert orders[0]["price"] == orders[1]["price"]
    assert (
        float(string(orders[0]["takeProfit"]))
        > float(string(orders[0]["price"]))
        > float(string(orders[1]["takeProfit"]))
    )
    assert (
        float(string(orders[0]["stopLoss"]))
        < float(string(orders[0]["price"]))
        < float(string(orders[1]["stopLoss"]))
    )
    assert set(hedged.exchange.hedge_positions) == {1, 2}
    resumed = hedged.consumer()
    assert resumed.reconcile_once() is None
    assert len(orders) == HEDGE_LEG_COUNT
    # One native TP fills; the other leg remains independently protected.
    _ = hedged.exchange.hedge_positions.pop(1)
    _ = resumed.reconcile_once()
    assert set(hedged.exchange.hedge_positions) == {2}
    _ = hedged.exchange.hedge_positions.pop(2)
    _ = resumed.reconcile_once()
    assert resumed.state.hedge_state is not None
    assert resumed.state.hedge_state["complete"]
    assert hedged.consumer().reconcile_once() is None
    assert len(orders) == HEDGE_LEG_COUNT


def test_lost_first_response_is_reconciled_before_second_submission(
    hedged: TradingSystem,
) -> None:
    """Verify lost first response is reconciled before second submission."""
    hedged.exchange.lose_next_response = True
    assert "intent retained" in (hedged.consumer().reconcile_once() or "")
    assert len(hedged.exchange.submissions) == 1
    resumed = hedged.consumer()
    assert "submitted Short" in (resumed.reconcile_once() or "")
    _ = resumed.reconcile_once()
    assert len(hedged.exchange.submissions) == HEDGE_LEG_COUNT


def test_stale_reference_closes_both_indices_and_recovery_does_not_replay(
    hedged: TradingSystem,
) -> None:
    """Close both stale hedge legs and prevent replay during recovery."""
    bot = hedged.consumer()
    for _ in range(3):
        _ = bot.reconcile_once()
    assert len(hedged.exchange.hedge_positions) == HEDGE_LEG_COUNT
    original = hedged.clock.value
    hedged.clock.value += timedelta(minutes=3)
    assert "trading halted" in (bot.reconcile_once() or "")
    assert not hedged.exchange.hedge_positions
    closes = hedged.exchange.submissions[2:]
    assert {o["positionIdx"] for o in closes} == {1, 2}
    assert all(o["reduceOnly"] for o in closes)
    with pytest.raises(RuntimeError):
        _ = recover(bot)
    hedged.clock.value = original
    assert "Hedge halt cleared" in recover(bot)
    assert hedged.consumer().reconcile_once() is None
    assert len(hedged.exchange.submissions) == ENTRY_AND_CLOSE_COUNT


def test_expired_pending_limits_are_cancelled_without_replacement(
    hedged: TradingSystem,
) -> None:
    """Verify expired pending limits are cancelled without replacement."""
    hedged.exchange.defer_fills = True
    bot = hedged.consumer()
    _ = bot.reconcile_once()
    _ = bot.reconcile_once()
    assert len(hedged.exchange.submissions) == HEDGE_LEG_COUNT
    # Keep publisher/cursor fresh while the cycle's entry window expires.
    hedged.clock.value += timedelta(minutes=3)
    payload = json_object(hedged.target.read_text())
    payload["published_at"] = hedged.clock.value.isoformat()
    payload["last_processed_candle"] = (
        hedged.clock.value - timedelta(minutes=1, seconds=5)
    ).isoformat()
    _ = hedged.target.write_text(json.dumps(payload))
    _ = bot.reconcile_once()
    _ = bot.reconcile_once()
    assert all(
        o["orderStatus"] == "Cancelled" for o in hedged.exchange.orders.values()
    )
    assert len(hedged.exchange.submissions) == HEDGE_LEG_COUNT


def test_unknown_order_response_never_resubmits_until_exchange_acknowledges(
    hedged: TradingSystem,
) -> None:
    """Wait for exchange acknowledgement before submitting again."""
    bot = hedged.consumer()
    hedged.exchange.lose_next_response = True
    _ = bot.reconcile_once()
    assert bot.state.hedge_state is not None
    link = bot.state.hedge_state["orders"]["Long"]["link"]
    accepted = hedged.exchange.orders.pop(link)
    assert "no resubmission" in (hedged.consumer().reconcile_once() or "")
    assert len(hedged.exchange.submissions) == 1
    hedged.exchange.orders[link] = accepted
    assert "submitted Short" in (hedged.consumer().reconcile_once() or "")
    assert len(hedged.exchange.submissions) == HEDGE_LEG_COUNT


def test_rejected_second_order_flattens_the_first_leg(
    hedged: TradingSystem,
) -> None:
    """Verify rejected second order flattens the first leg."""
    bot = hedged.consumer()
    hedged.exchange.defer_fills = True
    _ = bot.reconcile_once()
    assert bot.state.hedge_state is not None
    hedged.exchange.fill(bot.state.hedge_state["orders"]["Long"]["link"])
    assert bot.state.hedge_state is not None
    link = bot.state.hedge_state["orders"]["Short"]["link"]
    hedged.exchange.orders[link]["orderStatus"] = "Rejected"
    hedged.exchange.defer_fills = False
    assert "trading halted" in (bot.reconcile_once() or "")
    assert not hedged.exchange.hedge_positions
    assert hedged.exchange.submissions[-1]["reduceOnly"] is True
    assert hedged.exchange.submissions[-1]["positionIdx"] == 1


def test_cleanup_attempts_both_position_closes_even_if_cancel_fails(
    hedged: TradingSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify cleanup attempts both position closes even if cancel fails."""
    bot = hedged.consumer()
    for _ in range(3):
        _ = bot.reconcile_once()
    assert bot.state.hedge_state is not None
    link = bot.state.hedge_state["orders"]["Long"]["link"]
    hedged.exchange.orders[link]["orderStatus"] = "PartiallyFilled"

    def failed_cancel(*_args: object) -> None:
        """Failed cancel.

        Raises:
            BybitDemoError: If the requested operation cannot safely proceed.

        """
        message = "cancel unavailable"
        raise BybitDemoError(message)

    monkeypatch.setattr(hedged.client, "cancel_by_link", failed_cancel)
    hedged.clock.value += timedelta(minutes=3)
    with pytest.raises(BybitDemoError, match="cleanup incomplete"):
        _ = bot.reconcile_once()
    assert not hedged.exchange.hedge_positions
    assert bot.state.halted_reason


@pytest.mark.parametrize(
    "change",
    [
        {"version": 1},
        {"config_fingerprint": "wrong"},
        {"hedge_plan": {"legs": {}}},
    ],
)
def test_invalid_hedge_targets_submit_no_entry(
    hedged: TradingSystem, change: dict[str, object]
) -> None:
    """Verify invalid hedge targets submit no entry."""
    payload = json_object(hedged.target.read_text())
    payload.update(change)
    _ = hedged.target.write_text(json.dumps(payload))
    assert "trading halted" in (hedged.consumer().reconcile_once() or "")
    assert not hedged.exchange.submissions


def test_zero_hedge_requires_exchange_hedge_mode(hedged: TradingSystem) -> None:
    """Verify zero hedge requires exchange hedge mode."""
    hedged.exchange.hedge_mode = False
    hedged.exchange.position = {
        "symbol": "BTCUSDT",
        "positionIdx": 0,
        "side": "",
        "size": "0",
    }
    with pytest.raises(BybitDemoError, match="hedge position mode"):
        _ = hedged.consumer().reconcile_once()
    assert not hedged.exchange.submissions
