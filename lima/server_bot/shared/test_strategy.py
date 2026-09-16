"""Execution regressions: executable prices, causal bands, fees, and risk."""
import numpy as np
import pandas as pd
import pytest
from shared.strategy import backtest


def candles(rows, vwaps=None):
    df = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'])
    df['time'] = pd.date_range('2026-09-14', periods=len(df), freq='min', tz='UTC')
    df['anchored_vwap'] = vwaps if vwaps is not None else 100.
    df['anchored_std'] = 1.
    return df


def run(df, **kwargs):
    args = dict(strategy_start_at=df.time.iloc[0], strategy_end_at=df.time.iloc[-1],
                initial_capital=1000, fee_pct=0, stop_loss_pct=0, take_profit_pct=0,
                allow_reentry=True, strategy_mode='VWAP band mean reversion',
                band_sigma=1, exit_band_sigma=2, trend_mode=True)
    args.update(kwargs)
    return backtest(df, **args)


@pytest.mark.parametrize('side,price', [('Long', 110), ('Short', 90)])
def test_candles_wholly_beyond_entry_band_never_create_fictional_fills(side, price):
    df = candles([(price, price + .1, price - .1, price)] * 20)
    eq, trades = run(df, open_position_side=side)
    assert not trades
    assert (eq.equity == 1000).all()


def test_entry_uses_previous_indicator_and_has_no_first_bar_lookahead():
    df = candles([(100, 101, 99, 100), (101, 101.1, 100.9, 101), (102, 102.1, 101.9, 102)], [100, 500, 1])
    _, trades = run(df, open_position_side='Long')
    assert len(trades) == 1
    assert trades[0].entry_time == df.time.iloc[1]
    assert trades[0].entry_price == 101
    assert trades[0].exit_price == 102


@pytest.mark.parametrize('side,entry,exit_', [('Long', 101, 101), ('Short', 99, 99)])
def test_both_sides_pay_entry_and_exit_fees_and_mark_equity(side, entry, exit_):
    df = candles([(100, 100, 100, 100), (entry, entry, entry, entry), (exit_, exit_, exit_, exit_)])
    eq, trades = run(df, open_position_side=side, fee_pct=1)
    t = trades[0]
    assert t.quantity == pytest.approx(990 / entry)
    assert t.pnl == pytest.approx(-19.9)
    assert eq.equity.iloc[1] == pytest.approx(990)
    assert eq.equity.iloc[-1] == pytest.approx(980.1)


@pytest.mark.parametrize('side,entry,opening', [('Long', 101, 90), ('Short', 99, 110)])
def test_gap_through_stop_executes_at_open_not_unavailable_stop(side, entry, opening):
    df = candles([(100, 100, 100, 100), (entry, entry, entry, entry), (opening, opening, opening, opening)])
    _, trades = run(df, open_position_side=side, stop_loss_pct=1)
    assert trades[0].exit_price == opening
    assert trades[0].exit_reason == 'Stop loss'


@pytest.mark.parametrize('side,entry,opening', [('Long', 101, 110), ('Short', 99, 90)])
def test_gap_through_profit_executes_at_open(side, entry, opening):
    df = candles([(100, 100, 100, 100), (entry, entry, entry, entry), (opening, opening, opening, opening)])
    _, trades = run(df, open_position_side=side, take_profit_pct=1)
    assert trades[0].exit_price == opening
    assert trades[0].exit_reason == 'Take profit'


@pytest.mark.parametrize('side,row', [('Long', (101, 105, 95, 100)), ('Short', (99, 105, 95, 100))])
def test_entry_candle_adverse_stop_wins_over_unknown_intrabar_profit(side, row):
    df = candles([(100, 100, 100, 100), row, (100, 100, 100, 100)])
    _, trades = run(df, open_position_side=side, stop_loss_pct=1, take_profit_pct=1)
    t = trades[0]
    assert t.exit_time == t.entry_time
    assert t.exit_reason == 'Stop loss'
    assert t.pnl < 0


@pytest.mark.parametrize('trend,side,entry,opening', [(True,'Long',101,103), (True,'Short',99,97), (False,'Long',99,101), (False,'Short',101,99)])
def test_band_exit_gaps_use_real_open(trend, side, entry, opening):
    df = candles([(100,100,100,100),(entry,entry,entry,entry),(opening,opening,opening,opening)])
    _, trades = run(df, trend_mode=trend, open_position_side=side, exit_band_sigma=2 if trend else 0)
    assert trades[0].exit_price == opening
    assert 'exit band' in trades[0].exit_reason


@pytest.mark.parametrize('field,value', [('initial_capital',0),('initial_capital',np.nan),('fee_pct',100),('stop_loss_pct',-1),('minimum_order_size',np.inf),('open_position_side','bad')])
def test_invalid_parameters_rejected(field,value):
    with pytest.raises(ValueError):
        run(candles([(100,100,100,100)]*3), **{field:value})


@pytest.mark.parametrize('kind', ['nan','range','zero','duplicate','unsorted'])
def test_invalid_candles_rejected(kind):
    df=candles([(100,100,100,100)]*3)
    if kind=='nan': df.loc[1,'close']=np.nan
    if kind=='range': df.loc[1,'close']=110
    if kind=='zero': df.loc[1,'low']=0
    if kind=='duplicate': df.loc[1,'time']=df.time.iloc[0]
    if kind=='unsorted': df=df.iloc[::-1]
    with pytest.raises(ValueError): run(df)


def test_input_not_mutated_and_warmup_can_supply_first_order_band():
    df=candles([(100,100,100,100),(101,101,101,101),(102,102,102,102)])
    original=df.copy(deep=True)
    _, trades=run(df,strategy_start_at=df.time.iloc[1])
    assert trades[0].entry_price==101
    pd.testing.assert_frame_equal(df,original)
