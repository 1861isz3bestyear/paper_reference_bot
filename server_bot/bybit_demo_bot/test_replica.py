# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for bybit demo bot test replica."""

import hashlib
import json
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, override
from unittest.mock import Mock

import pytest

import bybit_demo_bot.replica as module
from bybit_demo_bot import cli, executor
from bybit_demo_bot import state as execution_state
from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from bybit_demo_bot.recover import recover, recover_command
from bybit_demo_bot.replica import ReferenceReplicaBot
from bybit_demo_bot.test_bybit_demo import config
from reference_bot.cli import BotState
from reference_bot.trading import LocalPosition
from shared.reference_target import publish_target
from shared.testing_support import ignoring_arguments
from shared.validation import json_object, object_mapping, string
from tests.mock_support import call_args, call_kwargs, mock_child, mock_result

if TYPE_CHECKING:
    from pathlib import Path

ORDER_LINK_LENGTH = 36
REVERSAL_ORDER_COUNT = 3
ENTRY_AND_EXIT_COUNT = 2
HALT_TEST_DURATION_SECONDS = 65
MINIMUM_RECONCILIATION_COUNT = 30


NOW = datetime(2026, 9, 13, 14, 16, 5, tzinfo=UTC)
RUN = "2026-09-13T10:58:55+00:00"
ENTRY = "2026-09-13T14:14:05+00:00"


class TargetWriter(Protocol):
    """Publish a reference target with optional test overrides."""

    def __call__(
        self,
        *,
        side: str | None = "Short",
        qty: str = "7.3",
        entry: str = ENTRY,
        now: datetime = NOW,
        **changes: object,
    ) -> None:
        """Write the requested target."""
        ...


type ReplicaSetup = tuple[ReferenceReplicaBot, Mock, TargetWriter]


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ReplicaSetup:
    """Create an isolated replica fixture.

    Returns:
        The setup result.

    """
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "demo.json")
    client = Mock()
    mock_child(client, "position").return_value = None
    mock_child(client, "available_usdt").return_value = Decimal(100)
    mock_child(client, "last_price").return_value = Decimal("1.34")
    mock_child(client, "instruments").return_value = {
        "lotSizeFilter": {
            "qtyStep": "0.1",
            "minOrderQty": "0.1",
            "minNotionalValue": "5",
            "maxMktOrderQty": "10000",
        }
    }
    mock_child(client, "market_order").return_value = "exchange-order"
    mock_child(client, "order_by_link").return_value = None
    bot = ReferenceReplicaBot(
        config(ticker="XRP_USDT"),
        client,
        cli.DemoState(RUN),
        target_path=tmp_path / "target.json",
    )

    def target(
        *,
        side: str | None = "Short",
        qty: str = "7.3",
        entry: str = ENTRY,
        now: datetime = NOW,
        **changes: object,
    ) -> None:
        """Target."""
        state = BotState(
            launched_at=RUN,
            last_processed_candle=(
                now.replace(second=0) - timedelta(minutes=1)
            ).isoformat(),
        )
        pos = LocalPosition(
            "XRPUSDT",
            side,
            Decimal(qty) if side else Decimal(0),
            1.34 if side else None,
            entry if side else None,
        )
        publish_target(bot.target_path, bot.config, state, position=pos)
        payload = json_object(bot.target_path.read_text())
        payload["published_at"] = now.isoformat()
        payload.update(changes)
        _ = bot.target_path.write_text(json.dumps(payload))

    target()
    return bot, client, target


def fill(
    bot: ReferenceReplicaBot,
    client: Mock,
    side: str | None = "Sell",
    *,
    qty: str = "7.3",
) -> None:
    """Fill."""
    mock_child(client, "order_by_link").return_value = {"orderStatus": "Filled"}
    mock_child(client, "position").return_value = (
        {"side": side, "size": qty, "avgPrice": "1.34"} if side else None
    )
    assert "fill confirmed" in (bot.reconcile_once(NOW) or "")


