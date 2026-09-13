from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bybit_demo_bot import cli
from bybit_demo_bot.client import BybitDemoError
from bybit_demo_bot.replica import ReferenceReplicaBot
from bybit_demo_bot.test_bybit_demo import config
from reference_bot.trading import LocalPosition
from shared.reference_target import publish_target

NOW = datetime(2026, 9, 13, 14, 16, 5, tzinfo=timezone.utc)
RUN = '2026-09-13T10:58:55+00:00'
ENTRY = '2026-09-13T14:14:05+00:00'


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'STATE_FILE', tmp_path / 'demo.json')
    client = Mock()
    client.position.return_value = None
    client.available_usdt.return_value = Decimal('100')
    client.last_price.return_value = Decimal('1.34')
    client.instruments.return_value = {'lotSizeFilter': {
        'qtyStep': '0.1', 'minOrderQty': '0.1', 'minNotionalValue': '5', 'maxMktOrderQty': '10000',
    }}
    client.market_order.return_value = 'exchange-order'
    client.order_by_link.return_value = None
    bot = ReferenceReplicaBot(config(ticker='XRP_USDT'), client, cli.DemoState(RUN),
                              target_path=tmp_path / 'target.json')

    def target(side='Short', qty='7.3', entry=ENTRY, now=NOW, **changes):
        state = SimpleNamespace(launched_at=RUN,
                                last_processed_candle=(now.replace(second=0) - timedelta(minutes=1)).isoformat())
        pos = LocalPosition('XRPUSDT', side, Decimal(qty) if side else Decimal('0'),
                            1.34 if side else None, entry if side else None)
        publish_target(bot.target_path, bot.config, state, pos)
        payload = json.loads(bot.target_path.read_text())
        payload['published_at'] = now.isoformat()
        payload.update(changes)
        bot.target_path.write_text(json.dumps(payload))
    target()
    return bot, client, target


def fill(bot, client, side='Sell', qty='7.3'):
    client.order_by_link.return_value = {'orderStatus': 'Filled'}
    client.position.return_value = {'side': side, 'size': qty, 'avgPrice': '1.34'} if side else None
    assert 'fill confirmed' in bot.reconcile_once(NOW)


@pytest.mark.parametrize('reference_side,exchange_side', [('Long', 'Buy'), ('Short', 'Sell')])
def test_exact_reference_quantity_no_ninety_percent_allocation(setup, reference_side, exchange_side):
    bot, client, target = setup
    target(side=reference_side)
    assert 'submitted entry' in bot.reconcile_once(NOW)
    args, kwargs = client.market_order.call_args
    assert args == ('XRPUSDT', exchange_side, Decimal('7.3'))
    assert kwargs['reduce_only'] is False
    assert len(kwargs['order_link_id']) == 36
    fill(bot, client, exchange_side)
    assert bot.reconcile_once(NOW) is None
    client.set_protection.assert_called_with('XRPUSDT', Decimal('0'), Decimal('0'))
    for _ in range(3):
        assert bot.reconcile_once(NOW) is None
    client.market_order.assert_called_once()


def test_reference_close_and_same_direction_reentry_are_copied(setup):
    bot, client, target = setup
    bot.reconcile_once(NOW)
    fill(bot, client)
    target(side=None)
    assert 'submitted reduce Buy' in bot.reconcile_once(NOW)
    fill(bot, client, side=None)
    target(qty='7.2', entry='2026-09-13T14:16:01+00:00')
    assert 'submitted entry Sell 7.2' in bot.reconcile_once(NOW)
    assert client.market_order.call_count == 3


def test_same_side_new_reference_trade_is_not_mistaken_for_old_position(setup):
    bot, client, target = setup
    bot.reconcile_once(NOW)
    fill(bot, client)
    # Close and reopen happened between polls: same side/quantity but new identity.
    target(entry='2026-09-13T14:16:01+00:00')
    assert 'submitted reduce' in bot.reconcile_once(NOW)
    fill(bot, client, side=None)
    assert 'submitted entry' in bot.reconcile_once(NOW)


def test_reverse_confirms_flat_before_opening(setup):
    bot, client, target = setup
    bot.reconcile_once(NOW)
    fill(bot, client)
    target(side='Long', entry='2026-09-13T14:16:01+00:00')
    bot.reconcile_once(NOW)
    assert client.market_order.call_args.kwargs['reduce_only'] is True
    assert 'awaiting' in bot.reconcile_once(NOW)  # Close order filled but position view still old.
    assert client.market_order.call_count == 2
    fill(bot, client, side=None)
    assert 'submitted entry Buy' in bot.reconcile_once(NOW)


def test_upgrade_adopts_matching_position_and_clears_existing_sl_tp(setup):
    bot, client, _ = setup
    client.position.return_value = {'side': 'Sell', 'size': '7.3', 'avgPrice': '1.34',
                                    'stopLoss': '1.35', 'takeProfit': '1.30'}
    bot.state.pending_protection_side = 'Sell'
    assert bot.reconcile_once(NOW) is None
    client.market_order.assert_not_called()
    client.set_protection.assert_called_once_with('XRPUSDT', Decimal('0'), Decimal('0'))
    assert bot.state.pending_protection_side is None


