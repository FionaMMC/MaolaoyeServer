import copy
import pandas as pd
import pytest

from compare_policies import Replay, Policy, limit_reference


GOLD = '518880.SH'
DOMESTIC = '510300.SH'


def engine(prices=None, adjacent=False):
    dates = pd.bdate_range('2024-12-23', periods=10)
    close = pd.DataFrame({GOLD:prices or [10.]*10,DOMESTIC:[10.]*10},index=dates)
    bars = {(d,s):dict(open=close.loc[d,s],high=close.loc[d,s]+.1,low=close.loc[d,s]-.1,
                      close=close.loc[d,s],volume=100000,suspendFlag=0) for d in dates for s in close}
    weights = pd.DataFrame({GOLD:[.5],DOMESTIC:[.5]},index=dates[:1])
    return Replay(weights,bars,close,[],10000.,0,adjacent)


def pending(e,state,date):
    p = e.make_pending(state,date,date+pd.Timedelta(days=1),e.weights.iloc[0])
    return p


def test_rolling_uses_previous_close_only_and_does_not_change_sells_or_other_etfs():
    e = engine([10.,10.2,10.2,10.2,10.2,10.2,10.2,10.2,10.2,10.2])
    state = e.initial_state()
    p = pending(e,state,e.dates[0])
    previous = e.close.iloc[1]
    rolling = Policy('rolling',rolling_focus_buy=True)
    assert limit_reference(p,GOLD,1,rolling,previous,[]) == (10.2,50)
    assert limit_reference(p,GOLD,-1,rolling,previous,[]) == (10.,50)
    assert limit_reference(p,DOMESTIC,1,rolling,previous,[]) == (10.,50)
    fixed_state, fixed_p = copy.deepcopy(state),copy.deepcopy(p)
    rolling_state, rolling_p = copy.deepcopy(state),copy.deepcopy(p)
    e.execute(fixed_state,e.dates[2],fixed_p,Policy('fixed'))
    e.execute(rolling_state,e.dates[2],rolling_p,rolling)
    assert fixed_state['qty'][GOLD] == 0
    assert rolling_state['qty'][GOLD] > 0


def test_skipped_monday_consumes_window_and_cannot_extend_to_day_four():
    e = engine(adjacent=True)
    thursday,friday,monday,tuesday,wednesday = [e.dates[i] for i in [3,4,5,6,7]]
    state = e.initial_state()
    p = pending(e,state,thursday)
    for date in [friday,monday,tuesday,wednesday]:
        # Prevent fills so residual remains throughout.
        for s in e.symbols:
            e.bars[date,s].update(open=11.,low=10.9,high=11.1)
        e.execute(state,date,p,Policy('fixed'))
    assert p['age'] == 4
    assert p['attempts'] == 2  # Friday and Tuesday only, not Monday/Wednesday.
    assert all(q == 0 for q in state['qty'].values())


def test_partial_fills_respect_per_day_volume_and_never_double_count():
    e = engine()
    state = e.initial_state()
    p = pending(e,state,e.dates[0])
    p['target'] = {GOLD:300,DOMESTIC:0}
    p['detail'] = {GOLD:dict(p['detail'][GOLD],intended=3000.)}
    e.bars[e.dates[1],GOLD]['volume'] = 200
    e.execute(state,e.dates[1],p,Policy('fixed'))
    assert state['qty'][GOLD] == 200
    assert p['detail'][GOLD]['filled'] == 2000
    e.execute(state,e.dates[2],p,Policy('fixed'))
    assert state['qty'][GOLD] == 300
    assert p['detail'][GOLD]['filled'] == 3000
    assert p['detail'][GOLD]['first_day_filled'] == 2000


def test_later_touch_sale_cannot_finance_an_earlier_open_buy():
    e = engine()
    e.weights.iloc[0] = [1.,0.]
    state = e.initial_state()
    state.update(cash=0.,qty={GOLD:0,DOMESTIC:200})
    p = pending(e,state,e.dates[0])
    p['target'] = {GOLD:100,DOMESTIC:0}
    p['detail'][GOLD]['intended'] = 1000.
    p['detail'][DOMESTIC]['intended'] = 2000.
    date = e.dates[1]
    e.bars[date,DOMESTIC].update(open=9.8,low=9.7,high=10.1)
    e.execute(state,date,p,Policy('fixed'))
    assert state['qty'][GOLD] == 100
    # Gold could only buy at intraday limit 10.05 after sell cash, not open 10.
    assert p['detail'][GOLD]['price_cost'] == pytest.approx(5.)
    assert state['cash'] >= 0


def test_dividend_and_split_accounting_preserve_economic_value():
    e = engine()
    state = e.initial_state()
    state['qty'][GOLD] = 100
    ex_date,pay_date = e.dates[2],e.dates[3]
    state['rights'][(GOLD,ex_date)] = 100
    e.ex[ex_date] = [dict(symbol=GOLD,cash=1.,factor=1.,ex_date=ex_date,pay_date=pay_date)]
    e.apply_actions(state,ex_date)
    assert state['cash'] == 10000 and state['receivables'][0][1] == 100
    e.apply_actions(state,pay_date)
    assert state['cash'] == 10100 and state['receivables'][0][1] == 0
    p = pending(e,state,pay_date)
    split_date = e.dates[4]
    e.ex[split_date] = [dict(symbol=GOLD,cash=0.,factor=2.,ex_date=split_date,pay_date=None)]
    previous_qty,previous_ref,previous_target = state['qty'][GOLD],p['original_ref'][GOLD],p['target'][GOLD]
    e.apply_actions(state,split_date,p)
    assert state['qty'][GOLD] == previous_qty*2
    assert p['original_ref'][GOLD] == previous_ref/2
    assert p['target'][GOLD] == previous_target*2


def test_opportunity_benchmark_cannot_spend_unpaid_dividends():
    e = engine()
    e.weights.iloc[0] = [1.,0.]
    state = e.initial_state()
    state.update(cash=0.,qty={GOLD:0,DOMESTIC:100},
                 receivables=[[e.dates[-1]+pd.Timedelta(days=100),5000.]])
    p = pending(e,state,e.dates[0])
    _, summary = e.episode((state,p),Policy('fixed'))
    assert summary['benchmark_buy_scaling'] == pytest.approx(.2)
    assert summary['terminal_shortfall'] == pytest.approx(5.)