@pytest.mark.parametrize(
    ("reference_side", "exchange_side"), [("Long", "Buy"), ("Short", "Sell")]
)
def test_exact_reference_quantity_no_ninety_percent_allocation(
    setup: ReplicaSetup, reference_side: str, exchange_side: str
) -> None:
    """Verify exact reference quantity no ninety percent allocation."""
    bot, client, target = setup
    target(side=reference_side)
    assert "submitted entry" in (bot.reconcile_once(NOW) or "")
    args = call_args(mock_child(client, "market_order"))
    kwargs = call_kwargs(mock_child(client, "market_order"))
    assert args == ("XRPUSDT", exchange_side, Decimal("7.3"))
    assert kwargs["reduce_only"] is False
    assert len(string(kwargs["order_link_id"])) == ORDER_LINK_LENGTH
    fill(bot, client, exchange_side)
    assert bot.reconcile_once(NOW) is None
    mock_child(client, "set_protection").assert_called_with(
        "XRPUSDT", Decimal(0), Decimal(0)
    )
    for _ in range(3):
        assert bot.reconcile_once(NOW) is None
    mock_child(client, "market_order").assert_called_once()


def test_reference_close_and_same_direction_reentry_are_copied(
    setup: ReplicaSetup,
) -> None:
    """Verify reference close and same direction reentry are copied."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    target(side=None)
    assert "submitted reduce Buy" in (bot.reconcile_once(NOW) or "")
    fill(bot, client, side=None)
    target(qty="7.2", entry="2026-09-13T14:16:01+00:00")
    assert "submitted entry Sell 7.2" in (bot.reconcile_once(NOW) or "")
    assert mock_child(client, "market_order").call_count == REVERSAL_ORDER_COUNT


def test_same_side_new_reference_trade_is_not_mistaken_for_old_position(
    setup: ReplicaSetup,
) -> None:
    """Verify same side new reference trade is not mistaken for old position."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    # Close and reopen happened between polls: same side/quantity but new
    # identity.
    target(entry="2026-09-13T14:16:01+00:00")
    assert "submitted reduce" in (bot.reconcile_once(NOW) or "")
    fill(bot, client, side=None)
    assert "submitted entry" in (bot.reconcile_once(NOW) or "")


def test_reverse_confirms_flat_before_opening(setup: ReplicaSetup) -> None:
    """Verify reverse confirms flat before opening."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    target(side="Long", entry="2026-09-13T14:16:01+00:00")
    _ = bot.reconcile_once(NOW)
    assert (
        call_kwargs(mock_child(client, "market_order"))["reduce_only"] is True
    )
    assert "awaiting" in (bot.reconcile_once(NOW) or "")
    assert mock_child(client, "market_order").call_count == ENTRY_AND_EXIT_COUNT
    fill(bot, client, side=None)
    assert "submitted entry Buy" in (bot.reconcile_once(NOW) or "")


def test_upgrade_adopts_matching_position_and_clears_existing_sl_tp(
    setup: ReplicaSetup,
) -> None:
    """Verify upgrade adopts matching position and clears existing sl tp."""
    bot, client, _ = setup
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
        "stopLoss": "1.35",
        "takeProfit": "1.30",
    }
    bot.state.pending_protection_side = "Sell"
    assert bot.reconcile_once(NOW) is None
    mock_child(client, "market_order").assert_not_called()
    mock_child(client, "set_protection").assert_called_once_with(
        "XRPUSDT", Decimal(0), Decimal(0)
    )
    assert bot.state.pending_protection_side is None


def test_upgrade_replaces_different_quantity_instead_of_silently_scaling(
    setup: ReplicaSetup,
) -> None:
    """Close mismatched quantities before adopting the reference."""
    bot, client, _ = setup
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "7.5",
        "avgPrice": "1.34",
    }
    assert "submitted reduce Buy 7.5" in (bot.reconcile_once(NOW) or "")
    fill(bot, client, side=None)
    assert "submitted entry Sell 7.3" in (bot.reconcile_once(NOW) or "")


def test_ambiguous_entry_is_reconciled_after_restart_without_resubmission(
    setup: ReplicaSetup,
) -> None:
    """Reconcile ambiguous entries across restarts without resubmission."""
    bot, client, _ = setup
    mock_child(client, "market_order").side_effect = BybitDemoError(
        "connection reset"
    )
    assert "intent retained" in (bot.reconcile_once(NOW) or "")
    assert bot.state.replica_order is not None
    pending = bot.state.replica_order.copy()
    resumed = ReferenceReplicaBot(
        bot.config,
        client,
        cli.DemoState.load_or_create(resume=True),
        target_path=bot.target_path,
    )
    assert "awaiting" in (resumed.reconcile_once(NOW) or "")
    mock_child(client, "market_order").assert_called_once()
    mock_child(client, "order_by_link").assert_called_with(
        "XRPUSDT", pending["link"]
    )
    fill(resumed, client)
    assert resumed.state.replica_position_id


def test_partial_fill_does_not_trigger_duplicate_entry(
    setup: ReplicaSetup,
) -> None:
    """Verify partial fill does not trigger duplicate entry."""
    bot, client, _ = setup
    _ = bot.reconcile_once(NOW)
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "2",
        "avgPrice": "1.34",
    }
    mock_child(client, "order_by_link").return_value = {
        "orderStatus": "PartiallyFilled"
    }
    assert "awaiting" in (bot.reconcile_once(NOW) or "")
    mock_child(client, "market_order").assert_called_once()


@pytest.mark.parametrize(
    "status", ["Rejected", "Cancelled", "PartiallyFilledCanceled"]
)
def test_failed_order_halts_and_flattens_partial_position(
    setup: ReplicaSetup, status: str
) -> None:
    """Verify failed order halts and flattens partial position."""
    bot, client, _ = setup
    _ = bot.reconcile_once(NOW)
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "2",
        "avgPrice": "1.34",
    }
    mock_child(client, "order_by_link").return_value = {"orderStatus": status}
    assert "emergency close submitted" in (bot.reconcile_once(NOW) or "")
    assert bot.state.halted_reason
    assert call_args(mock_child(client, "market_order")) == (
        "XRPUSDT",
        "Buy",
        Decimal(2),
    )
    assert call_kwargs(mock_child(client, "market_order"))["reduce_only"]


def test_timeout_halts_cancels_and_late_fill_is_closed(
    setup: ReplicaSetup,
) -> None:
    """Verify timeout halts cancels and late fill is closed."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    later = NOW + timedelta(seconds=61)
    target(now=later)
    assert "trading halted" in (bot.reconcile_once(later) or "")
    mock_child(client, "cancel_by_link").assert_called_once()
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
    }
    assert "emergency close submitted" in (bot.reconcile_once(later) or "")
    assert call_kwargs(mock_child(client, "market_order"))["reduce_only"]


