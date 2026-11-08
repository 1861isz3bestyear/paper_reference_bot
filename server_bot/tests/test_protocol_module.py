"""Subsystem tests through real HTTP and filesystem boundaries."""
from decimal import Decimal
import json
import threading
from types import SimpleNamespace

import pytest
from bybit_demo_bot.client import BybitDemoClient,BybitDemoError
from reference_bot.trading import LocalPosition
from shared.reference_target import publish_target

pytestmark=pytest.mark.module


def test_http_client_signs_requests_and_reports_authentication_failure(exchange_http):
    client=BybitDemoClient('test-key','test-secret',base_url=exchange_http.url,timeout=2)
    assert client.available_usdt()==Decimal('10000')
    assert client.position('BTCUSDT') is None
    order=client.market_order('BTCUSDT','Buy',Decimal('2'),order_link_id='module-order')
    assert order=='module-order'
    assert client.position('BTCUSDT')['size']=='2'
    assert client.order_by_link('BTCUSDT',order)['orderStatus']=='Filled'
    assert not client.has_open_orders('BTCUSDT')
    client.market_order('BTCUSDT','Sell',Decimal('2'),reduce_only=True,order_link_id='module-close')
    assert client.position('BTCUSDT') is None
    invalid=BybitDemoClient('test-key','wrong-secret',base_url=exchange_http.url,timeout=2)
    with pytest.raises(BybitDemoError,match='10004'):
        invalid.available_usdt()


def test_atomic_target_publication_never_exposes_partial_json(system):
    reference=system.producer()
    state=SimpleNamespace(launched_at=reference.state.launched_at,last_processed_candle='2026-01-01T00:01:00+00:00')
    flat=LocalPosition('BTCUSDT',None,Decimal('0'),None,None)
    opened=LocalPosition('BTCUSDT','Long',Decimal('10'),100.,'2026-01-01T00:02:05+00:00')
    publish_target(system.target,reference.config,state,position=flat)
    errors=[]
    started=threading.Event()
    def writer():
        try:
            started.set()
            for i in range(100):publish_target(system.target,reference.config,state,position=opened if i%2 else flat)
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=writer);thread.start();assert started.wait(2)
    try:
        for _ in range(200):
            payload=json.loads(system.target.read_text())
            assert (payload['side'],payload['quantity'],payload['entry_price']) in [(None,'0',None),('Long','10',100.)]
    finally:thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert not system.target.with_suffix('.json.tmp').exists()
