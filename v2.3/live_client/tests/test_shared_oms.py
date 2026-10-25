"""Account executor contracts, with synthetic QMT only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from live_client.gateway import AccountSnapshot, SubmissionResult
from live_client.offline_acceptance import _config
from live_client.oms_agent import OmsAgent
from live_client.oms_journal import OmsJournal, JournalConflict, plan_content_sha256

NOW = datetime(2026, 10, 15, 9, 15, 5, tzinfo=timezone(timedelta(hours=8)))
STOCK = '000001.SZ'
SPEC = dict(asset_type='stock', price_tick=.01, buy_lot=100, min_buy_quantity=100,
            sell_lot=100, odd_lot_sell_all=True, t_plus_one=True, min_commission=5,
            commission_rate=.0003, transfer_fee_rate=0, stamp_duty_sell=.0005, buy_reserve_rate=.001)

# Bands as the server freezes them in every v2 grant (Hydra C3: default 50 bp, sells 50 then 200 bp).
POLICY = dict(default_buy_bps=50., buy_bps=[['518880.SH', 100.], ['513100.SH', 150.], ['513500.SH', 150.]],
              sell_schedule=[50., 200.])


def plan(cfg, instance, coid, *, side='BUY', quantity=100, cash=3000, positions=None, sellable=None, target=100, price=10):
    value = dict(schema_version=2, account_alias=cfg.account_alias, account_fingerprint=cfg.expected_account_sha256,
                 instance_id=instance, cycle_id='C'+instance, session_id='S'+instance,
                 trade_date='20261015', phase=side, frozen_target={STOCK: target},
                 orders=[dict(client_order_id=coid, symbol=STOCK, side=side, quantity=quantity, limit_price=price)],
                 policy=dict(POLICY, reference_prices={STOCK: 10}), instrument_specs={STOCK: SPEC}, allowed_symbols=[STOCK],
                 valid_until=(NOW+timedelta(hours=6)).isoformat(), lease_expires_at=(NOW+timedelta(minutes=10)).isoformat(),
                 issued_at=NOW.isoformat(), strategy_cash=cash, strategy_positions=positions or {},
                 strategy_sellable=sellable or {}, executable=True)
    value['plan_sha256'] = plan_content_sha256(value)
    return value


class Gateway:
    def __init__(self, cfg, cash=10000, positions=None, sellable=None):
        self.cfg, self.cash = cfg, cash
        self.positions, self.sellable = positions or {}, sellable or {}
        self.calls, self.orders = [], []
        self.unknown = False

    def account_snapshot(self):
        return AccountSnapshot(self.cfg.account_id, self.cash, self.cash, self.positions, self.sellable)

    def day_orders(self):
        return self.orders

    def submit_limit(self, **kwargs):
        self.calls.append(kwargs)
        return SubmissionResult(None, 'UNKNOWN', 'accepted response lost') if self.unknown else SubmissionResult(str(len(self.calls)), 'SUBMITTED')


class Server:
    def __init__(self, plans):
        self.plans, self.down, self.events = plans, False, []

    def get_oms_plans(self, *args):
        if self.down:
            raise ConnectionError('offline')
        return {'schema_version': 2, 'plans': self.plans, 'symbols': [STOCK]}

    def post_oms_events(self, alias, events):
        self.events.extend(events)


def setup(tmp_path, **kwargs):
    cfg = replace(_config(tmp_path, 'shared'), execution_profile='shared_oms')
    gateway = Gateway(cfg, **kwargs)
    server = Server([])
    journal = OmsJournal(tmp_path/'oms.db', clock=lambda: NOW)
    journal.bind(account_alias=cfg.account_alias, execution_domain=cfg.execution_domain,
                 account_fingerprint=cfg.expected_account_sha256, client_id=cfg.task_prefix)
    agent = OmsAgent(account_alias=cfg.account_alias, gateway=gateway, server=server, journal=journal,
                     clock=lambda: NOW, symbols=[], cfg=cfg)
    return agent, cfg, gateway, server, journal


def test_two_strategies_buy_same_symbol_against_own_target_not_physical_total(tmp_path):
    a, cfg, g, s, j = setup(tmp_path, positions={STOCK: 900})
    s.plans = [plan(cfg, 'a', 'H000010101', positions={STOCK:100}, target=200),
               plan(cfg, 'b', 'H000020101', positions={STOCK:800}, target=900)]
    a.execute('20261015', 'BUY')
    assert [c['quantity'] for c in g.calls] == [100, 100]
    assert {r['instance_id'] for r in j.submissions()} == {'a', 'b'}


def test_two_strategy_sells_cap_owned_t1_shares_and_shared_physical_sellable(tmp_path):
    a, cfg, g, s, j = setup(tmp_path, positions={STOCK:1000}, sellable={STOCK:700})
    now = NOW.replace(hour=14, minute=57)
    a.clock = lambda: now
    s.plans = [plan(cfg, 'a', 'H000010101', side='SELL', quantity=1000, positions={STOCK:100}, sellable={STOCK:100}),
               plan(cfg, 'b', 'H000020101', side='SELL', quantity=900, positions={STOCK:900}, sellable={STOCK:900})]
    for p in s.plans:
        p['lease_expires_at'] = (now+timedelta(minutes=2)).isoformat()
    a.execute('20261015', 'SELL')
    assert [c['quantity'] for c in g.calls] == [100, 600]


def test_unknown_reserves_cash_for_following_strategy_and_after_restart(tmp_path):
    a, cfg, g, s, j = setup(tmp_path, cash=1500)
    s.plans = [plan(cfg, 'a', 'H000010101'), plan(cfg, 'b', 'H000020101')]
    g.unknown = True
    a.execute('20261015', 'BUY')
    assert len(g.calls) == 1
    a.journal = OmsJournal(j.path, clock=lambda: NOW)
    s.down = True
    a.execute('20261015', 'BUY')
    assert len(g.calls) == 1
    assert a.journal.intent('H000010101')['state'] == 'UNKNOWN'


def test_own_cash_cap_cannot_spend_other_strategy_cash(tmp_path):
    a, cfg, g, s, j = setup(tmp_path, cash=10000)
    s.plans = [plan(cfg, 'a', 'H000010101', cash=1000), plan(cfg, 'b', 'H000020101', cash=2000)]
    a.execute('20261015', 'BUY')
    assert [c['remark'] for c in g.calls] == ['H000020101']


def test_expired_cached_grant_refuses_without_broker_submit(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    s.plans = [plan(cfg, 'a', 'H000010101')]
    a.fetch_plans('20261015', 'BUY')
    s.down = True
    a.clock = lambda: NOW+timedelta(minutes=11)
    with pytest.raises(JournalConflict, match='expired'):
        a.execute('20261015', 'BUY')
    assert not g.calls


def test_account_binding_and_tampered_plan_rejected(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    with pytest.raises(JournalConflict, match='another account'):
        j.bind(account_alias='someone-else', execution_domain='live', account_fingerprint='x', client_id='x')
    p = plan(cfg, 'a', 'H000010101')
    p['orders'][0]['quantity'] = 200
    s.plans = [p]
    with pytest.raises(JournalConflict, match='hash'):
        a.execute('20261015', 'BUY')
    assert not g.calls


def test_recaching_mutable_lease_and_capacity_does_not_rewrite_frozen_order(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    p = plan(cfg, 'a', 'H000010101')
    j.cache_plan(p)
    p['strategy_cash'] = 999
    p['lease_expires_at'] = (NOW+timedelta(minutes=12)).isoformat()
    j.cache_plan(p)
    assert j.plan('20261015', 'BUY', 'a')['strategy_cash'] == 999
    assert j.plan('20261015', 'BUY', 'b') is None


def test_hold_one_strategy_does_not_block_the_other(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    s.plans = [plan(cfg, 'a', 'H000010101'), plan(cfg, 'b', 'H000020101')]
    (tmp_path/'HOLD.a').touch()
    a.execute('20261015', 'BUY')
    assert [c['remark'] for c in g.calls] == ['H000020101']


def test_manual_order_honors_environment_kill_switch(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    a.cfg = replace(cfg, trading_enabled=False)
    p = plan(cfg, 'a', 'E000010101')
    p['phase'] = 'MANUAL'; p['plan_sha256'] = plan_content_sha256(p)
    order = dict(p['orders'][0], instruction_id='I1', kind='ORDER', execution_plan=p)
    result, cost = a._manual_order(order, '20261015', g.account_snapshot(), {}, 10000)
    assert result['status'] == 'FAILED' and not g.calls


def test_broker_remark_collision_never_claims_foreign_order(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    s.plans = [plan(cfg, 'a', 'H000010101')]
    g.orders = [SimpleNamespace(order_remark='H000010101', stock_code='000002.SZ', order_type=23,
                               order_volume=100, price=10, account_id=cfg.account_id)]
    with pytest.raises(JournalConflict, match='identity'):
        a.execute('20261015', 'BUY')
    assert not g.calls


def test_stock_tick_and_daily_notional_guard(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    s.plans = [plan(cfg, 'a', 'H000010101', price=10.001)]
    out = a.execute('20261015', 'BUY')
    assert out['plans'][0]['orders'][0]['status'] == 'GUARD_REJECTED'
    assert not g.calls
    a, cfg, g, s, j = setup(tmp_path/'second')
    a.cfg = replace(cfg, max_daily_buy_notional=1500)
    s.plans = [plan(cfg, 'a', 'H000010101'), plan(cfg, 'b', 'H000020101')]
    a.execute('20261015', 'BUY')
    assert len(g.calls) == 1


def test_server_reserved_order_is_not_charged_twice_against_strategy_cash(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    s.plans = [plan(cfg, 'a', 'H000010101', cash=3000, target=300)]
    g.unknown = True
    a.execute('20261015', 'BUY')
    next_plan = plan(cfg, 'a', 'H000010102', cash=1995, target=300)
    next_plan['session_id'] = 'Sa2'
    next_plan['server_reserved_order_ids'] = ['H000010101']
    next_plan['plan_sha256'] = plan_content_sha256(next_plan)
    s.plans = [next_plan]
    a.execute('20261015', 'BUY')
    assert len(g.calls) == 2  # cash 1995 already excludes the first 1005 reservation


def test_immediate_fill_not_yet_projected_cannot_reuse_strategy_cash(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    s.plans = [plan(cfg, 'a', 'H000010101', cash=1500, target=200)]
    a.execute('20261015', 'BUY')
    g.orders = [SimpleNamespace(order_remark='H000010101', stock_code=STOCK, order_type=23,
                               order_volume=100, price=10, account_id=cfg.account_id, order_id=1,
                               traded_volume=100, order_status=56)]
    next_plan = plan(cfg, 'a', 'H000010102', cash=1500, target=200)
    next_plan['session_id'] = 'Sa2'
    next_plan['plan_sha256'] = plan_content_sha256(next_plan)
    s.plans = [next_plan]
    a.execute('20261015', 'BUY')
    assert len(g.calls) == 1
    # Once the grant identifies the first fill as projected, the remaining allocation is usable.
    next_plan['server_accounted_fill_quantities'] = {'H000010101':100}
    next_plan['strategy_positions'] = {STOCK:100}
    a.execute('20261015', 'BUY')
    assert len(g.calls) == 2


def test_previous_day_unknown_requires_projected_terminal_proof(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    s.plans = [plan(cfg, 'a', 'H000010101')]
    g.unknown = True
    a.execute('20261015', 'BUY')
    tomorrow = NOW + timedelta(days=1)
    a.clock = lambda: tomorrow
    p = plan(cfg, 'a', 'H000010102')
    p.update(trade_date='20261016', session_id='Sa2', valid_until=(tomorrow+timedelta(hours=1)).isoformat(),
             lease_expires_at=(tomorrow+timedelta(minutes=10)).isoformat())
    p['plan_sha256'] = plan_content_sha256(p)
    s.plans = [p]
    assert a.execute('20261016', 'BUY')['status'] == 'UNRESOLVED_PRIOR_DAY'
    assert len(g.calls) == 1
    p['server_terminal_orders'] = {'H000010101': dict(state='NOT_SUBMITTED', filled_qty=0, projected_qty=0,
                                                     trade_date='20261015', last_observed_at=NOW.replace(hour=15, minute=30).isoformat())}
    a.execute('20261016', 'BUY')
    assert len(g.calls) == 2
    assert j.intent('H000010101')['state'] == 'UNKNOWN'  # never erase the original claim
    assert j.terminal_proof('H000010101')['state'] == 'NOT_SUBMITTED'


def test_client_fee_reservation_matches_server_stock_rules():
    from app.oms.instruments import InstrumentSpec, buy_cost
    from live_client.oms_shared import order_cost
    for reserve in (None, .001):
        spec = dict(SPEC, transfer_fee_rate=.00001, buy_reserve_rate=reserve)
        for price, quantity in ((10.03,100), (111.11,1000), (1.01,100)):
            assert order_cost(quantity, price, spec) == buy_cost(InstrumentSpec(**spec), price, quantity)


def test_live_profile_requires_current_broker_bands_and_approved_tick(tmp_path):
    a, cfg, g, s, j = setup(tmp_path)
    a.cfg = replace(cfg, mode='live')
    s.plans = [plan(cfg, 'a', 'H000010101')]
    g.execution_rules = lambda symbol: dict(price_tick=.01, up_limit=9.99, down_limit=8., source_time=NOW.isoformat())
    out = a.execute('20261015', 'BUY')
    assert out['plans'][0]['orders'][0]['status'] == 'GUARD_REJECTED' and not g.calls
    g.execution_rules = lambda symbol: dict(price_tick=.001, up_limit=11., down_limit=8., source_time=NOW.isoformat())
    a.execute('20261015', 'BUY')
    assert not g.calls
    g.execution_rules = lambda symbol: dict(price_tick=.01, up_limit=11., down_limit=8., source_time=(NOW-timedelta(days=1)).isoformat())
    a.execute('20261015', 'BUY')
    assert not g.calls
    g.execution_rules = lambda symbol: dict(price_tick=.01, up_limit=11., down_limit=8., source_time=NOW.isoformat())
    a.execute('20261015', 'BUY')
    assert len(g.calls) == 1


def test_guard_allows_each_frozen_policy_band_and_nothing_wider():
    """Bug: a 50 bp client cap rejected Hydra's frozen 100/150 bp buys and its 200 bp second sells."""
    from live_client.oms_shared import require_guard
    agent = SimpleNamespace(cfg=None)

    def check(symbol, side, price, reference=10., policy=POLICY):
        require_guard(agent, dict(symbol=symbol, side=side, limit_price=price), 100,
                      allowed_symbols={symbol}, policy=dict(policy, reference_prices={symbol: reference}))

    check('518880.SH', 'BUY', 8.558, reference=8.474)     # the real 10/9 gold buy: +99 bp
    check('513100.SH', 'BUY', 10.149)                     # +149 bp
    check('510300.SH', 'SELL', 9.801)                     # -199 bp, a second sell attempt
    for symbol, side, price in (('518880.SH', 'BUY', 10.11), ('510300.SH', 'BUY', 10.06), ('510300.SH', 'SELL', 9.79)):
        with pytest.raises(ValueError, match='frozen policy allows'):
            check(symbol, side, price)
    wide = dict(POLICY, default_buy_bps=1000.)
    check('510300.SH', 'BUY', 10.25, policy=wide)         # within the 300 bp client ceiling
    with pytest.raises(ValueError, match='frozen policy allows'):
        check('510300.SH', 'BUY', 10.40, policy=wide)
    with pytest.raises(ValueError, match='no price band'):
        check('510300.SH', 'BUY', 10., policy={})


