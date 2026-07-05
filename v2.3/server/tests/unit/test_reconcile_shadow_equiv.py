"""ReconcileService.shadow_compare 测试（影子对比：total vs QMT 一致性探针）。"""
from datetime import datetime
from pathlib import Path

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.services.reconcile import ReconcileService


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


def test_shadow_equiv_disjoint_universes(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_instance(sf, "paper_v20h_v20h_v1_3", 1e7,
                    {"600353.SH": 100, "000727.SZ": 100}, owned=None)
    _seed_instance(sf, "paper_v53_v53", 1e7,
                    {"511260.SH": 10000, "518880.SH": 5000},
                    owned=["511260.SH", "518880.SH"])

    qmt = {"600353.SH": 100, "000727.SZ": 100, "511260.SH": 10000, "518880.SH": 5000}
    report = ReconcileService(sf).shadow_compare(qmt, 2e7, "t")

    assert report["consistent"] is True
    assert report["total"].n_mismatched == 0


def test_shadow_flags_when_ledger_drifts(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_instance(sf, "paper_v53_v53", 1e7, {"511260.SH": 9000}, owned=["511260.SH"])

    report = ReconcileService(sf).shadow_compare({"511260.SH": 10000}, 1e7, "t")

    assert report["consistent"] is False
