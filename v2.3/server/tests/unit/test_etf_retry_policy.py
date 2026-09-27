import pandas as pd
import pytest
from sqlalchemy import select

from app.exceptions import APIError
from app.models import HydraExecutionAttempt, HydraRebalance, InstanceState, Order
from app.schemas.hydra_relay import HydraAttemptCloseRequest
from app.services.etf_retry_policy import guarded_orders, window_outcome
from app.services.orders_queue import OrdersQueueService
from tests.unit.test_hydra_adjacent_execution import live, publication, advance


@pytest.mark.parametrize('day,outcome',[
    ('20260924','BEFORE_WINDOW'),('20260925','ELIGIBLE'),('20260928','ELIGIBLE'),
    ('20260929','ELIGIBLE'),('20260930','WINDOW_EXPIRED'),('20261008','WINDOW_EXPIRED'),
    ('20260926','CALENDAR_INCOMPLETE'),
])
def test_window_counts_sessions_including_missed_days_not_retry_count(day,outcome):
    calendar=pd.DataFrame({'trade_date':['20260924','20260925','20260928','20260929','20260930','20261008']})
    assert window_outcome(calendar,'20260925',day)==outcome


def test_missing_first_day_cannot_restart_the_window():
    assert window_outcome(pd.DataFrame({'trade_date':['20260928','20260929']}),'20260925','20260929')=='CALENDAR_INCOMPLETE'


def test_price_anchor_does_not_compound_and_cash_keeps_lots():
    orders=[dict(symbol='X',direction='BUY',quantity=1000,reference_price=10.2,limit_price=10.251),
            dict(symbol='Y',direction='SELL',quantity=100,reference_price=4.9,limit_price=4.875)]
    result=guarded_orders(orders,{'X':10.,'Y':5.},1600,100)
    buy,sell=result
    assert buy['limit_price']==10.05 and buy['reference_price']==10.
    assert sell['limit_price']==4.975 and sell['reference_price']==5.
    assert buy['quantity']==200 and buy['quantity']%100==0
    assert buy['quantity']*buy['limit_price']+5 <=1600+100*4.975-5
    assert orders[0]['quantity']==1000 # input is immutable


def test_no_cash_does_not_emit_a_zero_lot():
    assert guarded_orders([dict(symbol='X',direction='BUY',quantity=100,reference_price=10.,limit_price=10.05)],{'X':10.},100,100)==[]


def start_and_close(live, *, status='CANCELLED'):
    service,sf,req=live
    service.stage_initial(req)
    publication(service,'20260803')
    first=advance(service,'20260803')['results'][0]
    close(service,sf,first,status=status)
    return first


def close(service,sf,attempt, *, status='CANCELLED'):
    with sf() as session:
        for order in session.scalars(select(Order).where(Order.attempt_id==attempt['attempt_id'])):
            order.status=status
        session.commit()
    service.close_attempt(HydraAttemptCloseRequest(execution_domain='live',account_alias='hydra-live',
        attempt_id=attempt['attempt_id'],actual_cash=1000000,actual_positions={},reconciliation_evidence_sha256='c'*64))


def test_overnight_rise_keeps_original_limits_and_target_quantities(live):
    service,sf,req=live
    first=start_and_close(live)
    publication(service,'20260804',multiplier=1.04)
    result=advance(service,'20260804')
    assert len(result['results'])==1
    second=result['results'][0]
    orders=OrdersQueueService(sf).list_pending('20260805','live',('hydra-live',))
    assert {o.limit_price for o in orders}=={2.01,4.02}
    assert {o.execution_reference_price for o in orders}=={2.,4.}
    assert advance(service,'20260804')['results'][0]['attempt_id']==second['attempt_id']
    with sf() as session:
        assert len(session.scalars(select(HydraExecutionAttempt)).all())==2
        assert session.get(HydraRebalance,first['rebalance_id']).target_shares=={o.symbol:o.quantity for o in orders}


def test_unconfirmed_policy_expiry_never_licenses_a_duplicate_order(live):
    service,sf,req=live
    start_and_close(live,status='EXPIRED_BY_POLICY')
    publication(service,'20260804')
    result=advance(service,'20260804')
    assert result['results']==[]
    assert result['deferred'][0]['retry_outcome']=='BROKER_NOT_FINAL'
    with sf() as session:assert len(session.scalars(select(HydraExecutionAttempt)).all())==1