def test_scheduled_buy_at_its_frozen_100bp_band_reaches_the_broker(tmp_path):
    """The production failure: with the client config loaded, a +100 bp policy buy was GUARD_REJECTED."""
    a, cfg, g, s, j = setup(tmp_path)
    p = plan(cfg, 'a', 'H000010101', price=10.10)
    p['policy'] = dict(POLICY, buy_bps=[[STOCK, 100.]], reference_prices={STOCK: 10})
    p['plan_sha256'] = plan_content_sha256({k: v for k, v in p.items() if k != 'plan_sha256'})
    s.plans = [p]
    out = a.execute('20261015', 'BUY')
    assert out['plans'][0]['orders'][0]['status'] == 'SUBMITTED' and len(g.calls) == 1



def test_star_sale_below_200_shares_is_only_sent_for_the_whole_position(tmp_path):
    a, cfg, g, s, j = setup(tmp_path, positions={STOCK: 300}, sellable={STOCK: 300})
    now = NOW.replace(hour=14, minute=57)
    a.clock = lambda: now
    star = dict(SPEC, buy_lot=1, min_buy_quantity=200, sell_lot=1, min_sell_quantity=200)
    plans = [plan(cfg, 'a', 'H000010101', side='SELL', quantity=100, positions={STOCK: 300}, sellable={STOCK: 300}),
             plan(cfg, 'b', 'H000020101', side='SELL', quantity=150, positions={STOCK: 150}, sellable={STOCK: 150})]
    for p in plans:
        p['instrument_specs'] = {STOCK: star}
        p['lease_expires_at'] = (now+timedelta(minutes=2)).isoformat()
        p['plan_sha256'] = plan_content_sha256({k: v for k, v in p.items() if k != 'plan_sha256'})
    s.plans = plans
    a.execute('20261015', 'SELL')
    assert [c['quantity'] for c in g.calls] == [150]
