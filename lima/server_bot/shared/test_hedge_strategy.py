from types import SimpleNamespace
import pandas as pd
import pytest
from shared.hedge_strategy import HedgeEngine, HEDGE_MODE, stop_price
from shared.strategy import backtest


def row(i,opening=100,high=100.2,low=99.8,close=100):
    return SimpleNamespace(time=pd.Timestamp('2026-01-01',tz='UTC')+pd.Timedelta(minutes=i),open=opening,high=high,low=low,close=close)


def engine(**kwargs):
    return HedgeEngine(1000,kwargs.pop('fee_pct',0),kwargs.pop('target_sigma',1.2),kwargs.pop('stop_loss_pct',5),**kwargs)


def test_touch_arms_two_limits_and_never_fills_retroactively():
    e=engine()
    e.process(row(0,high=103,low=97),100,1)
    assert set(e.pending)=={'Long','Short'}
    assert not e.positions and not e.history
    assert e.plan['legs']['Long']['entry_price']==e.plan['legs']['Short']['entry_price']==100
    assert e.plan['legs']['Long']['take_profit']==101.2
    assert e.plan['legs']['Short']['take_profit']==98.8
    e.process(row(1),100,1)
    assert set(e.positions)=={'Long','Short'}
    assert e.equity(100)==1000


def test_both_legs_exit_independently_and_cash_is_not_double_counted():
    e=engine()
    e.process(row(0),100,1);e.process(row(1),100,1)
    e.process(row(2,high=101.3,low=100,close=101),100,1)
    assert set(e.positions)=={'Short'}
    assert e.history[0]['side']=='Long'
    e.process(row(3,high=100,low=98.7,close=99),100,1)
    assert not e.positions and not e.pending
    assert e.cash==pytest.approx(1012)
    assert sum(t.pnl for t in e.trades())==pytest.approx(12)


@pytest.mark.parametrize('basis,risk',[('Position',5),('Deposit',10)])
def test_sl_budget_includes_both_fees(basis,risk):
    e=engine(fee_pct=.1,stop_loss_pct=1,stop_loss_basis=basis)
    e.process(row(0),100,1);e.process(row(1,high=100,low=100),100,1)
    e.process(row(2,high=103,low=97),100,1)
    assert len(e.history)==2
    assert all(t['exit_reason']=='Stop loss' for t in e.history)
    assert [t['pnl'] for t in e.history]==pytest.approx([-risk,-risk])
    assert e.cash==pytest.approx(1000-2*risk)


def test_snapshot_restart_does_not_duplicate_fills_or_trades():
    e=engine();e.process(row(0),100,1);e.process(row(1),100,1)
    restored=HedgeEngine(**e.snapshot())
    assert not restored.process(row(1),100,1)
    assert restored.snapshot()==e.snapshot()
    for i in [2,3]:
        candle=row(i,high=103,low=97)
        e.process(candle,100,1);restored.process(candle,100,1)
    assert restored.snapshot()==e.snapshot()


def test_pending_leg_expires_and_does_not_rearm_without_fresh_touch():
    e=engine(entry_timeout_bars=1)
    e.process(row(0),100,1)
    e.process(row(1,opening=101,high=101,low=101,close=101),100,1)
    assert not e.pending
    assert set(e.positions)=={'Short'}
    e.close_all(101,row(1).time,'test')
    e.process(row(2),100,1)
    assert set(e.pending)=={'Long','Short'}


def test_entry_bar_losses_are_charged_without_same_bar_tp_credit():
    e=engine(stop_loss_pct=1)
    e.process(row(0),100,1)
    e.process(row(1,high=103,low=97),100,1)
    assert len(e.history)==2
    assert all(t['exit_reason']=='Stop loss' for t in e.history)


def test_zero_variance_and_insufficient_capital_do_not_arm():
    e=engine(minimum_order_size=501)
    e.process(row(0),100,1)
    assert not e.pending
    e=engine();e.process(row(0),100,0)
    assert not e.pending


@pytest.mark.parametrize('basis',['Position','Deposit'])
def test_backtest_dispatch_opens_both_sides_and_liquidates_each_at_end(basis):
    data=pd.DataFrame([vars(row(i)) for i in range(4)])
    data['anchored_vwap']=100.;data['anchored_std']=1.
    eq,trades=backtest(data,data.time.iloc[0],data.time.iloc[-1],1000,.1,5,0,True,HEDGE_MODE,0,
        exit_band_sigma=1.2,stop_loss_basis=basis)
    assert {t.side for t in trades}=={'Long','Short'}
    assert all(t.entry_time==data.time.iloc[2] for t in trades)
    assert all(t.exit_reason=='End of strategy' for t in trades)
    assert eq.equity.iloc[-1]==pytest.approx(1000+sum(t.pnl for t in trades))
    assert eq.equity.iloc[-1]<1000


def test_stop_budget_validation():
    with pytest.raises(ValueError,match='fees'):stop_price(100,5,500,1000,.1,'Position',.1)
    with pytest.raises(ValueError,match='smaller'):stop_price(100,5,500,1000,100,'Deposit')
    with pytest.raises(ValueError,match='basis'):stop_price(100,5,500,1000,1,'wrong')


def test_streaming_hedge_ledger_and_backtest_use_same_closed_trade_accounting():
    from shared.hedge_strategy import process_hedge_history
    from shared.indicators import add_launch_weekly_anchored_vwap
    cfg=SimpleNamespace(initial_capital=1000,close_order_vwap_sigma=1.2,stop_loss_pct=1,
        stop_loss_basis='Position',hedge_entry_timeout_bars=3,minimum_order_size=.01,
        anchor_before_strategy_start=False,anchor_before_days=0,vwap_anchor_reset_weeks=2)
    state=SimpleNamespace(hedge_state=None,last_processed_candle=None)
    raw=pd.DataFrame([vars(row(0,high=100,low=100)),vars(row(1,opening=101,high=102,low=100,close=102)),
                      vars(row(2,opening=101,high=101,low=100)),vars(row(3,opening=100.5,high=101,low=100)),
                      vars(row(4,opening=100,high=104,low=96))])
    raw['volume']=10
    for length in range(1,len(raw)+1):
        process_hedge_history(state,raw.iloc[:length],cfg,raw.time.iloc[0],.1)
    streaming=HedgeEngine(**state.hedge_state)
    streaming.close_all(raw.close.iloc[-1],raw.time.iloc[-1],'End of strategy')
    data=add_launch_weekly_anchored_vwap(raw,raw.time.iloc[0],2)
    eq,trades=backtest(data,raw.time.iloc[0],raw.time.iloc[-1],1000,.1,1,0,True,HEDGE_MODE,0,
                       exit_band_sigma=1.2,minimum_order_size=.01)
    assert trades==streaming.trades()
    assert eq.equity.iloc[-1]==pytest.approx(streaming.cash)
    before=state.hedge_state
    assert process_hedge_history(state,raw,cfg,raw.time.iloc[0],.1)==0
    assert state.hedge_state==before


def test_hedge_gap_stop_uses_actual_open_and_disabled_stop_remains_disabled():
    e=engine(stop_loss_pct=1)
    e.process(row(0),100,1);e.process(row(1),100,1)
    e.process(row(2,opening=90,high=90,low=90,close=90),100,1)
    long=next(t for t in e.trades() if t.side=='Long')
    assert long.exit_price==90
    assert long.pnl==pytest.approx(-50)
    assert stop_price(100,5,500,1000,0,'Position')=={'Long':None,'Short':None}
