"""Stateful local Bybit protocol simulator; never uses real credentials/accounts."""
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit

import pandas as pd
import pytest


@pytest.fixture
def exchange_http():
    start=int(pd.Timestamp('2026-01-01',tz='UTC').timestamp()*1000)
    state=SimpleNamespace(price='100',hedge_mode=False,hedge_positions={},position=None,orders={},submissions=[],requests=[],
        lose_next_response=False,defer_fills=False,fail_kline=False,
        candles=[[str(start+i*60000),str(o),str(h),str(l),str(c),'10',str(c*10)]
                 for i,(o,h,l,c) in enumerate([(100,100,100,100),(100,101,100,101),(102,102,102,102)])])
    def fill(link):
        order=state.orders[link]
        order['orderStatus']='Filled'
        if order.get('positionIdx'):
            idx=int(order['positionIdx'])
            if order.get('reduceOnly'):
                state.hedge_positions.pop(idx,None)
            else:
                state.hedge_positions[idx]={'symbol':order['symbol'],'side':order['side'],'size':order['qty'],'avgPrice':order.get('price',state.price),'positionIdx':idx,'stopLoss':order.get('stopLoss','0'),'takeProfit':order.get('takeProfit','0')}
        elif order['reduceOnly']:
            state.position=None
        else:
            state.position={'symbol':order['symbol'],'side':order['side'],'size':order['qty'],
                            'avgPrice':state.price,'stopLoss':'0','takeProfit':'0'}
    state.fill=fill

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass
        def respond(self,result=None,code=0,message='OK',status=200):
            data=json.dumps({'retCode':code,'retMsg':message,'result':result or {}}).encode()
            self.send_response(status);self.send_header('Content-Length',str(len(data)));self.end_headers()
            self.wfile.write(data)
        def handle_request(self):
            url=urlsplit(self.path)
            body=self.rfile.read(int(self.headers.get('Content-Length','0'))).decode()
            values=json.loads(body) if body else {k:v[0] for k,v in parse_qs(url.query).items()}
            private=url.path.startswith(('/v5/account/','/v5/position/','/v5/order/'))
            if private:
                timestamp=self.headers.get('X-BAPI-TIMESTAMP','')
                recv=self.headers.get('X-BAPI-RECV-WINDOW','')
                signed=f'{timestamp}test-key{recv}{body or url.query}'
                expected=hmac.new(b'test-secret',signed.encode(),hashlib.sha256).hexdigest()
                if self.headers.get('X-BAPI-API-KEY')!='test-key' or self.headers.get('X-BAPI-SIGN')!=expected:
                    self.respond(code=10004,message='bad test signature');return
            state.requests.append((self.command,url.path,values,private))
            if url.path=='/v5/market/kline':
                if state.fail_kline:self.respond(status=503);return
                rows=[r for r in state.candles if int(values['start'])<=int(r[0])<=int(values['end'])]
                self.respond({'list':sorted(rows,key=lambda r:int(r[0]),reverse=True)});return
            if url.path=='/v5/market/mark-price-kline':
                rows=[r[:5] for r in state.candles if int(values['start'])<=int(r[0])<=int(values['end'])]
                result={'list':sorted(rows,key=lambda r:int(r[0]),reverse=True)}
            elif url.path=='/v5/market/funding/history':
                times=[start-480*60000,start]
                result={'list':[dict(fundingRateTimestamp=str(t),fundingRate='.0001') for t in times if int(values['startTime'])<=t<=int(values['endTime'])]}
            elif url.path=='/v5/market/risk-limit':
                result={'list':[dict(riskLimitValue='1000000',maintenanceMargin='.005',mmDeduction='',maxLeverage='100')]}
            elif url.path=='/v5/market/tickers':result={'list':[{'lastPrice':state.price}]}
            elif url.path=='/v5/market/instruments-info':
                result={'list':[{'contractType':'LinearPerpetual','settleCoin':'USDT','fundingInterval':480,'leverageFilter':{'maxLeverage':'100','leverageStep':'1'},'priceFilter':{'tickSize':'0.01'},'lotSizeFilter':{'qtyStep':'0.001','minOrderQty':'0.001','minNotionalValue':'1','maxMktOrderQty':'10000'}}]}
            elif url.path=='/v5/account/fee-rate':result={'list':[{'takerFeeRate':'0.001'}]}
            elif url.path=='/v5/account/wallet-balance':result={'list':[{'coin':[{'coin':'USDT','walletBalance':'10000'}]}]}
            elif url.path=='/v5/position/list':
                result={'list':list(state.hedge_positions.values()) if state.hedge_mode else ([state.position] if state.position else [])}
            elif url.path=='/v5/position/trading-stop':result={}
            elif url.path=='/v5/order/create':
                link=values.get('orderLinkId',f'emergency-{len(state.submissions)}')
                if link in state.orders:self.respond(code=10014,message='duplicate order');return
                state.submissions.append(values)
                state.orders[link]=dict(values,orderId=link,orderStatus='New')
                if not state.defer_fills:fill(link)
                if state.lose_next_response:
                    state.lose_next_response=False
                    self.respond(status=503);return  # Accepted/fill persisted, response unavailable.
                result={'orderId':link}
            elif url.path in ('/v5/order/realtime','/v5/order/history'):
                link=values.get('orderLinkId')
                rows=[state.orders[link]] if link in state.orders else []
                if not link:rows=[o for o in state.orders.values() if o['orderStatus']=='New']
                result={'list':rows}
            elif url.path=='/v5/order/cancel':
                state.orders[values['orderLinkId']]['orderStatus']='Cancelled';result={}
            else:self.respond(code=10001,message='unhandled test endpoint');return
            self.respond(result)
        do_GET=handle_request
        do_POST=handle_request
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    state.url=f'http://127.0.0.1:{server.server_port}'
    try:yield state
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