def test_upgrade_replaces_different_quantity_instead_of_silently_scaling(setup):
    bot, client, _ = setup
    client.position.return_value = {'side': 'Sell', 'size': '7.5', 'avgPrice': '1.34'}
    assert 'submitted reduce Buy 7.5' in bot.reconcile_once(NOW)
    fill(bot, client, side=None)
    assert 'submitted entry Sell 7.3' in bot.reconcile_once(NOW)


def test_ambiguous_entry_is_reconciled_after_restart_without_resubmission(setup):
    bot, client, _ = setup
    client.market_order.side_effect = BybitDemoError('connection reset')
    assert 'intent retained' in bot.reconcile_once(NOW)
    pending = bot.state.replica_order.copy()
    resumed = ReferenceReplicaBot(bot.config, client, cli.DemoState.load_or_create(True), target_path=bot.target_path)
    assert 'awaiting' in resumed.reconcile_once(NOW)
    client.market_order.assert_called_once()
    client.order_by_link.assert_called_with('XRPUSDT', pending['link'])
    fill(resumed, client)
    assert resumed.state.replica_position_id


def test_partial_fill_does_not_trigger_duplicate_entry(setup):
    bot, client, _ = setup
    bot.reconcile_once(NOW)
    client.position.return_value = {'side': 'Sell', 'size': '2', 'avgPrice': '1.34'}
    client.order_by_link.return_value = {'orderStatus': 'PartiallyFilled'}
    assert 'awaiting' in bot.reconcile_once(NOW)
    client.market_order.assert_called_once()


@pytest.mark.parametrize('status', ['Rejected', 'Cancelled', 'PartiallyFilledCanceled'])
def test_failed_order_halts_and_flattens_partial_position(setup, status):
    bot, client, _ = setup
    bot.reconcile_once(NOW)
    client.position.return_value = {'side': 'Sell', 'size': '2', 'avgPrice': '1.34'}
    client.order_by_link.return_value = {'orderStatus': status}
    assert 'emergency close submitted' in bot.reconcile_once(NOW)
    assert bot.state.halted_reason
    assert client.market_order.call_args.args == ('XRPUSDT', 'Buy', Decimal('2'))
    assert client.market_order.call_args.kwargs['reduce_only']


def test_timeout_halts_cancels_and_late_fill_is_closed(setup):
    bot, client, target = setup
    bot.reconcile_once(NOW)
    later = NOW + timedelta(seconds=61)
    target(now=later)
    assert 'trading halted' in bot.reconcile_once(later)
    client.cancel_by_link.assert_called_once()
    client.position.return_value = {'side': 'Sell', 'size': '7.3', 'avgPrice': '1.34'}
    assert 'emergency close submitted' in bot.reconcile_once(later)
    assert client.market_order.call_args.kwargs['reduce_only']


@pytest.mark.parametrize('changes', [
    {'published_at': '2026-09-13T14:10:00+00:00'},
    {'last_processed_candle': '2026-09-13T14:10:00+00:00'},
    {'config_fingerprint': 'other'}, {'symbol': 'BTCUSDT'}, {'quantity': 'NaN'},
    {'side': 'invalid'}, {'position_id': None}, {'version': 2},
])
def test_invalid_or_stale_reference_halts_and_closes(setup, changes):
    bot, client, target = setup
    client.position.return_value = {'side': 'Sell', 'size': '7.3', 'avgPrice': '1.34'}
    target(**changes)
    assert 'emergency close submitted' in bot.reconcile_once(NOW)
    assert bot.state.halted_reason
    assert client.market_order.call_args.kwargs['reduce_only']


def test_stale_reference_during_pending_entry_still_halts(setup):
    bot, client, target = setup
    bot.reconcile_once(NOW)
    target(published_at='2026-09-13T14:10:00+00:00')
    assert 'trading halted' in bot.reconcile_once(NOW)
    client.cancel_by_link.assert_called_once()


def test_missing_reference_never_treated_as_flat_signal(setup):
    bot, client, _ = setup
    bot.target_path.unlink()
    bot.state.replica_initialized = True
    assert 'trading halted' in bot.reconcile_once(NOW)
    client.market_order.assert_not_called()
    assert bot.state.halted_reason


def test_unexpected_external_close_does_not_create_reentry_loop(setup):
    bot, client, _ = setup
    bot.reconcile_once(NOW)
    fill(bot, client)
    client.position.return_value = None
    assert 'trading halted' in bot.reconcile_once(NOW)
    assert 'disappeared' in bot.state.halted_reason
    client.market_order.assert_called_once()


def test_insufficient_balance_does_not_scale_down_reference_order(setup):
    bot, client, _ = setup
    client.available_usdt.return_value = Decimal('5')
    assert 'trading halted' in bot.reconcile_once(NOW)
    assert 'fund' in bot.state.halted_reason
    client.market_order.assert_not_called()