@pytest.mark.parametrize(
    "changes",
    [
        {"published_at": "2026-09-13T14:10:00+00:00"},
        {"last_processed_candle": "2026-09-13T14:10:00+00:00"},
        {"config_fingerprint": "other"},
        {"symbol": "BTCUSDT"},
        {"quantity": "NaN"},
        {"side": "invalid"},
        {"position_id": None},
        {"version": 2},
    ],
)
def test_invalid_or_stale_reference_halts_and_closes(
    setup: ReplicaSetup, changes: dict[str, object]
) -> None:
    """Verify invalid or stale reference halts and closes."""
    bot, client, target = setup
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
    }
    target()
    payload = json_object(bot.target_path.read_text())
    payload.update(changes)
    _ = bot.target_path.write_text(json.dumps(payload))
    assert "emergency close submitted" in (bot.reconcile_once(NOW) or "")
    assert bot.state.halted_reason
    assert call_kwargs(mock_child(client, "market_order"))["reduce_only"]


def test_stale_reference_during_pending_entry_still_halts(
    setup: ReplicaSetup,
) -> None:
    """Verify stale reference during pending entry still halts."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    target(published_at="2026-09-13T14:10:00+00:00")
    assert "trading halted" in (bot.reconcile_once(NOW) or "")
    mock_child(client, "cancel_by_link").assert_called_once()


def test_missing_reference_never_treated_as_flat_signal(
    setup: ReplicaSetup,
) -> None:
    """Verify missing reference never treated as flat signal."""
    bot, client, _ = setup
    bot.target_path.unlink()
    bot.state.replica_initialized = True
    assert "trading halted" in (bot.reconcile_once(NOW) or "")
    mock_child(client, "market_order").assert_not_called()
    assert bot.state.halted_reason


def test_unexpected_external_close_does_not_create_reentry_loop(
    setup: ReplicaSetup,
) -> None:
    """Verify unexpected external close does not create reentry loop."""
    bot, client, _ = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    mock_child(client, "position").return_value = None
    assert "trading halted" in (bot.reconcile_once(NOW) or "")
    assert bot.state.halted_reason is not None
    assert "disappeared" in bot.state.halted_reason
    mock_child(client, "market_order").assert_called_once()


def test_insufficient_balance_does_not_scale_down_reference_order(
    setup: ReplicaSetup,
) -> None:
    """Verify insufficient balance does not scale down reference order."""
    bot, client, _ = setup
    mock_child(client, "available_usdt").return_value = Decimal(5)
    assert "trading halted" in (bot.reconcile_once(NOW) or "")
    assert bot.state.halted_reason is not None
    assert "fund" in bot.state.halted_reason
    mock_child(client, "market_order").assert_not_called()


def test_legacy_unresolved_entry_blocks_migration(setup: ReplicaSetup) -> None:
    """Verify legacy unresolved entry blocks migration."""
    bot, client, _ = setup
    bot.state.pending_protection_side = "Sell"
    assert "trading halted" in (bot.reconcile_once(NOW) or "")
    mock_child(client, "market_order").assert_not_called()


def test_new_reference_run_replaces_existing_trade(setup: ReplicaSetup) -> None:
    """Verify new reference run replaces existing trade."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    target(reference_run="2026-09-13T14:00:00+00:00")
    assert "submitted reduce" in (bot.reconcile_once(NOW) or "")


