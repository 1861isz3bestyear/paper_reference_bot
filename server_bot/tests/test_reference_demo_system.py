"""Reference HTTP ingestion -> SQLite/ledger -> target file -> real demo HTTP client."""
from datetime import timedelta
from decimal import Decimal
import json

import pytest

pytestmark=pytest.mark.integration


def test_reference_entry_close_and_restart_are_idempotent(system):
    reference=system.producer()
    reference.run_forever()
    target=json.loads(system.target.read_text())
    assert target['side']=='Long'
    assert Decimal(target['quantity'])==10
    assert target['last_processed_candle']=='2026-01-01T00:01:00+00:00'
    demo=system.consumer()
    assert 'submitted entry Buy' in demo.reconcile_once(system.clock.value)
    assert 'fill confirmed' in demo.reconcile_once(system.clock.value)
    assert system.exchange.position['size']==target['quantity']
    position_id=target['position_id']
    # Recreate both services from their actual disk state, then repeat the candle.
    system.producer().run_forever()
    restarted=system.consumer()
    assert restarted.reconcile_once(system.clock.value) is None
    assert json.loads(system.target.read_text())['position_id']==position_id
    assert len(system.exchange.submissions)==1
    system.clock.value+=timedelta(minutes=1)
    system.exchange.price='102'
    system.producer().run_forever()
    assert json.loads(system.target.read_text())['side'] is None
    assert 'submitted reduce Sell' in restarted.reconcile_once(system.clock.value)
    assert 'fill confirmed' in restarted.reconcile_once(system.clock.value)
    system.producer().run_forever()
    assert system.consumer().reconcile_once(system.clock.value) is None
    assert system.exchange.position is None
    assert len(system.exchange.submissions)==2
    assert system.exchange.submissions[1]['reduceOnly'] is True
    ledger=json.loads(system.account.read_text())
    assert len(ledger['history'])==1
    assert Decimal(ledger['history'][0]['pnl'])==Decimal('17.9800')
    assert any(path=='/v5/account/fee-rate' and signed for _,path,_,signed in system.exchange.requests)


def test_accepted_order_with_lost_response_survives_restart_without_duplicate(system):
    system.producer().run_forever()
    system.exchange.lose_next_response=True
    demo=system.consumer()
    assert 'intent retained' in demo.reconcile_once(system.clock.value)
    saved=json.loads(system.demo_path.read_text())
    link=saved['replica_order']['link']
    assert system.exchange.orders[link]['orderStatus']=='Filled'
    restarted=system.consumer()
    assert 'fill confirmed' in restarted.reconcile_once(system.clock.value)
    assert restarted.reconcile_once(system.clock.value) is None
    assert len(system.exchange.submissions)==1
    assert json.loads(system.demo_path.read_text())['replica_order'] is None


def test_delayed_fill_after_restart_is_observed_before_new_orders(system):
    system.producer().run_forever()
    system.exchange.defer_fills=True
    demo=system.consumer()
    assert 'submitted entry' in demo.reconcile_once(system.clock.value)
    restarted=system.consumer()
    assert 'awaiting order/fill' in restarted.reconcile_once(system.clock.value)
    link=json.loads(system.demo_path.read_text())['replica_order']['link']
    assert len(system.exchange.submissions)==1
    system.exchange.fill(link)
    assert 'fill confirmed' in restarted.reconcile_once(system.clock.value)
    assert restarted.reconcile_once(system.clock.value) is None
    assert len(system.exchange.submissions)==1


def test_history_failure_stale_halt_and_checked_recovery_preserve_accounts(system):
    from bybit_demo_bot.recover import recover
    system.producer().run_forever()
    demo=system.consumer()
    demo.reconcile_once(system.clock.value)
    demo.reconcile_once(system.clock.value)
    last_good=system.target.read_bytes()
    system.clock.value+=timedelta(minutes=3)
    # Missing completed candles prevent publication, even though the process runs.
    system.producer().run_forever()
    assert system.target.read_bytes()==last_good
    assert 'emergency close submitted' in demo.reconcile_once(system.clock.value)
    assert system.exchange.position is None
    restarted=system.consumer()
    assert 'current reference unavailable' in restarted.reconcile_once(system.clock.value)
    before=system.demo_path.read_bytes()
    with pytest.raises(RuntimeError,match='reference target unavailable'):
        recover(restarted)
    assert system.demo_path.read_bytes()==before
    start=int(system.exchange.candles[0][0])
    system.exchange.candles.extend([[str(start+i*60000),'102','102','102','102','10','1020'] for i in (3,4)])
    system.exchange.price='102'
    system.producer().run_forever()
    assert system.target.read_bytes()!=last_good
    assert 'current reference healthy' in restarted.reconcile_once(system.clock.value)
    assert restarted.state.halted_reason is not None
    orders=len(system.exchange.submissions)
    account_before=system.account.read_bytes()
    assert 'Halt cleared after checks' in recover(restarted)
    assert len(system.exchange.submissions)==orders
    assert system.account.read_bytes()==account_before
    assert list(system.demo_path.parent.glob('*.recovery-*.bak'))
    assert system.consumer().reconcile_once(system.clock.value) is None
