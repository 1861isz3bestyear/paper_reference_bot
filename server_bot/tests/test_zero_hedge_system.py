"""Actual reference ingestion/publication and two hedge-indexed limit orders over HTTP."""
from dataclasses import replace
from datetime import timedelta
import json
import pytest

from bybit_demo_bot import hedge
from bybit_demo_bot.cli import DemoState
from reference_bot.config import PaperBotConfig
from shared.hedge_strategy import HEDGE_MODE

pytestmark=pytest.mark.integration


@pytest.fixture
def hedged(system,monkeypatch):
    path=system.state_path.with_name('config.json')
    cfg=replace(PaperBotConfig.load(path),strategy_mode=HEDGE_MODE,open_order_vwap_sigma=0,
                close_order_vwap_sigma=1.2,open_position_side='Both',stop_loss_pct=1,
                stop_loss_basis='Position')
    cfg.save(path)
    system.clock.value+=timedelta(minutes=1)
    monkeypatch.setattr(hedge,'datetime',system.clock)
    start=int(system.exchange.candles[0][0])
    system.exchange.candles=[[str(start+i*60000),str(o),str(h),str(l),str(c),'10',str(c*10)]
        for i,(o,h,l,c) in enumerate([(100,100,100,100),(101,102,100,102),(101,101,100,100)])]
    system.exchange.hedge_mode=True
    system.producer().run_forever()
    system.cfg=cfg
    system.consumer=lambda:hedge.HedgeReplicaBot(cfg,system.client,DemoState.load_or_create(True),target_path=system.target)
    return system


def test_two_same_vwap_limits_both_fill_and_restart_does_not_duplicate(hedged):
    target=json.loads(hedged.target.read_text())
    assert target['version']==2
    plan=target['hedge_plan']
    assert set(plan['legs'])=={'Long','Short'}
    bot=hedged.consumer()
    action=bot.reconcile_once()
    assert 'submitted Long limit' in action
    assert 'submitted Short limit' in action
    assert bot.reconcile_once() is None
    orders=hedged.exchange.submissions
    assert len(orders)==2
    assert [o['orderType'] for o in orders]==['Limit','Limit']
    assert [o['positionIdx'] for o in orders]==[1,2]
    assert orders[0]['price']==orders[1]['price']
    assert float(orders[0]['takeProfit'])>float(orders[0]['price'])>float(orders[1]['takeProfit'])
    assert float(orders[0]['stopLoss'])<float(orders[0]['price'])<float(orders[1]['stopLoss'])
    assert set(hedged.exchange.hedge_positions)=={1,2}
    resumed=hedged.consumer()
    assert resumed.reconcile_once() is None
    assert len(orders)==2
    # One native TP fills; the other leg remains independently protected.
    hedged.exchange.hedge_positions.pop(1)
    resumed.reconcile_once()
    assert set(hedged.exchange.hedge_positions)=={2}
    hedged.exchange.hedge_positions.pop(2)
    resumed.reconcile_once()
    assert resumed.state.hedge_state['complete']
    assert hedged.consumer().reconcile_once() is None
    assert len(orders)==2


def test_lost_first_response_is_reconciled_before_second_submission(hedged):
    hedged.exchange.lose_next_response=True
    assert 'intent retained' in hedged.consumer().reconcile_once()
    assert len(hedged.exchange.submissions)==1
    resumed=hedged.consumer()
    assert 'submitted Short' in resumed.reconcile_once()
    resumed.reconcile_once()
    assert len(hedged.exchange.submissions)==2


def test_stale_reference_closes_both_indices_and_recovery_does_not_replay(hedged):
    from bybit_demo_bot.recover import recover
    bot=hedged.consumer()
    for _ in range(3):bot.reconcile_once()
    assert len(hedged.exchange.hedge_positions)==2
    original=hedged.clock.value
    hedged.clock.value+=timedelta(minutes=3)
    assert 'trading halted' in bot.reconcile_once()
    assert not hedged.exchange.hedge_positions
    closes=hedged.exchange.submissions[2:]
    assert {o['positionIdx'] for o in closes}=={1,2}
    assert all(o['reduceOnly'] for o in closes)
    with pytest.raises(RuntimeError):recover(bot)
    hedged.clock.value=original
    assert 'Hedge halt cleared' in recover(bot)
    assert hedged.consumer().reconcile_once() is None
    assert len(hedged.exchange.submissions)==4