def test_new_start_flat_snapshot_waits_without_date_range_error(
    setup: ReplicaSetup,
) -> None:
    """Verify new start flat snapshot waits without date range error."""
    bot, client, target = setup
    target(side=None, reference_run=NOW.isoformat(), last_processed_candle=None)
    assert bot.reconcile_once(NOW) is None
    mock_child(client, "market_order").assert_not_called()


def test_failed_cancellation_does_not_prevent_emergency_close(
    setup: ReplicaSetup,
) -> None:
    """Verify failed cancellation does not prevent emergency close."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "1",
        "avgPrice": "1.34",
    }
    mock_child(client, "order_by_link").side_effect = BybitDemoError(
        "history unavailable"
    )
    target(published_at="2026-09-13T14:10:00+00:00")
    assert "emergency close submitted" in (bot.reconcile_once(NOW) or "")
    assert call_kwargs(mock_child(client, "market_order"))["reduce_only"]


def test_default_demo_command_uses_replica_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify default demo command uses replica executor."""
    config_path = tmp_path / "config.json"
    _ = config_path.write_text(config(ticker="XRP_USDT").to_json())
    env = tmp_path / "demo.env"
    _ = env.write_text("BYBIT_API_KEY=test\nBYBIT_API_SECRET=test\n")
    monkeypatch.setattr(execution_state, "STATE_FILE", tmp_path / "demo.json")
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "demo.lock")
    factory = Mock()
    monkeypatch.setattr(cli, "ReferenceReplicaBot", factory)
    cli.run_demo_command(config_path, env, resume=True)
    factory.assert_called_once()
    mock_child(mock_result(factory), "run").assert_called_once_with(2)


def test_client_order_link_lookup_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify client order link lookup and cancel."""
    client = BybitDemoClient("key", "secret")
    request = Mock(
        side_effect=[
            {"orderId": "one"},
            {"list": []},
            {"list": [{"orderStatus": "Filled"}]},
            {},
        ]
    )
    monkeypatch.setattr(client, "_request", request)
    assert (
        client.market_order(
            "XRPUSDT", "Sell", Decimal("7.3"), order_link_id="ref-test"
        )
        == "one"
    )
    assert object_mapping(call_args(request)[2])["orderLinkId"] == "ref-test"
    order = client.order_by_link("XRPUSDT", "ref-test")
    assert order is not None
    assert order["orderStatus"] == "Filled"
    assert call_args(request)[1] == "/v5/order/history"
    client.cancel_by_link("XRPUSDT", "ref-test")
    assert object_mapping(call_args(request)[2])["orderLinkId"] == "ref-test"


def test_protection_clear_failure_halts_and_closes(setup: ReplicaSetup) -> None:
    """Verify protection clear failure halts and closes."""
    bot, client, _ = setup
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
    }
    mock_child(client, "set_protection").side_effect = BybitDemoError(
        "cannot remove protection"
    )
    assert "emergency close submitted" in (bot.reconcile_once(NOW) or "")
    assert bot.state.halted_reason


def test_rejected_close_cannot_open_opposite_position(
    setup: ReplicaSetup,
) -> None:
    """Verify rejected close cannot open opposite position."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    target(side="Long")
    _ = bot.reconcile_once(NOW)
    mock_child(client, "order_by_link").return_value = {
        "orderStatus": "Rejected"
    }
    assert "trading halted" in (bot.reconcile_once(NOW) or "")
    assert all(
        call.kwargs.get("reduce_only")
        for call in mock_child(client, "market_order").call_args_list[1:]
    )


