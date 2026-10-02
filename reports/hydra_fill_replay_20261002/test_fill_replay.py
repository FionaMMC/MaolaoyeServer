from copy import deepcopy

import pandas as pd
import pytest

from allocation_code import replan_residual
from fill_replay import episode, session


def order(symbol, direction, quantity, price):
    return dict(symbol=symbol, direction=direction, quantity=quantity,
                reference_price=price, limit_price=price)


def bar(price=10, volume=1000000):
    return dict(open=price, low=price-.1, high=price+.1, close=price, volume=volume)


def test_filled_a1000_is_not_sold_to_follow_a900_and_b600_capped_to_old500():
    orders,audit = replan_residual(target_shares={'A':1000,'B':500},
        actual_positions={'A':1000}, actual_cash=5500, weights={'A':.6,'B':.4},
        prices={'A':10,'B':10}, anchors={'A':10,'B':10},
        cash_buffer_weight=.01, lot_size=100, priorities={}, blocked_symbols=set())
    assert int(15500*.99*.6/10/1.006005/100)*100 == 900
    assert audit['fresh_target_shares']['B'] == 600
    assert [(o['symbol'],o['direction'],o['quantity']) for o in orders] == [('B','BUY',500)]
    assert audit['buy_reserved_cash'] <= 5500


def test_sell_cash_cannot_be_spent_in_same_bar():
    state={'cash':0.,'positions':{'OLD':100}}
    orders=[order('OLD','SELL',100,10), order('NEW','BUY',100,9)]
    result=session(state,orders,{0:{'OLD':bar(10.1),'NEW':bar(8.9)}})
    assert [f['symbol'] for f in result['fills']] == ['OLD']
    assert state['positions'].get('NEW',0)==0


def test_report_lag_one_vs_two_bars_changes_eligible_buy_time():
    bars={0:{'OLD':bar(10.1),'NEW':bar(8.9)}, 1:{'NEW':bar(8.9)}, 2:{'NEW':bar(8.9)}}
    orders=[order('OLD','SELL',100,10), order('NEW','BUY',100,9)]
    for lag in (1,2):
        state={'cash':0.,'positions':{'OLD':100}}
        result=session(state,orders,bars,lag_bars=lag)
        buy=next(f for f in result['fills'] if f['direction']=='BUY')
        assert buy['time']==str(lag)
        assert result['minimum_cash']>=0


def test_whole_order_must_be_affordable_before_submit_not_just_partial_fill():
    state={'cash':1000.,'positions':{}}
    result=session(state,[order('A','BUY',200,9)],{0:{'A':bar(8.9)}})
    assert not result['fills']
    assert result['rows'][0]['submit_status']=='PREPARED'


def test_partial_fill_volume_and_no_duplicate_fill_over_original_order():
    state={'cash':4000.,'positions':{}}
    result=session(state,[order('A','BUY',300,10)],{i:{'A':bar(9.9,10000)} for i in range(5)})
    assert [f['quantity'] for f in result['fills']]==[100,100,100]
    assert result['rows'][0]['fee']==pytest.approx(5)
    assert state['positions']['A']==300


def simple_case():
    dates=pd.date_range('2026-09-01',periods=4)
    close=pd.DataFrame({'A':[10.]*4},index=dates)
    snapshot=dict(cash=3000.,positions={},target={'A':200},anchors={'A':10.},weights={'A':1.})
    daily={(date,'A'):bar(10) for date in dates}
    minutes={date:{0:{'A':bar(11 if i==1 else 9.9)}} for i,date in enumerate(dates[1:],1)}
    return snapshot,dates[1:],close,daily,minutes


def test_final_broker_state_required_before_cross_day_resubmit():
    args=simple_case()
    result=episode(*args,arm='replan_3d',broker_final=False)
    assert result['details'][0]['filled_quantity']==0
    assert result['days'][1]['reason']=='PREVIOUS_BROKER_NOT_FINAL'
    assert result['days'][2]['reason']=='PREVIOUS_BROKER_NOT_FINAL'


def test_retry_completion_denominator_remains_original_quantity():
    args=simple_case()
    result=episode(*args,arm='replan_3d')
    assert result['details'][0]['requested_quantity']==200
    assert result['details'][0]['day1_quantity']==0
    assert result['details'][0]['filled_quantity']==200
    assert len(result['fills'])==1


def test_snapshot_not_mutated_and_same_day_one_day_comparison():
    args=simple_case()
    original=deepcopy(args[0])
    result=episode(*args,arm='one_day')
    assert result['details'][0]['filled_quantity']==0
    assert args[0]==original


def test_cash_limited_retry_shrinks_without_improving_denominator():
    snapshot,dates,close,daily,minutes=simple_case()
    snapshot['cash']=1100.
    result=episode(snapshot,dates,close,daily,minutes,arm='replan_3d')
    assert result['details'][0]['requested_quantity']==200
    assert result['details'][0]['filled_quantity']==100
    assert result['details'][0]['remaining_quantity']==100


def test_weekend_retry_is_not_silently_executed_on_non_adjacent_pair():
    dates=pd.to_datetime(['20260903','20260904','20260907','20260908'])
    close=pd.DataFrame({'A':[10.]*4},index=dates)
    snapshot=dict(cash=3000.,positions={},target={'A':200},anchors={'A':10.},weights={'A':1.})
    daily={(d,'A'):bar(10) for d in dates}
    minutes={d:{0:{'A':bar(11 if i==1 else 9.9)}} for i,d in enumerate(dates[1:],1)}
    result=episode(snapshot,dates[1:],close,daily,minutes,arm='replan_3d')
    assert result['days'][1]['reason']=='NON_ADJACENT_TRADING_DAY'
    assert result['fills'][0]['day']==3
