from copy import deepcopy
import pandas as pd
import pytest
from continuous_replay import next_pair,run


def case():
    dates=pd.bdate_range('2026-08-24',periods=18)
    close=pd.DataFrame({'A':10.,'B':10.},index=dates)
    daily={(d,s):dict(open=10.,close=10.,high=10.1,low=9.9,volume=1e7,suspendFlag=0) for d in dates for s in close.columns}
    weights=pd.DataFrame([[1.,0.],[0.,1.]],index=[dates[0],dates[7]],columns=close.columns)
    return weights,daily,close,[]


def test_october_pair_strictly_after_known_signal_no_weekend_pair():
    dates=pd.to_datetime(['20260930','20261008','20261009','20261012','20261013'])
    assert next_pair(dates,dates[0])==(dates[1],dates[2])
    assert next_pair(dates,dates[2])==(dates[3],dates[4])


def test_constant_prices_frictionless_flat_and_costs_explain_nav_loss():
    args=case()
    for arm in ('signal_ideal','pair_ideal'):
        s,h,*_=run(*args,arm=arm)
        assert s['total_return']==pytest.approx(0)
    s,h,cy,fills,_=run(*args,arm='fixed',slip_bps=0)
    assert h.nav.iloc[-1]==pytest.approx(200000-fills.fee.sum())
    assert s['minimum_cash']>=0
    assert cy.buy_filled.sum()<=cy.buy_intended.sum()+1e-8


def test_sell_strictly_after_signal_buy_next_day_and_not_same_phase():
    weights,*rest=case()
    _,_,_,fills,_=run(weights,*rest,arm='fixed',slip_bps=0)
    sell=fills[fills.direction=='SELL'].iloc[0]
    buy=fills[(fills.direction=='BUY') & (fills.symbol=='B')].iloc[0]
    assert pd.Timestamp(sell.date)>weights.index[1]
    assert pd.Timestamp(buy.date)==pd.Timestamp(sell.date)+pd.Timedelta(days=1)
    assert sell.phase=='close' and buy.phase=='open'


def test_broker_unknown_blocks_opening_buy_without_cancelling_reality():
    s,_,_,fills,_=run(*case(),arm='fixed',terminal_delay=3)
    assert fills.empty
    assert s['buy_completion']==0


def test_bad_open_above_fixed_envelope_stays_unfilled():
    w,d,c,a=case()
    for date in c.index[1:]:
        d[date,'A']['open']=11.
        d[date,'A']['high']=11.1
    s,_,_,fills,_=run(w,d,c,a,arm='fixed')
    assert not (fills.symbol=='A').any()
    assert s['max_end_underweight']>.02


def test_no_reset_carries_price_return_and_fees_into_next_target():
    w,d,c,a=case()
    for date in c.index[5:]:
        c.loc[date,'A']=12
        d[date,'A'].update(open=12.,close=12.,high=12.1,low=11.9)
    s,h,cy,f,_=run(w,d,c,a,arm='fixed',slip_bps=0)
    assert h.nav.iloc[-1]>230000
    assert cy.iloc[1].buy_intended>230000
    assert s['minimum_cash']>=0


def test_inputs_not_mutated():
    w,d,c,a=case()
    saved=deepcopy(d)
    run(w,d,c,a,arm='replan')
    assert d==saved


def test_dividend_receivable_not_early_cash_and_no_false_nav_drop():
    w,d,c,a=case()
    w=w.iloc[:1]
    ex,record,pay=c.index[6],c.index[5],c.index[10]
    a=[dict(symbol='A',ex_date=ex,record_date=record,pay_date=pay,cash=1.,factor=1.)]
    for date in c.index[6:]:
        c.loc[date,'A']=9.
        d[date,'A'].update(open=9.,close=9.,high=9.1,low=8.9)
    s,h,*_=run(w,d,c,a,arm='fixed',slip_bps=0)
    assert h.loc[ex,'receivables']>0
    assert h.loc[ex,'cash']==h.loc[record,'cash']
    assert h.loc[ex,'nav']==pytest.approx(h.loc[record,'nav'])
    assert h.loc[pay,'receivables']==0
    assert h.loc[pay,'nav']==pytest.approx(h.loc[ex,'nav'])


def test_reserve_in_total_capital_denominator_and_no_free_external_cash():
    w,d,c,a=case()
    w=w.iloc[:1]
    for date in c.index[5:]:
        c.loc[date,'A']=11
        d[date,'A'].update(open=11,close=11,high=11.1,low=10.9)
    s,h,*_=run(w,d,c,a,arm='signal_ideal',cash_buffer=.05)
    assert s['total_return']==pytest.approx(.095)
    assert h.cash.iloc[-1]==pytest.approx(10000)
    assert h.nav.iloc[-1]==pytest.approx(219000)