@pytest.fixture
def system(tmp_path,monkeypatch,exchange_http):
    from reference_bot import cli as reference,market,trading
    from bybit_demo_bot import cli as demo,replica
    from bybit_demo_bot.client import BybitDemoClient
    from shared import reference_target
    from reference_bot.config import PaperBotConfig
    class Clock(datetime):
        value=datetime(2026,1,1,0,2,5,tzinfo=timezone.utc)
        @classmethod
        def now(cls,tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)
    for module in (reference,trading,replica,reference_target):
        monkeypatch.setattr(module,'datetime',Clock)
    monkeypatch.setattr(market,'BYBIT_KLINES_URL',exchange_http.url+'/v5/market/kline')
    monkeypatch.setattr(market,'BYBIT_TICKERS_URL',exchange_http.url+'/v5/market/tickers')
    monkeypatch.setattr(trading,'BYBIT_FEE_RATE_URL',exchange_http.url+'/v5/account/fee-rate')
    monkeypatch.setattr(reference,'load_bybit_credentials',lambda:('test-key','test-secret'))
    account=tmp_path/'reference_account.json'
    monkeypatch.setattr(reference,'LocalPaperTrader',lambda:trading.LocalPaperTrader(account))
    monkeypatch.setattr(demo,'STATE_FILE',tmp_path/'demo_state.json')
    cfg=PaperBotConfig(strategy_mode='VWAP band mean reversion',timeframe='1m',trend=True,
        open_order_vwap_sigma=1,close_order_vwap_sigma=2,initial_capital=1000,stop_loss_pct=0,
        allow_immediate_reentry=True,vwap_anchor_reset_weeks=2,data_source='Bybit REST',ticker='BTC_USDT',
        open_position_side='Long',minimum_order_size=.01)
    config_path=tmp_path/'config.json';cfg.save(config_path)
    state_path=tmp_path/'reference_state.json'
    reference.BotState('2026-01-01T00:00:00+00:00').save(state_path)
    def producer():
        bot=reference.PaperBot(config_path,state_path,tmp_path/'candles.sqlite')
        # The external websocket is disabled; production REST ingestion is used.
        bot.market_feed=market.RestMarketFeed()
        bot._wait=lambda _:bot.stop()  # Execute exactly one production loop iteration.
        return bot
    client=BybitDemoClient('test-key','test-secret',base_url=exchange_http.url,timeout=2)
    target=tmp_path/'reference_target.json'
    def consumer():
        return replica.ReferenceReplicaBot(cfg,client,demo.DemoState.load_or_create(True),target_path=target)
    return SimpleNamespace(producer=producer,consumer=consumer,clock=Clock,target=target,account=account,
                           state_path=state_path,client=client,exchange=exchange_http,demo_path=demo.STATE_FILE)