def test_startup_allows_reference_to_publish_but_never_enters_without_target(
    setup: ReplicaSetup,
) -> None:
    """Wait for initial publication before entering a position."""
    bot, client, target = setup
    bot.target_path.unlink()
    assert "waiting for initial" in (bot.reconcile_once(NOW) or "")
    assert not bot.state.halted_reason
    mock_child(client, "market_order").assert_not_called()
    target()
    assert "submitted entry" in (bot.reconcile_once(NOW) or "")


def test_startup_missing_target_eventually_halts(setup: ReplicaSetup) -> None:
    """Verify startup missing target eventually halts."""
    bot, client, _ = setup
    bot.target_path.unlink()
    _ = bot.reconcile_once(NOW)
    assert "trading halted" in (
        bot.reconcile_once(NOW + timedelta(seconds=121)) or ""
    )
    mock_child(client, "market_order").assert_not_called()


def test_restart_accepts_already_filled_order_even_after_timeout(
    setup: ReplicaSetup,
) -> None:
    """Verify restart accepts already filled order even after timeout."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    later = NOW + timedelta(minutes=2)
    target(now=later)
    mock_child(client, "order_by_link").return_value = {"orderStatus": "Filled"}
    mock_child(client, "position").return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
    }
    resumed = ReferenceReplicaBot(
        bot.config,
        client,
        cli.DemoState.load_or_create(resume=True),
        target_path=bot.target_path,
    )
    assert "fill confirmed" in (resumed.reconcile_once(later) or "")
    assert not resumed.state.halted_reason
    mock_child(client, "market_order").assert_called_once()


def test_slow_position_request_cannot_make_fresh_heartbeat_look_future(
    setup: ReplicaSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify slow position request cannot make fresh heartbeat look future."""
    bot, client, target = setup
    clock = [NOW]

    class Clock(datetime):
        """Represent clock."""

        @classmethod
        @override
        def now(cls, tz: tzinfo | None = None) -> datetime:
            """Now.

            Returns:
                The now result.

            """
            del tz
            return clock[0]

    monkeypatch.setattr(module, "datetime", Clock)

    def slow_position(_symbol: str) -> None:
        """Slow position."""
        clock[0] += timedelta(seconds=10)
        target(now=clock[0])

    mock_child(client, "position").side_effect = slow_position
    assert "submitted entry" in (bot.reconcile_once() or "")
    assert not bot.state.halted_reason