def test_expired_pending_limits_are_cancelled_without_replacement(hedged):
    hedged.exchange.defer_fills=True
    bot=hedged.consumer()
    bot.reconcile_once();bot.reconcile_once()
    assert len(hedged.exchange.submissions)==2
    # Keep publisher/cursor fresh while the cycle's entry window expires.
    hedged.clock.value+=timedelta(minutes=3)
    payload=json.loads(hedged.target.read_text())
    payload['published_at']=hedged.clock.value.isoformat()
    payload['last_processed_candle']=(hedged.clock.value-timedelta(minutes=1,seconds=5)).isoformat()
    hedged.target.write_text(json.dumps(payload))
    bot.reconcile_once();bot.reconcile_once()
    assert all(o['orderStatus']=='Cancelled' for o in hedged.exchange.orders.values())
    assert len(hedged.exchange.submissions)==2


def test_unknown_order_response_never_resubmits_until_exchange_acknowledges(hedged):
    bot=hedged.consumer()
    hedged.exchange.lose_next_response=True
    bot.reconcile_once()
    link=bot.state.hedge_state['orders']['Long']['link']
    accepted=hedged.exchange.orders.pop(link)
    assert 'no resubmission' in hedged.consumer().reconcile_once()
    assert len(hedged.exchange.submissions)==1
    hedged.exchange.orders[link]=accepted
    assert 'submitted Short' in hedged.consumer().reconcile_once()
    assert len(hedged.exchange.submissions)==2


def test_rejected_second_order_flattens_the_first_leg(hedged):
    bot=hedged.consumer()
    hedged.exchange.defer_fills=True
    bot.reconcile_once()
    hedged.exchange.fill(bot.state.hedge_state['orders']['Long']['link'])
    link=bot.state.hedge_state['orders']['Short']['link']
    hedged.exchange.orders[link]['orderStatus']='Rejected'
    hedged.exchange.defer_fills=False
    assert 'trading halted' in bot.reconcile_once()
    assert not hedged.exchange.hedge_positions
    assert hedged.exchange.submissions[-1]['reduceOnly'] is True
    assert hedged.exchange.submissions[-1]['positionIdx']==1


def test_cleanup_attempts_both_position_closes_even_if_cancel_fails(hedged,monkeypatch):
    from bybit_demo_bot.client import BybitDemoError
    bot=hedged.consumer()
    for _ in range(3):bot.reconcile_once()
    link=bot.state.hedge_state['orders']['Long']['link']
    hedged.exchange.orders[link]['orderStatus']='PartiallyFilled'
    def failed_cancel(*args):raise BybitDemoError('cancel unavailable')
    monkeypatch.setattr(hedged.client,'cancel_by_link',failed_cancel)
    hedged.clock.value+=timedelta(minutes=3)
    with pytest.raises(BybitDemoError,match='cleanup incomplete'):
        bot.reconcile_once()
    assert not hedged.exchange.hedge_positions
    assert bot.state.halted_reason


@pytest.mark.parametrize('change',[{'version':1},{'config_fingerprint':'wrong'},{'hedge_plan':{'legs':{}}}])
def test_invalid_hedge_targets_submit_no_entry(hedged,change):
    payload=json.loads(hedged.target.read_text());payload.update(change)
    hedged.target.write_text(json.dumps(payload))
    assert 'trading halted' in hedged.consumer().reconcile_once()
    assert not hedged.exchange.submissions


def test_zero_hedge_requires_exchange_hedge_mode(hedged):
    from bybit_demo_bot.client import BybitDemoError
    hedged.exchange.hedge_mode=False
    hedged.exchange.position={'symbol':'BTCUSDT','positionIdx':0,'side':'','size':'0'}
    with pytest.raises(BybitDemoError,match='hedge position mode'):
        hedged.consumer().reconcile_once()
    assert not hedged.exchange.submissions
