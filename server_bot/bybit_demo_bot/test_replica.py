# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for bybit demo bot test replica."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol
from unittest.mock import Mock

import pytest

from bybit_demo_bot import cli
from bybit_demo_bot.client import BybitDemoError
from bybit_demo_bot.replica import ReferenceReplicaBot
from bybit_demo_bot.test_bybit_demo import config
from reference_bot.trading import LocalPosition
from shared.reference_target import publish_target

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
    """Setup."""
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "demo.json")
    client = Mock()
    client.position.return_value = None
    client.available_usdt.return_value = Decimal(100)
    client.last_price.return_value = Decimal("1.34")
    client.instruments.return_value = {
        "lotSizeFilter": {
            "qtyStep": "0.1",
            "minOrderQty": "0.1",
            "minNotionalValue": "5",
            "maxMktOrderQty": "10000",
        }
    }
    client.market_order.return_value = "exchange-order"
    client.order_by_link.return_value = None
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
        state = SimpleNamespace(
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
        payload = json.loads(bot.target_path.read_text())
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
    client.order_by_link.return_value = {"orderStatus": "Filled"}
    client.position.return_value = (
        {"side": side, "size": qty, "avgPrice": "1.34"} if side else None
    )
    if "fill confirmed" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'fill confirmed' in bot.reconcile_once(NOW)")


@pytest.mark.parametrize(
    "reference_side,exchange_side", [("Long", "Buy"), ("Short", "Sell")]
)
def test_exact_reference_quantity_no_ninety_percent_allocation(
    setup: ReplicaSetup, reference_side, exchange_side
) -> None:
    """Verify exact reference quantity no ninety percent allocation."""
    bot, client, target = setup
    target(side=reference_side)
    if "submitted entry" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'submitted entry' in bot.reconcile_once(NOW)")
    args, kwargs = client.market_order.call_args
    if args != ("XRPUSDT", exchange_side, Decimal("7.3")):
        pytest.fail(
            "Expected args == ('XRPUSDT', exchange_side, Decimal('7.3'))"
        )
    if (kwargs["reduce_only"]) is not (False):
        pytest.fail("Expected kwargs['reduce_only'] is False")
    if len(kwargs["order_link_id"]) != 36:
        pytest.fail("Expected len(kwargs['order_link_id']) == 36")
    fill(bot, client, exchange_side)
    if (bot.reconcile_once(NOW)) is not (None):
        pytest.fail("Expected bot.reconcile_once(NOW) is None")
    client.set_protection.assert_called_with("XRPUSDT", Decimal(0), Decimal(0))
    for _ in range(3):
        if (bot.reconcile_once(NOW)) is not (None):
            pytest.fail("Expected bot.reconcile_once(NOW) is None")
    client.market_order.assert_called_once()


def test_reference_close_and_same_direction_reentry_are_copied(
    setup: ReplicaSetup,
) -> None:
    """Verify reference close and same direction reentry are copied."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    target(side=None)
    if "submitted reduce Buy" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'submitted reduce Buy' in bot.reconcile_once(NOW)"
        )
    fill(bot, client, side=None)
    target(qty="7.2", entry="2026-09-13T14:16:01+00:00")
    if "submitted entry Sell 7.2" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'submitted entry Sell 7.2' in bot.reconcile_once(NOW)"
        )
    if client.market_order.call_count != 3:
        pytest.fail("Expected client.market_order.call_count == 3")


def test_same_side_new_reference_trade_is_not_mistaken_for_old_position(
    setup: ReplicaSetup,
) -> None:
    """Verify same side new reference trade is not mistaken for old position."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    # Close and reopen happened between polls: same side/quantity but new identity.
    target(entry="2026-09-13T14:16:01+00:00")
    if "submitted reduce" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'submitted reduce' in bot.reconcile_once(NOW)")
    fill(bot, client, side=None)
    if "submitted entry" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'submitted entry' in bot.reconcile_once(NOW)")


def test_reverse_confirms_flat_before_opening(setup: ReplicaSetup) -> None:
    """Verify reverse confirms flat before opening."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    target(side="Long", entry="2026-09-13T14:16:01+00:00")
    _ = bot.reconcile_once(NOW)
    if (client.market_order.call_args.kwargs["reduce_only"]) is not (True):
        pytest.fail(
            "Expected client.market_order.call_args.kwargs['reduce_only'] is"
            " True"
        )
    if "awaiting" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'awaiting' in bot.reconcile_once(NOW)"
        )  # Close order filled but position view still old.
    if client.market_order.call_count != 2:
        pytest.fail("Expected client.market_order.call_count == 2")
    fill(bot, client, side=None)
    if "submitted entry Buy" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'submitted entry Buy' in bot.reconcile_once(NOW)")


def test_upgrade_adopts_matching_position_and_clears_existing_sl_tp(
    setup: ReplicaSetup,
) -> None:
    """Verify upgrade adopts matching position and clears existing sl tp."""
    bot, client, _ = setup
    client.position.return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
        "stopLoss": "1.35",
        "takeProfit": "1.30",
    }
    bot.state.pending_protection_side = "Sell"
    if (bot.reconcile_once(NOW)) is not (None):
        pytest.fail("Expected bot.reconcile_once(NOW) is None")
    client.market_order.assert_not_called()
    client.set_protection.assert_called_once_with(
        "XRPUSDT", Decimal(0), Decimal(0)
    )
    if (bot.state.pending_protection_side) is not (None):
        pytest.fail("Expected bot.state.pending_protection_side is None")


def test_upgrade_replaces_different_quantity_instead_of_silently_scaling(
    setup: ReplicaSetup,
) -> None:
    """Verify upgrade replaces different quantity instead of silently scaling."""
    bot, client, _ = setup
    client.position.return_value = {
        "side": "Sell",
        "size": "7.5",
        "avgPrice": "1.34",
    }
    if "submitted reduce Buy 7.5" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'submitted reduce Buy 7.5' in bot.reconcile_once(NOW)"
        )
    fill(bot, client, side=None)
    if "submitted entry Sell 7.3" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'submitted entry Sell 7.3' in bot.reconcile_once(NOW)"
        )


def test_ambiguous_entry_is_reconciled_after_restart_without_resubmission(
    setup: ReplicaSetup,
) -> None:
    """Verify ambiguous entry is reconciled after restart without resubmission."""
    bot, client, _ = setup
    client.market_order.side_effect = BybitDemoError("connection reset")
    if "intent retained" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'intent retained' in bot.reconcile_once(NOW)")
    pending = bot.state.replica_order.copy()
    resumed = ReferenceReplicaBot(
        bot.config,
        client,
        cli.DemoState.load_or_create(True),
        target_path=bot.target_path,
    )
    if "awaiting" not in (resumed.reconcile_once(NOW)):
        pytest.fail("Expected 'awaiting' in resumed.reconcile_once(NOW)")
    client.market_order.assert_called_once()
    client.order_by_link.assert_called_with("XRPUSDT", pending["link"])
    fill(resumed, client)
    if not (resumed.state.replica_position_id):
        pytest.fail("Expected resumed.state.replica_position_id")


def test_partial_fill_does_not_trigger_duplicate_entry(
    setup: ReplicaSetup,
) -> None:
    """Verify partial fill does not trigger duplicate entry."""
    bot, client, _ = setup
    _ = bot.reconcile_once(NOW)
    client.position.return_value = {
        "side": "Sell",
        "size": "2",
        "avgPrice": "1.34",
    }
    client.order_by_link.return_value = {"orderStatus": "PartiallyFilled"}
    if "awaiting" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'awaiting' in bot.reconcile_once(NOW)")
    client.market_order.assert_called_once()


@pytest.mark.parametrize(
    "status", ["Rejected", "Cancelled", "PartiallyFilledCanceled"]
)
def test_failed_order_halts_and_flattens_partial_position(
    setup: ReplicaSetup, status
) -> None:
    """Verify failed order halts and flattens partial position."""
    bot, client, _ = setup
    _ = bot.reconcile_once(NOW)
    client.position.return_value = {
        "side": "Sell",
        "size": "2",
        "avgPrice": "1.34",
    }
    client.order_by_link.return_value = {"orderStatus": status}
    if "emergency close submitted" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'emergency close submitted' in bot.reconcile_once(NOW)"
        )
    if not (bot.state.halted_reason):
        pytest.fail("Expected bot.state.halted_reason")
    if client.market_order.call_args.args != ("XRPUSDT", "Buy", Decimal(2)):
        pytest.fail(
            "Expected client.market_order.call_args.args == ('XRPUSDT', 'Buy',"
            " Decimal(2))"
        )
    if not (client.market_order.call_args.kwargs["reduce_only"]):
        pytest.fail(
            "Expected client.market_order.call_args.kwargs['reduce_only']"
        )


def test_timeout_halts_cancels_and_late_fill_is_closed(
    setup: ReplicaSetup,
) -> None:
    """Verify timeout halts cancels and late fill is closed."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    later = NOW + timedelta(seconds=61)
    target(now=later)
    if "trading halted" not in (bot.reconcile_once(later)):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once(later)")
    client.cancel_by_link.assert_called_once()
    client.position.return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
    }
    if "emergency close submitted" not in (bot.reconcile_once(later)):
        pytest.fail(
            "Expected 'emergency close submitted' in bot.reconcile_once(later)"
        )
    if not (client.market_order.call_args.kwargs["reduce_only"]):
        pytest.fail(
            "Expected client.market_order.call_args.kwargs['reduce_only']"
        )


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
    setup: ReplicaSetup, changes
) -> None:
    """Verify invalid or stale reference halts and closes."""
    bot, client, target = setup
    client.position.return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
    }
    target(**changes)
    if "emergency close submitted" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'emergency close submitted' in bot.reconcile_once(NOW)"
        )
    if not (bot.state.halted_reason):
        pytest.fail("Expected bot.state.halted_reason")
    if not (client.market_order.call_args.kwargs["reduce_only"]):
        pytest.fail(
            "Expected client.market_order.call_args.kwargs['reduce_only']"
        )