def test_window_expires_even_when_intermediate_sessions_were_missed(live):
    service,sf,req=live
    start_and_close(live)
    publication(service,'20260810')
    result=advance(service,'20260810')
    assert result['results']==[] and result['deferred'][0]['retry_outcome']=='WINDOW_EXPIRED'
    with sf() as session:assert len(session.scalars(select(HydraExecutionAttempt)).all())==1


def test_legacy_option_remains_available_for_explicit_rollback(live):
    service,sf,req=live
    service.etf_execution_policy='legacy'
    start_and_close(live)
    publication(service,'20260804',multiplier=.95)
    assert len(advance(service,'20260804')['results'])==1
    orders=OrdersQueueService(sf).list_pending('20260805','live',('hydra-live',))
    assert {o.execution_reference_price for o in orders}=={1.9,3.8}
    assert all('retry_guard' not in o.execution_policy for o in orders)


def test_waiting_cash_does_not_create_empty_attempt_or_change_target(live):
    service,sf,req=live
    first=start_and_close(live)
    with sf() as session:
        session.get(InstanceState,'live_hydra').virtual_cash=1.
        before=dict(session.get(HydraRebalance,first['rebalance_id']).target_shares)
        session.commit()
    publication(service,'20260804')
    result=advance(service,'20260804',actual_cash=1.)
    assert result['results']==[] and result['deferred'][0]['retry_outcome']=='WAITING_CASH'
    with sf() as session:
        assert len(session.scalars(select(HydraExecutionAttempt)).all())==1
        assert session.get(HydraRebalance,first['rebalance_id']).target_shares==before


def test_existing_windows_client_accepts_frozen_anchor_retry(live,tmp_path):
    from live_client.core import validate_order_batch
    from live_client.tests.test_live_client import _cfg
    service,sf,req=live
    start_and_close(live)
    publication(service,'20260804',multiplier=1.04)
    advance(service,'20260804')
    orders=[o.model_dump() for o in OrdersQueueService(sf).list_pending('20260805','live',('hydra-live',))]
    batch=validate_order_batch(orders,'20260805',_cfg(tmp_path))
    assert batch.attempt_number==2
    assert all(o['execution_policy']['retry_guard']['max_trading_sessions']==3 for o in batch.orders)


def test_two_retries_then_fourth_trading_day_is_stopped(live,monkeypatch):
    from tests.unit import test_hydra_adjacent_execution as fixture_module
    monkeypatch.setattr(fixture_module,'DATES',['20260731','20260803','20260804','20260805','20260806','20260807'])
    service,sf,req=live
    start_and_close(live)
    for reference in ['20260804','20260805']:
        publication(service,reference)
        result=advance(service,reference)
        assert len(result['results'])==1
        close(service,sf,result['results'][0])
    publication(service,'20260806')
    last=advance(service,'20260806')
    assert last['results']==[] and last['deferred'][0]['retry_outcome']=='WINDOW_EXPIRED'
    with sf() as session:
        assert len(session.scalars(select(HydraExecutionAttempt)).all())==3


def test_partial_fill_only_remaining_quantity_is_reissued(live):
    service,sf,req=live
    service.stage_initial(req)
    publication(service,'20260803')
    first=advance(service,'20260803')['results'][0]
    with sf() as session:
        for o in session.scalars(select(Order)):
            o.status='CANCELLED'
        state=session.get(InstanceState,'live_hydra')
        state.virtual_positions={'159915.SZ':100}
        state.virtual_cash=999799.
        targets=dict(session.get(HydraRebalance,first['rebalance_id']).target_shares)
        session.commit()
    service.close_attempt(HydraAttemptCloseRequest(execution_domain='live',account_alias='hydra-live',
        attempt_id=first['attempt_id'],actual_cash=999799.,actual_positions={'159915.SZ':100},reconciliation_evidence_sha256='c'*64))
    publication(service,'20260804')
    advance(service,'20260804',actual_cash=999799.,actual_positions={'159915.SZ':100})
    orders={o.symbol:o for o in OrdersQueueService(sf).list_pending('20260805','live',('hydra-live',))}
    assert orders['159915.SZ'].quantity==targets['159915.SZ']-100
    assert orders['510300.SH'].quantity==targets['510300.SH']
