"""Six confirmed sells can fund six of nine buys without an all-sells barrier."""
from copy import deepcopy

import pytest

from live_client import cli
from live_client.core import validate_order_batch
from live_client.execution_queue import submission_order
from live_client.gateway import AccountSnapshot
from test_execution_queue import QueueGateway, _freeze
from test_live_client import _cfg, _orders


def policy(priorities=None):
    return dict(policy_id="HYDRA_ADJACENT_DAY_50BP_V1", reference_date="20260802",
                buy_max_bps=50, sell_max_bps=50,
                cash_allocation={"policy_id": "PROPORTIONAL_RESIDUAL_V1",
                                 "queue_order": "PRIORITY_THEN_REMAINING_NOTIONAL",
                                 "budget_source": "SETTLED_STRATEGY_CASH",
                                 "fee_reserve_bps": 10, "min_commission": 5,
                                 "buy_priorities": priorities or {}})


def setup_queue(tmp_path, monkeypatch, *, buys=9, sells=9, cash=0, priorities=None, buy_quantities=None):
    orders = []
    for side, count, prefix, price in [('SELL', sells, '51', 10.1), ('BUY', buys, '15', 10.)]:
        for index in range(count):
            orders.append({**_orders()[0], 'order_id': f'{side}_{index}',
                           'symbol': f'{prefix}{index:04d}.SH', 'direction': side,
                           'quantity': buy_quantities[index] if side == 'BUY' and buy_quantities else 100, 'limit_price': price,
                           'execution_reference_price': price, 'execution_policy': policy(priorities)})
    cfg = _cfg(tmp_path, ledger_mode='attributed', initial_allocated_cash=cash,
               allowed_symbols=frozenset(o['symbol'] for o in orders), max_daily_orders=30)
    batch, state = _freeze(cfg, rotate=False, cash=cash, orders=orders)
    positions = {o['symbol']: o['quantity'] for o in orders if o['direction']=='SELL'}

    class Gateway(QueueGateway):
        filled_orders = 6

        def account_snapshot(self):
            return AccountSnapshot(cfg.account_id, self.cash, 100000, positions, positions)

        def confirmed_sell_fills(self, submissions):
            return {s['order_id']: {'filled_quantity': 100 if int(s['order_id'].split('_')[1]) < self.filled_orders else 0,
                                    'filled_price': 10.1 if int(s['order_id'].split('_')[1]) < self.filled_orders else 0}
                    for s in submissions}

    gateway = Gateway(cfg)
    gateway.cash = 6030
    monkeypatch.setattr(cli, '_gateway', lambda *_: gateway)

    def forbidden(*args, **kwargs):
        pytest.fail('cash queue must stay offline from server')

    monkeypatch.setattr(cli, 'LiveServerClient', forbidden)
    return cfg, batch, state, gateway


def test_six_of_nine_fills_submit_six_buys_and_resume_only_remaining_three(tmp_path, monkeypatch):
    cfg, batch, state, gateway = setup_queue(tmp_path, monkeypatch)
    result = cli.submit(cfg, '20260803', None)
    assert gateway.calls == ['SELL'] * 9 + ['BUY'] * 6
    assert len(result['deferred_cash']) == 3
    assert result['status'] == 'WAITING_FOR_CASH'
    assert [o['order_id'] for o in batch.orders if state.submission(o['order_id'])['submit_status']=='DEFERRED_CASH'] == ['BUY_6', 'BUY_7', 'BUY_8']
    assert cli.submit(cfg, '20260803', None)['attempted_now'] == 0
    gateway.filled_orders = 9
    gateway.cash = 3015  # remaining balance, not cumulative proceeds
    assert cli.submit(cfg, '20260803', None)['submitted_now'] == 3
    assert cli.submit(cfg, '20260803', None)['attempted_now'] == 0
    assert gateway.calls.count('BUY') == 9


def test_one_slow_sell_does_not_block_eight_funded_buys(tmp_path, monkeypatch):
    cfg, _, _, gateway = setup_queue(tmp_path, monkeypatch)
    gateway.filled_orders = 8
    gateway.cash = 8040
    result = cli.submit(cfg, '20260803', None)
    assert gateway.calls.count('BUY') == 8
    assert len(result['deferred_cash']) == 1


def test_explicit_priority_is_frozen_hashed_and_controls_actual_submission(tmp_path, monkeypatch):
    cfg, batch, state, gateway = setup_queue(tmp_path, monkeypatch, buys=2, sells=0, cash=1005,
                                            priorities={'150001.SH': 0})
    gateway.cash = 1005
    result = cli.submit(cfg, '20260803', None)
    assert result['submitted_now'] == 1
    assert state.submission('BUY_1')['submit_status'] == 'SUBMITTED'
    assert state.submission('BUY_0')['submit_status'] == 'DEFERRED_CASH'
    changed = deepcopy(list(batch.orders))
    for order in changed:
        order['execution_policy']['cash_allocation']['buy_priorities'] = {'150000.SH': 0}
    with pytest.raises(ValueError, match='hash|sha256'):
        validate_order_batch(changed, '20260803', cfg)


def test_default_order_largest_remaining_amount_then_code_and_no_mutation():
    orders = [{**o, 'execution_policy': policy()} for o in _orders()]
    before = deepcopy(orders)
    assert [o['symbol'] for o in submission_order(orders)] == ['510300.SH', '159915.SZ']
    assert orders == before


def test_unknown_allocation_policy_fails_closed(tmp_path, monkeypatch):
    cfg, batch, _, _ = setup_queue(tmp_path, monkeypatch)
    changed = deepcopy(list(batch.orders))
    for order in changed:
        order['execution_policy']['cash_allocation']['policy_id'] = 'UNKNOWN'
    with pytest.raises(ValueError, match='cash_allocation'):
        validate_order_batch(changed, '20260803', cfg)


def test_unaffordable_priority_order_does_not_block_smaller_order(tmp_path, monkeypatch):
    cfg, _, state, gateway = setup_queue(tmp_path, monkeypatch, buys=2, sells=0, cash=3000,
                                         priorities={'150000.SH': 0}, buy_quantities=[200,100])
    gateway.cash = 1005
    result = cli.submit(cfg, '20260803', None)
    assert result['submitted_now'] == 1
    assert state.submission('BUY_0')['submit_status'] == 'DEFERRED_CASH'
    assert state.submission('BUY_1')['submit_status'] == 'SUBMITTED'
    assert gateway.calls == ['BUY']