def test_stale_reference_during_pending_entry_still_halts(
    setup: ReplicaSetup,
) -> None:
    """Verify stale reference during pending entry still halts."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    target(published_at="2026-09-13T14:10:00+00:00")
    if "trading halted" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once(NOW)")
    client.cancel_by_link.assert_called_once()


def test_missing_reference_never_treated_as_flat_signal(
    setup: ReplicaSetup,
) -> None:
    """Verify missing reference never treated as flat signal."""
    bot, client, _ = setup
    bot.target_path.unlink()
    bot.state.replica_initialized = True
    if "trading halted" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once(NOW)")
    client.market_order.assert_not_called()
    if not (bot.state.halted_reason):
        pytest.fail("Expected bot.state.halted_reason")


def test_unexpected_external_close_does_not_create_reentry_loop(
    setup: ReplicaSetup,
) -> None:
    """Verify unexpected external close does not create reentry loop."""
    bot, client, _ = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    client.position.return_value = None
    if "trading halted" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once(NOW)")
    if "disappeared" not in (bot.state.halted_reason):
        pytest.fail("Expected 'disappeared' in bot.state.halted_reason")
    client.market_order.assert_called_once()


def test_insufficient_balance_does_not_scale_down_reference_order(
    setup: ReplicaSetup,
) -> None:
    """Verify insufficient balance does not scale down reference order."""
    bot, client, _ = setup
    client.available_usdt.return_value = Decimal(5)
    if "trading halted" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once(NOW)")
    if "fund" not in (bot.state.halted_reason):
        pytest.fail("Expected 'fund' in bot.state.halted_reason")
    client.market_order.assert_not_called()


def test_legacy_unresolved_entry_blocks_migration(setup: ReplicaSetup) -> None:
    """Verify legacy unresolved entry blocks migration."""
    bot, client, _ = setup
    bot.state.pending_protection_side = "Sell"
    if "trading halted" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once(NOW)")
    client.market_order.assert_not_called()


def test_new_reference_run_replaces_existing_trade(setup: ReplicaSetup) -> None:
    """Verify new reference run replaces existing trade."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    target(reference_run="2026-09-13T14:00:00+00:00")
    if "submitted reduce" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'submitted reduce' in bot.reconcile_once(NOW)")


