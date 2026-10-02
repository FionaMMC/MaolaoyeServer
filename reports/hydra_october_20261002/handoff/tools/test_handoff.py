from types import SimpleNamespace
import pytest
from collect_qmt_readonly import collect
from check_readiness import evaluate
from lot_feasibility import best_lot_fit


class ReadOnlyFake:
    def query_stock_asset(self,a): return SimpleNamespace(account_id='TEST',cash=1000,total_asset=3000)
    def query_stock_orders(self,a): return []
    def query_stock_trades(self,a): return []
    def query_stock_positions(self,a): return [SimpleNamespace(account_id='TEST',stock_code='TEST.SH',volume=200,can_use_volume=100,market_value=2000)]
    def __getattr__(self,name): raise AssertionError('Unexpected API: '+name)


def test_collector_only_queries_and_never_asserts_historical_finality():
    r=collect(ReadOnlyFake(),object(),'TEST')
    assert r['mutations']==0
    assert r['positions'][0]['can_use_volume']==100
    assert r['historical_completeness'].startswith('UNPROVEN')


def test_empty_response_not_confused_with_no_orders():
    f=ReadOnlyFake();f.query_stock_orders=lambda _:None
    with pytest.raises(ValueError,match='unavailable'): collect(f,object(),'TEST')


def test_account_mismatch_stops_capture():
    with pytest.raises(ValueError,match='identity mismatch'): collect(ReadOnlyFake(),object(),'DIFFERENT')


def test_readiness_missing_evidence_never_passes():
    r=evaluate({})
    assert r['status']=='REVIEW_REQUIRED'
    assert r['does_not_authorize_or_submit_orders'] is True
    assert 'two_phase_native_acceptance' in r['blocking_items']


def test_rounding_can_make_two_percent_impossible_even_without_cost():
    result=best_lot_fit(200000,{'BOND':.3,'OTHER':.7},{'BOND':135.,'OTHER':1.})
    assert result['minimum_allocation_error']>.02
    assert result['largest_lot_weight']==pytest.approx(.0675)


def test_fractional_weight_target_exact_lots_zero_error():
    assert best_lot_fit(200000,{'A':.6,'B':.4},{'A':10,'B':10})['minimum_allocation_error']==pytest.approx(0)


def test_reserve_is_accounted_in_integer_diagnostic():
    result=best_lot_fit(200000,{'A':1.},{'A':10.},cash_buffer=.05)
    assert result['cash_weight']==pytest.approx(.05)
    assert result['minimum_allocation_error']==pytest.approx(0)
