import math
import pandas as pd
import pytest

from backtest import Policy, fee, price_for_fill, run, load_inputs

BAR={'open':10.,'high':10.3,'low':9.9,'close':10.1,'volume':100000,'suspendFlag':0}

def test_gap_up_does_not_fill_stale_buy_at_open():
    p=Policy('old',0,1,True)
    assert price_for_fill(BAR,9.8,1,p,5,'open') is None
    assert price_for_fill(BAR,9.8,1,p,5,'touch') is None
    assert price_for_fill(BAR,10.,1,p,5,'open') is None
    assert price_for_fill(BAR,10.,1,p,5,'touch')==10.

def test_protected_limit_has_hard_total_price_cap_both_sides():
    p=Policy('cap50',50,1)
    assert price_for_fill(BAR,10.,1,p,5,'open')==pytest.approx(10.005)
    assert price_for_fill(BAR,9.9,1,p,5,'open') is None
    assert price_for_fill(BAR,10.1,-1,p,5,'open') is None
    assert price_for_fill(BAR,10.,-1,p,5,'open')==pytest.approx(9.995)

def test_open_fill_never_uses_later_low_to_choose_price():
    p=Policy('cap50',50,1)
    assert price_for_fill(BAR,10.,1,p,5,'open')==price_for_fill(dict(BAR,low=1.,high=20.),10.,1,p,5,'open')

def test_locked_and_suspended_are_not_assumed_filled():
    p=Policy('arrival',math.inf,1)
    assert price_for_fill(dict(BAR,high=10.,low=10.),10.,1,p,5,'open') is None
    assert price_for_fill(dict(BAR,suspendFlag=1),10.,1,p,5,'open') is None

def test_dividend_receivable_cash_payment_split_and_no_signal_day_fill():
    dates=pd.to_datetime(['2024-12-30','2024-12-31','2025-01-02','2025-01-03','2025-01-06'])
    close=pd.DataFrame({'X':[10.,10.,9.,9.,4.5]},index=dates)
    bars={(d,'X'):{'open':p,'high':p+.1,'low':p-.1,'close':p,'volume':100000,'suspendFlag':0} for d,p in zip(dates,close.X)}
    w=pd.DataFrame({'X':[1.]},index=dates[:1])
    actions=[{'symbol':'X','record_date':dates[1],'ex_date':dates[2],'pay_date':dates[3],'cash':1.,'factor':1.},
             {'symbol':'X','record_date':None,'ex_date':dates[4],'pay_date':None,'cash':0.,'factor':2.}]
    result,h,c=run(w,bars,close,actions,Policy('arrival',math.inf,1),10000.,0)
    assert h.iloc[0].cash==10000 and h.iloc[0].marked_holdings==0
    assert h.iloc[1].nav==9995
    assert h.iloc[2].receivable==900 and h.iloc[2].cash==995
    assert h.iloc[3].receivable==0 and h.iloc[3].cash==1895
    assert h.iloc[4].marked_holdings==8100
    assert (h.iloc[1:].nav==9995).all()
    assert result['modeled_notional_completion']==1.

def test_cash_never_borrowed_to_finish_a_gap_up_buy():
    dates=pd.to_datetime(['2024-12-30','2024-12-31','2025-01-02'])
    close=pd.DataFrame({'X':[10.,20.,20.]},index=dates)
    bars={(d,'X'):{'open':p,'high':p+.1,'low':p-.1,'close':p,'volume':100000,'suspendFlag':0} for d,p in zip(dates,close.X)}
    w=pd.DataFrame({'X':[1.]},index=dates[:1])
    result,h,c=run(w,bars,close,[],Policy('arrival',math.inf,1),10000.,0)
    assert h.cash.min()>=0
    assert result['modeled_notional_completion']==pytest.approx(400/900)
    assert result['cash_clipped_fills']==1

def test_all_public_raw_prices_reconcile_to_hfq_with_events():
    w,b,c,a,v=load_inputs()
    assert len(v)==9 and len(w)==82
    assert max(x['max_abs_raw_action_vs_hfq'] for x in v.values())<.002
    assert max(w.index)<max(c.index)

def test_fee_minimum():
    assert fee(0)==0 and fee(1000)==5 and fee(100000)==10