def test_new_start_flat_snapshot_waits_without_date_range_error(
    setup: ReplicaSetup,
) -> None:
    """Verify new start flat snapshot waits without date range error."""
    bot, client, target = setup
    target(side=None, reference_run=NOW.isoformat(), last_processed_candle=None)
    if (bot.reconcile_once(NOW)) is not (None):
        pytest.fail("Expected bot.reconcile_once(NOW) is None")
    client.market_order.assert_not_called()


def test_failed_cancellation_does_not_prevent_emergency_close(
    setup: ReplicaSetup,
) -> None:
    """Verify failed cancellation does not prevent emergency close."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    client.position.return_value = {
        "side": "Sell",
        "size": "1",
        "avgPrice": "1.34",
    }
    client.order_by_link.side_effect = BybitDemoError("history unavailable")
    target(published_at="2026-09-13T14:10:00+00:00")
    if "emergency close submitted" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'emergency close submitted' in bot.reconcile_once(NOW)"
        )
    if not (client.market_order.call_args.kwargs["reduce_only"]):
        pytest.fail(
            "Expected client.market_order.call_args.kwargs['reduce_only']"
        )


def test_default_demo_command_uses_replica_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify default demo command uses replica executor."""
    from bybit_demo_bot import replica

    config_path = tmp_path / "config.json"
    _ = config_path.write_text(config(ticker="XRP_USDT").to_json())
    env = tmp_path / "demo.env"
    _ = env.write_text("BYBIT_API_KEY=test\nBYBIT_API_SECRET=test\n")
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "demo.json")
    monkeypatch.setattr(cli, "LOCK_FILE", tmp_path / "demo.lock")
    factory = Mock()
    monkeypatch.setattr(replica, "ReferenceReplicaBot", factory)
    cli.run_demo_command(config_path, env, resume=True)
    factory.assert_called_once()
    factory.return_value.run.assert_called_once_with(2)