def test_snapshot_read_delay_uses_clock_after_read(
    setup: ReplicaSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify snapshot read delay uses clock after read."""
    bot, _, target = setup
    later = NOW + timedelta(seconds=10)
    target(now=later)
    clock = Mock()
    mock_child(clock, "now").return_value = later
    monkeypatch.setattr(module, "datetime", clock)
    mock_child(clock, "fromisoformat").side_effect = datetime.fromisoformat
    assert bot.load_reference_target().side == "Sell"


def test_heartbeat_error_records_measured_age_and_timestamps(
    setup: ReplicaSetup,
) -> None:
    """Verify heartbeat error records measured age and timestamps."""
    bot, _, target = setup
    target(published_at=(NOW + timedelta(seconds=10)).isoformat())
    with pytest.raises(
        RuntimeError, match=r"age=-10\.000s; published_at=.*checked_at="
    ):
        _ = bot.load_reference_target(NOW)


def test_halted_flat_state_clears_old_position_but_preserves_order_intent(
    setup: ReplicaSetup,
) -> None:
    """Preserve order intents when clearing halted flat positions."""
    bot, client, _ = setup
    bot.state.halted_reason = "stale"
    bot.state.observed_position_side = "Buy"
    bot.state.replica_position_id = "old"
    bot.state.replica_order = {"link": "old-order"}
    mock_child(client, "order_by_link").return_value = None
    _ = bot.reconcile_once(NOW)
    saved = cli.DemoState.load_or_create(resume=True)
    assert saved.observed_position_side is None
    assert saved.replica_position_id is None
    assert saved.replica_order == {"link": "old-order"}
    assert saved.halted_reason == "stale"


@pytest.mark.parametrize(
    "blocked",
    [
        None,
        "position",
        "open_order",
        "unknown_order",
        "pending_order",
        "legacy",
        "stale",
    ],
)
def test_checked_recovery_is_read_only_on_exchange_and_preserves_failed_state(
    setup: ReplicaSetup, monkeypatch: pytest.MonkeyPatch, blocked: str | None
) -> None:
    """Preserve exchange and failed state during checked recovery."""
    bot, client, target = setup
    bot.state.halted_reason = (
        "reference target unavailable: reference heartbeat is stale or in the"
        " future"
    )
    bot.state.observed_position_side = "Buy"
    bot.state.replica_position_id = "old"
    mock_child(client, "has_open_orders").return_value = False
    original_target = bot.load_reference_target
    monkeypatch.setattr(
        bot, "load_reference_target", lambda: original_target(NOW)
    )
    if blocked == "position":
        mock_child(client, "position").return_value = {
            "side": "Buy",
            "size": "7.3",
        }
    elif blocked == "open_order":
        mock_child(client, "has_open_orders").return_value = True
    elif blocked in {"unknown_order", "pending_order"}:
        bot.state.replica_order = {"link": "old"}
        mock_child(client, "order_by_link").return_value = (
            None if blocked == "unknown_order" else {"orderStatus": "New"}
        )
    elif blocked == "legacy":
        bot.state.pending_protection_side = "Buy"
    elif blocked == "stale":
        target(published_at="2026-09-13T14:00:00+00:00")
    bot.state.save()
    before = execution_state.STATE_FILE.read_bytes()
    if blocked:
        with pytest.raises(RuntimeError):
            _ = recover(bot)
        assert execution_state.STATE_FILE.read_bytes() == before
    else:
        assert "Halt cleared" in recover(bot)
        saved = cli.DemoState.load_or_create(resume=True)
        assert saved.halted_reason is None
        assert saved.replica_position_id is None
        backups = list(
            execution_state.STATE_FILE.parent.glob("*.recovery-*.bak")
        )
        assert len(backups) == 1
        assert backups[0].read_bytes() == before
    mock_child(client, "market_order").assert_not_called()
    mock_child(client, "cancel_by_link").assert_not_called()
    mock_child(client, "set_protection").assert_not_called()


def test_recovery_command_refuses_running_service(
    setup: ReplicaSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify recovery command refuses running service."""
    bot, _, _ = setup
    monkeypatch.setattr(
        cli, "LOCK_FILE", bot.target_path.parent / "instance.lock"
    )
    monkeypatch.setattr("fcntl.flock", Mock(side_effect=BlockingIOError))
    with pytest.raises(RuntimeError, match=r"Stop bybit-demo\.service"):
        _ = recover_command(bot.target_path, bot.target_path)


def test_latched_reference_halt_reports_recovery_without_resuming_orders(
    setup: ReplicaSetup,
) -> None:
    """Report recovery readiness while keeping the reference halt."""
    bot, client, target = setup
    target(published_at=(NOW - timedelta(minutes=3)).isoformat())
    assert "trading halted" in (bot.reconcile_once(NOW) or "")
    original_reason = bot.state.halted_reason
    assert "current reference unavailable" in (bot.reconcile_once(NOW) or "")
    target(now=NOW)
    assert "current reference healthy; checked recovery required" in (
        bot.reconcile_once(NOW) or ""
    )
    assert bot.state.halted_reason == original_reason
    mock_child(client, "market_order").assert_not_called()


def test_repeated_flat_halt_logs_are_throttled_but_reconciliation_continues(
    setup: ReplicaSetup,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Throttle repeated halt logs while continuing reconciliation."""
    bot, _client, _ = setup
    ticks = [0.0]
    monkeypatch.setattr("signal.signal", ignoring_arguments(lambda: None))
    monkeypatch.setattr(executor, "monotonic", lambda: ticks[0])

    def sleep(seconds: float) -> None:
        """Sleep."""
        ticks[0] += seconds
        if ticks[0] >= HALT_TEST_DURATION_SECONDS:
            bot.stop()

    monkeypatch.setattr(executor, "sleep", sleep)
    bot.reconcile_once = Mock(
        return_value="trading halted: saved reason; exchange position flat"
    )
    bot.run(2)
    assert bot.reconcile_once.call_count > MINIMUM_RECONCILIATION_COUNT
    assert (
        capsys.readouterr().out.count("trading halted:") == ENTRY_AND_EXIT_COUNT
    )


def test_old_engine_snapshot_is_rejected_even_with_matching_config(
    setup: ReplicaSetup,
) -> None:
    """Verify old engine snapshot is rejected even with matching config."""
    bot, client, target = setup
    target(
        config_fingerprint=hashlib.sha256(
            bot.config.to_json().encode()
        ).hexdigest()
    )
    assert "configuration/version does not match" in (
        bot.reconcile_once(NOW) or ""
    )
    mock_child(client, "market_order").assert_not_called()
