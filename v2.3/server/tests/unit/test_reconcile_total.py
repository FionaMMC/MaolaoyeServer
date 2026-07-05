"""ReconcileService.reconcile_total / shadow_compare 测试（portfolio 级总量对账）。"""
from datetime import datetime
from pathlib import Path

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState


def _factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    return make_session_factory(engine)


def _seed_instance(sf, instance_id, cash, positions, owned=None):
    with sf() as s:
        s.add(InstanceState(instance_id=instance_id, virtual_cash=cash,
                            virtual_positions=positions, last_update=datetime.now().isoformat(),
                            owned_symbols=owned))
        s.commit()


def test_total_reconcile_result_fields():
    from app.schemas.reconcile import TotalReconcileResult
    r = TotalReconcileResult(snapshot_time="t", n_symbols=3, n_matched=2, n_mismatched=1,
        mismatches=[{"symbol":"511260.SH","qmt":13000,"ledger_sum":10000,"diff":3000,"per_instance":{"paper_v53_v53":10000}}],
        cash_ok=True, ledger_cash_total=2e7, qmt_cash=2e7)
    assert r.n_mismatched == 1 and r.mismatches[0]["diff"] == 3000