def test_client_order_link_lookup_and_cancel() -> None:
    """Verify client order link lookup and cancel."""
    from bybit_demo_bot.client import BybitDemoClient

    client = BybitDemoClient("key", "secret")
    client._request = Mock(
        side_effect=[
            {"orderId": "one"},
            {"list": []},
            {"list": [{"orderStatus": "Filled"}]},
            {},
        ]
    )
    if (
        client.market_order(
            "XRPUSDT", "Sell", Decimal("7.3"), order_link_id="ref-test"
        )
        != "one"
    ):
        pytest.fail(
            "Expected client.market_order('XRPUSDT', 'Sell', Decimal('7.3'),"
            " order_link_id='ref-test') == 'one'"
        )
    if client._request.call_args.args[2]["orderLinkId"] != "ref-test":
        pytest.fail(
            "Expected client._request.call_args.args[2]['orderLinkId'] =="
            " 'ref-test'"
        )
    if client.order_by_link("XRPUSDT", "ref-test")["orderStatus"] != "Filled":
        pytest.fail(
            "Expected client.order_by_link('XRPUSDT',"
            " 'ref-test')['orderStatus'] == 'Filled'"
        )
    if client._request.call_args.args[1] != "/v5/order/history":
        pytest.fail(
            "Expected client._request.call_args.args[1] == '/v5/order/history'"
        )
    client.cancel_by_link("XRPUSDT", "ref-test")
    if client._request.call_args.args[2]["orderLinkId"] != "ref-test":
        pytest.fail(
            "Expected client._request.call_args.args[2]['orderLinkId'] =="
            " 'ref-test'"
        )


