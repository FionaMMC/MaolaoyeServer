"""Portable OMS install gate: synthetic account only, no network or xtquant."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

from live_client.gateway import AccountSnapshot, SubmissionResult
from live_client.offline_acceptance import _config
from live_client.oms_agent import OmsAgent
from live_client.oms_journal import OmsJournal, JournalConflict, plan_content_sha256


def run_acceptance(root: Path) -> dict:
    now = datetime(2099, 1, 5, 9, 15, 5, tzinfo=timezone(timedelta(hours=8)))
    cfg = replace(_config(root, 'shared-oms'), execution_profile='shared_oms')
    class Gateway:
        def __init__(self):
            self.calls = []
        def account_snapshot(self):
            return AccountSnapshot(cfg.account_id, 1500., 1500., {}, {})
        def day_orders(self):
            return []
        def submit_limit(self, **order):
            self.calls.append(order)
            return SubmissionResult(None, 'UNKNOWN', 'synthetic lost response')
    class Server:
        offline = False
        def get_oms_plans(self, *_args):
            if self.offline:
                raise ConnectionError('synthetic offline')
            return {'schema_version': 2, 'plans': plans, 'symbols': ['000001.SZ']}
        def post_oms_events(self, *_args):
            return {}
    plans = []
    for index, instance in enumerate(('acceptance_a', 'acceptance_b'), 1):
        plan = dict(schema_version=2, account_alias=cfg.account_alias, instance_id=instance,
                    account_fingerprint=cfg.expected_account_sha256, cycle_id=f'C{index}', session_id=f'S{index}',
                    trade_date='20990105', phase='BUY', frozen_target={'000001.SZ': 100},
                    orders=[dict(client_order_id=f'H{index:09d}', symbol='000001.SZ', side='BUY', quantity=100, limit_price=10.)],
                    policy={'reference_prices': {'000001.SZ': 10.}, 'default_buy_bps': 50.,
                            'buy_bps': [], 'sell_schedule': [50., 200.]}, instrument_specs={'000001.SZ': dict(
                        asset_type='stock', price_tick=.01, buy_lot=100, min_buy_quantity=100,
                        sell_lot=100, odd_lot_sell_all=True, t_plus_one=True, commission_rate=.0003,
                        min_commission=5., stamp_duty_sell=.0005, transfer_fee_rate=.00001, buy_reserve_rate=None)},
                    allowed_symbols=['000001.SZ'], valid_until=(now+timedelta(minutes=15)).isoformat(),
                    issued_at=now.isoformat(), lease_expires_at=(now+timedelta(minutes=10)).isoformat(),
                    executable=True, strategy_cash=3000., strategy_positions={}, strategy_sellable={})
        plan['plan_sha256'] = plan_content_sha256(plan)
        plans.append(plan)
    gateway, server = Gateway(), Server()
    journal = OmsJournal(root/'oms-agent.db', clock=lambda: now)
    journal.bind(account_alias=cfg.account_alias, execution_domain=cfg.execution_domain,
                 account_fingerprint=cfg.expected_account_sha256, client_id=cfg.task_prefix)
    agent = OmsAgent(account_alias=cfg.account_alias, gateway=gateway, server=server, journal=journal,
                     clock=lambda: now, symbols=[], cfg=cfg)
    agent.execute('20990105', 'BUY')
    assert len(gateway.calls) == 1, 'unknown order must reserve shared physical cash'
    assert journal.intent('H000000001')['state'] == 'UNKNOWN'
    agent.journal = OmsJournal(journal.path, clock=lambda: now)
    server.offline = True
    agent.execute('20990105', 'BUY')
    assert len(gateway.calls) == 1, 'restart must not resubmit or reuse reserved cash'
    agent.clock = lambda: now+timedelta(minutes=11)
    try:
        agent.execute('20990105', 'BUY')
    except JournalConflict:
        pass
    else:
        raise AssertionError('expired offline lease must be refused')
    return dict(passed=True, broker='synthetic', server_contacted=False, qmt_contacted=False,
                checks=['two_strategy_cash_isolation', 'unknown_reservation', 'restart_idempotency', 'offline_lease_expiry'])


def main():
    with tempfile.TemporaryDirectory(prefix='oms-offline-acceptance-') as temporary:
        print(json.dumps(run_acceptance(Path(temporary)), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
