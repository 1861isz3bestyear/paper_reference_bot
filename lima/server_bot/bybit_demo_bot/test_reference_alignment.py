from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pandas as pd
import pytest

from bybit_demo_bot import cli
from bybit_demo_bot.test_bybit_demo import config


@pytest.fixture
def bot(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'STATE_FILE', tmp_path / 'state.json')
    client = Mock()
    client.position.return_value = None
    client.last_price.return_value = Decimal('100')
    client.available_usdt.return_value = Decimal('100')
    client.instruments.return_value = {'lotSizeFilter': {'qtyStep': '0.001', 'minOrderQty': '0.001'}}
    candles = pd.DataFrame([{'time': pd.Timestamp('2026-01-01T00:01:00Z')}])
    monkeypatch.setattr(cli, 'fetch_completed_linear_klines', lambda *_: candles)
    monkeypatch.setattr(cli, 'reference_decision', lambda *_: Mock(side='Long'))
    return cli.ReferenceAlignedDemoBot(config(), client, cli.DemoState('2026-01-01T00:00:00+00:00'))


NOW = datetime(2026, 1, 1, 0, 2, 30, tzinfo=timezone.utc)


def test_reenters_consumed_signal_without_vwap_entry_filter(bot):
    bot.state.consumed_signal_side = 'Buy'
    bot._exit_band = Mock(side_effect=AssertionError('must not filter VWAP TP'))
    assert 'opened Buy' in bot.reconcile_once(NOW)
    bot.client.market_order.assert_called_once()
    assert bot.state.pending_take_profit == '0'
    assert 'awaiting entry reconciliation' in bot.reconcile_once(NOW)
    bot.client.market_order.assert_called_once()


@pytest.mark.parametrize('side,stop', [('Buy', '99.600'), ('Sell', '100.400')])
def test_existing_position_removes_tp_and_retains_sl(bot, side, stop):
    bot._protect({'side': side, 'avgPrice': '100', 'size': '1'}, Decimal('110'))
    bot.client.set_protection.assert_called_once_with('BTCUSDT', Decimal(stop), Decimal('0'))


def test_zero_stop_disables_stop(bot):
    bot.config = config(stop_loss_pct=0)
    bot._protect({'side': 'Buy', 'avgPrice': '100'}, Decimal('110'))
    bot.client.set_protection.assert_called_once_with('BTCUSDT', Decimal('0'), Decimal('0'))


def test_pending_legacy_tp_is_removed_after_fill(bot):
    bot.state.pending_protection_side = 'Buy'
    bot.state.pending_take_profit = '90'
    bot.client.position.return_value = {'side': 'Buy', 'avgPrice': '100', 'size': '1'}
    assert 'protected' in bot.reconcile_once(NOW)
    assert bot.state.pending_protection_side is None
    bot.client.market_order.assert_not_called()
    bot.client.set_protection.assert_called_once_with('BTCUSDT', Decimal('99.600'), Decimal('0'))


def test_reversal_waits_for_confirmed_flat(bot):
    bot.client.position.return_value = {'side': 'Sell', 'avgPrice': '100', 'size': '1'}
    assert 'submitted close Sell' in bot.reconcile_once(NOW)
    bot.client.market_order.assert_called_once_with('BTCUSDT', 'Buy', Decimal('1'), reduce_only=True)
    assert bot.state.last_processed_candle is None
    bot.client.position.return_value = None
    assert 'opened Buy' in bot.reconcile_once(NOW)


def test_protection_failure_emergency_closes_and_halts(bot):
    bot.state.pending_protection_side = 'Buy'
    bot.client.position.return_value = {'side': 'Buy', 'avgPrice': '100', 'size': '1'}
    bot.client.set_protection.side_effect = RuntimeError('unavailable')
    with pytest.raises(RuntimeError, match='emergency close submitted'):
        bot.reconcile_once(NOW)
    assert bot.state.halted_reason
    bot.client.market_order.assert_called_once_with('BTCUSDT', 'Sell', Decimal('1'), reduce_only=True)