def test_protection_clear_failure_halts_and_closes(setup: ReplicaSetup) -> None:
    """Verify protection clear failure halts and closes."""
    bot, client, _ = setup
    client.position.return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
    }
    client.set_protection.side_effect = BybitDemoError(
        "cannot remove protection"
    )
    if "emergency close submitted" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'emergency close submitted' in bot.reconcile_once(NOW)"
        )
    if not (bot.state.halted_reason):
        pytest.fail("Expected bot.state.halted_reason")


def test_rejected_close_cannot_open_opposite_position(
    setup: ReplicaSetup,
) -> None:
    """Verify rejected close cannot open opposite position."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    fill(bot, client)
    target(side="Long")
    _ = bot.reconcile_once(NOW)
    client.order_by_link.return_value = {"orderStatus": "Rejected"}
    if "trading halted" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once(NOW)")
    if not (
        all(
            call.kwargs.get("reduce_only")
            for call in client.market_order.call_args_list[1:]
        )
    ):
        pytest.fail(
            "Expected all((call.kwargs.get('reduce_only') for call in"
            " client.market_order.call_args_list[1:]))"
        )


def test_startup_allows_reference_to_publish_but_never_enters_without_target(
    setup: ReplicaSetup,
) -> None:
    """Verify startup allows reference to publish but never enters without target."""
    bot, client, target = setup
    bot.target_path.unlink()
    if "waiting for initial" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'waiting for initial' in bot.reconcile_once(NOW)")
    if bot.state.halted_reason:
        pytest.fail("Expected not bot.state.halted_reason")
    client.market_order.assert_not_called()
    target()
    if "submitted entry" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'submitted entry' in bot.reconcile_once(NOW)")


def test_startup_missing_target_eventually_halts(setup: ReplicaSetup) -> None:
    """Verify startup missing target eventually halts."""
    bot, client, _ = setup
    bot.target_path.unlink()
    _ = bot.reconcile_once(NOW)
    if "trading halted" not in (
        bot.reconcile_once(NOW + timedelta(seconds=121))
    ):
        pytest.fail(
            "Expected 'trading halted' in bot.reconcile_once(NOW +"
            " timedelta(seconds=121))"
        )
    client.market_order.assert_not_called()


def test_restart_accepts_already_filled_order_even_after_timeout(
    setup: ReplicaSetup,
) -> None:
    """Verify restart accepts already filled order even after timeout."""
    bot, client, target = setup
    _ = bot.reconcile_once(NOW)
    later = NOW + timedelta(minutes=2)
    target(now=later)
    client.order_by_link.return_value = {"orderStatus": "Filled"}
    client.position.return_value = {
        "side": "Sell",
        "size": "7.3",
        "avgPrice": "1.34",
    }
    resumed = ReferenceReplicaBot(
        bot.config,
        client,
        cli.DemoState.load_or_create(True),
        target_path=bot.target_path,
    )
    if "fill confirmed" not in (resumed.reconcile_once(later)):
        pytest.fail(
            "Expected 'fill confirmed' in resumed.reconcile_once(later)"
        )
    if resumed.state.halted_reason:
        pytest.fail("Expected not resumed.state.halted_reason")
    client.market_order.assert_called_once()


def test_slow_position_request_cannot_make_fresh_heartbeat_look_future(
    setup: ReplicaSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify slow position request cannot make fresh heartbeat look future."""
    import bybit_demo_bot.replica as module

    bot, client, target = setup
    clock = [NOW]

    class Clock(datetime):
        """Represent clock."""

        @classmethod
        def now(cls, tz=None):
            """Now."""
            return clock[0]

    monkeypatch.setattr(module, "datetime", Clock)

    def slow_position(_) -> None:
        """Slow position."""
        clock[0] += timedelta(seconds=10)
        target(now=clock[0])

    client.position.side_effect = slow_position
    if "submitted entry" not in (bot.reconcile_once()):
        pytest.fail("Expected 'submitted entry' in bot.reconcile_once()")
    if bot.state.halted_reason:
        pytest.fail("Expected not bot.state.halted_reason")