def test_legacy_unresolved_entry_blocks_migration(setup):
    bot, client, _ = setup
    bot.state.pending_protection_side = 'Sell'
    assert 'trading halted' in bot.reconcile_once(NOW)
    client.market_order.assert_not_called()


def test_new_reference_run_replaces_existing_trade(setup):
    bot, client, target = setup
    bot.reconcile_once(NOW)
    fill(bot, client)
    target(reference_run='2026-09-13T14:00:00+00:00')
    assert 'submitted reduce' in bot.reconcile_once(NOW)


def test_new_start_flat_snapshot_waits_without_date_range_error(setup):
    bot, client, target = setup
    target(side=None, reference_run=NOW.isoformat(), last_processed_candle=None)
    assert bot.reconcile_once(NOW) is None
    client.market_order.assert_not_called()


def test_failed_cancellation_does_not_prevent_emergency_close(setup):
    bot, client, target = setup
    bot.reconcile_once(NOW)
    client.position.return_value = {'side': 'Sell', 'size': '1', 'avgPrice': '1.34'}
    client.order_by_link.side_effect = BybitDemoError('history unavailable')
    target(published_at='2026-09-13T14:10:00+00:00')
    assert 'emergency close submitted' in bot.reconcile_once(NOW)
    assert client.market_order.call_args.kwargs['reduce_only']


def test_default_demo_command_uses_replica_executor(monkeypatch, tmp_path):
    import bybit_demo_bot.replica as replica
    config_path = tmp_path / 'config.json'
    config_path.write_text(config(ticker='XRP_USDT').to_json())
    env = tmp_path / 'demo.env'
    env.write_text('BYBIT_API_KEY=test\nBYBIT_API_SECRET=test\n')
    monkeypatch.setattr(cli, 'STATE_FILE', tmp_path / 'demo.json')
    monkeypatch.setattr(cli, 'LOCK_FILE', tmp_path / 'demo.lock')
    factory = Mock()
    monkeypatch.setattr(replica, 'ReferenceReplicaBot', factory)
    cli.run_demo_command(config_path, env, resume=True)
    factory.assert_called_once()
    factory.return_value.run.assert_called_once_with(2)


def test_client_order_link_lookup_and_cancel():
    from bybit_demo_bot.client import BybitDemoClient
    client = BybitDemoClient('key', 'secret')
    client._request = Mock(side_effect=[{'orderId': 'one'}, {'list': []},
                                       {'list': [{'orderStatus': 'Filled'}]}, {}])
    assert client.market_order('XRPUSDT', 'Sell', Decimal('7.3'), order_link_id='ref-test') == 'one'
    assert client._request.call_args.args[2]['orderLinkId'] == 'ref-test'
    assert client.order_by_link('XRPUSDT', 'ref-test')['orderStatus'] == 'Filled'
    assert client._request.call_args.args[1] == '/v5/order/history'
    client.cancel_by_link('XRPUSDT', 'ref-test')
    assert client._request.call_args.args[2]['orderLinkId'] == 'ref-test'


def test_protection_clear_failure_halts_and_closes(setup):
    bot, client, _ = setup
    client.position.return_value = {'side': 'Sell', 'size': '7.3', 'avgPrice': '1.34'}
    client.set_protection.side_effect = BybitDemoError('cannot remove protection')
    assert 'emergency close submitted' in bot.reconcile_once(NOW)
    assert bot.state.halted_reason


def test_rejected_close_cannot_open_opposite_position(setup):
    bot, client, target = setup
    bot.reconcile_once(NOW)
    fill(bot, client)
    target(side='Long')
    bot.reconcile_once(NOW)
    client.order_by_link.return_value = {'orderStatus': 'Rejected'}
    assert 'trading halted' in bot.reconcile_once(NOW)
    assert all(call.kwargs.get('reduce_only') for call in client.market_order.call_args_list[1:])


def test_startup_allows_reference_to_publish_but_never_enters_without_target(setup):
    bot, client, target = setup
    bot.target_path.unlink()
    assert 'waiting for initial' in bot.reconcile_once(NOW)
    assert not bot.state.halted_reason
    client.market_order.assert_not_called()
    target()
    assert 'submitted entry' in bot.reconcile_once(NOW)


def test_startup_missing_target_eventually_halts(setup):
    bot, client, _ = setup
    bot.target_path.unlink()
    bot.reconcile_once(NOW)
    assert 'trading halted' in bot.reconcile_once(NOW + timedelta(seconds=121))
    client.market_order.assert_not_called()


def test_restart_accepts_already_filled_order_even_after_timeout(setup):
    bot, client, target = setup
    bot.reconcile_once(NOW)
    later = NOW + timedelta(minutes=2)
    target(now=later)
    client.order_by_link.return_value = {'orderStatus': 'Filled'}
    client.position.return_value = {'side': 'Sell', 'size': '7.3', 'avgPrice': '1.34'}
    resumed = ReferenceReplicaBot(bot.config, client, cli.DemoState.load_or_create(True), target_path=bot.target_path)
    assert 'fill confirmed' in resumed.reconcile_once(later)
    assert not resumed.state.halted_reason
    client.market_order.assert_called_once()