def test_snapshot_read_delay_uses_clock_after_read(
    setup: ReplicaSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify snapshot read delay uses clock after read."""
    import bybit_demo_bot.replica as module

    bot, _, target = setup
    later = NOW + timedelta(seconds=10)
    target(now=later)
    clock = Mock()
    clock.now.return_value = later
    monkeypatch.setattr(module, "datetime", clock)
    clock.fromisoformat.side_effect = datetime.fromisoformat
    if bot._target()["side"] != "Sell":
        pytest.fail("Expected bot._target()['side'] == 'Sell'")


def test_heartbeat_error_records_measured_age_and_timestamps(
    setup: ReplicaSetup,
) -> None:
    """Verify heartbeat error records measured age and timestamps."""
    bot, _, target = setup
    target(published_at=(NOW + timedelta(seconds=10)).isoformat())
    with pytest.raises(
        RuntimeError, match="age=-10.000s; published_at=.*checked_at="
    ):
        _ = bot._target(NOW)


def test_halted_flat_state_clears_old_position_but_preserves_order_intent(
    setup: ReplicaSetup,
) -> None:
    """Verify halted flat state clears old position but preserves order intent."""
    bot, client, _ = setup
    bot.state.halted_reason = "stale"
    bot.state.observed_position_side = "Buy"
    bot.state.replica_position_id = "old"
    bot.state.replica_order = {"link": "old-order"}
    client.order_by_link.return_value = None
    _ = bot.reconcile_once(NOW)
    saved = cli.DemoState.load_or_create(True)
    if (saved.observed_position_side) is not (None):
        pytest.fail("Expected saved.observed_position_side is None")
    if (saved.replica_position_id) is not (None):
        pytest.fail("Expected saved.replica_position_id is None")
    if saved.replica_order != {"link": "old-order"}:
        pytest.fail("Expected saved.replica_order == {'link': 'old-order'}")
    if saved.halted_reason != "stale":
        pytest.fail("Expected saved.halted_reason == 'stale'")


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
    setup: ReplicaSetup, monkeypatch: pytest.MonkeyPatch, blocked
) -> None:
    """Verify checked recovery is read only on exchange and preserves failed state."""
    from bybit_demo_bot.recover import recover

    bot, client, target = setup
    bot.state.halted_reason = (
        "reference target unavailable: reference heartbeat is stale or in the"
        " future"
    )
    bot.state.observed_position_side = "Buy"
    bot.state.replica_position_id = "old"
    client.has_open_orders.return_value = False
    original_target = bot._target
    monkeypatch.setattr(bot, "_target", lambda: original_target(NOW))
    if blocked == "position":
        client.position.return_value = {"side": "Buy", "size": "7.3"}
    elif blocked == "open_order":
        client.has_open_orders.return_value = True
    elif blocked in ("unknown_order", "pending_order"):
        bot.state.replica_order = {"link": "old"}
        client.order_by_link.return_value = (
            None if blocked == "unknown_order" else {"orderStatus": "New"}
        )
    elif blocked == "legacy":
        bot.state.pending_protection_side = "Buy"
    elif blocked == "stale":
        target(published_at="2026-09-13T14:00:00+00:00")
    bot.state.save()
    before = cli.STATE_FILE.read_bytes()
    if blocked:
        with pytest.raises(RuntimeError):
            _ = recover(bot)
        if cli.STATE_FILE.read_bytes() != before:
            pytest.fail("Expected cli.STATE_FILE.read_bytes() == before")
    else:
        if "Halt cleared" not in (recover(bot)):
            pytest.fail("Expected 'Halt cleared' in recover(bot)")
        saved = cli.DemoState.load_or_create(True)
        if (saved.halted_reason) is not (None):
            pytest.fail("Expected saved.halted_reason is None")
        if (saved.replica_position_id) is not (None):
            pytest.fail("Expected saved.replica_position_id is None")
        backups = list(cli.STATE_FILE.parent.glob("*.recovery-*.bak"))
        if len(backups) != 1:
            pytest.fail("Expected len(backups) == 1")
        if backups[0].read_bytes() != before:
            pytest.fail("Expected backups[0].read_bytes() == before")
    client.market_order.assert_not_called()
    client.cancel_by_link.assert_not_called()
    client.set_protection.assert_not_called()


def test_recovery_command_refuses_running_service(
    setup: ReplicaSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify recovery command refuses running service."""
    from bybit_demo_bot.recover import recover_command

    bot, _, _ = setup
    monkeypatch.setattr(
        cli, "LOCK_FILE", bot.target_path.parent / "instance.lock"
    )
    monkeypatch.setattr(cli.fcntl, "flock", Mock(side_effect=BlockingIOError))
    with pytest.raises(RuntimeError, match="Stop bybit-demo.service"):
        _ = recover_command(bot.target_path, bot.target_path)


def test_latched_reference_halt_reports_recovery_without_resuming_orders(
    setup: ReplicaSetup,
) -> None:
    """Verify latched reference halt reports recovery without resuming orders."""
    bot, client, target = setup
    target(published_at=(NOW - timedelta(minutes=3)).isoformat())
    if "trading halted" not in (bot.reconcile_once(NOW)):
        pytest.fail("Expected 'trading halted' in bot.reconcile_once(NOW)")
    original_reason = bot.state.halted_reason
    if "current reference unavailable" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'current reference unavailable' in"
            " bot.reconcile_once(NOW)"
        )
    target(now=NOW)
    if "current reference healthy; checked recovery required" not in (
        bot.reconcile_once(NOW)
    ):
        pytest.fail(
            "Expected 'current reference healthy; checked recovery required' in"
            " bot.reconcile_once(NOW)"
        )
    if bot.state.halted_reason != original_reason:
        pytest.fail("Expected bot.state.halted_reason == original_reason")
    client.market_order.assert_not_called()


def test_repeated_flat_halt_logs_are_throttled_but_reconciliation_continues(
    setup: ReplicaSetup,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify repeated flat halt logs are throttled but reconciliation continues."""
    bot, _client, _ = setup
    ticks = [0.0]
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    monkeypatch.setattr(cli, "monotonic", lambda: ticks[0])

    def sleep(seconds) -> None:
        """Sleep."""
        ticks[0] += seconds
        if ticks[0] >= 65:
            bot.stop()

    monkeypatch.setattr(cli, "sleep", sleep)
    bot.reconcile_once = Mock(
        return_value="trading halted: saved reason; exchange position flat"
    )
    bot.run(2)
    if not ((bot.reconcile_once.call_count) > (30)):
        pytest.fail("Expected bot.reconcile_once.call_count > 30")
    if capsys.readouterr().out.count("trading halted:") != 2:
        pytest.fail(
            "Expected capsys.readouterr().out.count('trading halted:') == 2"
        )


def test_old_engine_snapshot_is_rejected_even_with_matching_config(
    setup: ReplicaSetup,
) -> None:
    """Verify old engine snapshot is rejected even with matching config."""
    import hashlib

    bot, client, target = setup
    target(
        config_fingerprint=hashlib.sha256(
            bot.config.to_json().encode()
        ).hexdigest()
    )
    if "configuration/version does not match" not in (bot.reconcile_once(NOW)):
        pytest.fail(
            "Expected 'configuration/version does not match' in"
            " bot.reconcile_once(NOW)"
        )
    client.market_order.assert_not_called()
