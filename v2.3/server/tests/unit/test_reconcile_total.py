"""ReconcileService.reconcile_total / shadow_compare 测试（portfolio 级总量对账）。"""
from datetime import datetime
from pathlib import Path

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.services.reconcile import ReconcileService


def _factory(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    return make_session_factory(engine)


def _seed_instance(
    sf, instance_id, cash, positions, owned=None,
    execution_domain="paper", account_alias=None,
):
    with sf() as s:
        s.add(InstanceState(
            instance_id=instance_id,
            execution_domain=execution_domain,
            account_alias=account_alias,
            virtual_cash=cash,
            virtual_positions=positions,
            last_update=datetime.now().isoformat(),
            owned_symbols=owned,
        ))
        s.commit()


def test_total_reconcile_result_fields():
    from app.schemas.reconcile import TotalReconcileResult
    r = TotalReconcileResult(snapshot_time="t", n_symbols=3, n_matched=2, n_mismatched=1,
        mismatches=[{"symbol":"511260.SH","qmt":13000,"ledger_sum":10000,"diff":3000,"per_instance":{"paper_v53_v53":10000}}],
        cash_ok=True, ledger_cash_total=2e7, qmt_cash=2e7)
    assert r.n_mismatched == 1 and r.mismatches[0]["diff"] == 3000


def test_total_reconcile_overlap_sum_matches(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_instance(sf, "paper_v53_v53", 1e7, {"511260.SH": 10000}, owned=["511260.SH"])
    _seed_instance(sf, "paper_v79_v79_relay", 1e7, {"511260.SH": 3000}, owned=["511260.SH"])

    result = ReconcileService(sf).reconcile_total({"511260.SH": 13000}, 2e7, "t")

    assert result.n_mismatched == 0
    assert result.n_matched == 1


def test_total_reconcile_mismatch_alerts_no_write(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_instance(sf, "paper_v53_v53", 1e7, {"511260.SH": 10000}, owned=["511260.SH"])

    result = ReconcileService(sf).reconcile_total({"511260.SH": 12000}, 1e7, "t")

    assert result.n_mismatched == 1
    assert result.mismatches[0]["diff"] == 2000

    with sf() as s:
        inst = s.get(InstanceState, "paper_v53_v53")
        assert inst.virtual_positions == {"511260.SH": 10000}


def test_cash_ok_when_account_has_far_more_cash(tmp_path: Path):
    # 真实场景：QMT 账户现金(2e8)远超各策略台账现金之和(2e7) —— 大量未分配现金。
    # 充足性检查应通过（cash_ok=True），positions 匹配 → shadow consistent=True。
    sf = _factory(tmp_path)
    _seed_instance(sf, "paper_v20h_v20h_v1_3", 1e7, {"600000.SH": 100}, owned=None)
    _seed_instance(sf, "paper_v53_v53", 1e7, {"511260.SH": 10000}, owned=["511260.SH"])
    svc = ReconcileService(sf)
    r = svc.reconcile_total({"600000.SH": 100, "511260.SH": 10000}, 2e8, "t")
    assert r.n_mismatched == 0
    assert r.cash_ok is True
    assert r.unallocated_cash == 180_000_000.0
    report = svc.shadow_compare({"600000.SH": 100, "511260.SH": 10000}, 2e8, "t")
    assert report["consistent"] is True


def test_cash_not_ok_when_account_cash_below_ledger(tmp_path: Path):
    # 透支风险：Σ台账现金(2e7) 明显 > QMT 账户现金(1e6) → cash_ok=False。
    sf = _factory(tmp_path)
    _seed_instance(sf, "paper_v20h_v20h_v1_3", 1e7, {"600000.SH": 100}, owned=None)
    _seed_instance(sf, "paper_v53_v53", 1e7, {"511260.SH": 10000}, owned=["511260.SH"])
    r = ReconcileService(sf).reconcile_total({"600000.SH": 100, "511260.SH": 10000}, 1e6, "t")
    assert r.n_mismatched == 0
    assert r.cash_ok is False


def test_explicit_live_account_ignores_external_positions(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_instance(
        sf, "live_hydra", 72_057, {}, owned=["510300.SH", "159915.SZ"],
        execution_domain="live", account_alias="hydra-live",
    )

    result = ReconcileService(sf).reconcile_total(
        {"920071.BJ": 400, "920268.BJ": 100},
        72_057,
        "t",
        execution_domain="live",
        account_alias="hydra-live",
    )

    assert result.n_symbols == 0
    assert result.n_mismatched == 0
    assert result.n_external_positions == 2
    assert result.external_positions == {"920071.BJ": 400, "920268.BJ": 100}
    assert result.cash_ok is True


def test_total_reconcile_is_scoped_to_explicit_account_alias(tmp_path: Path):
    sf = _factory(tmp_path)
    _seed_instance(
        sf, "live_a", 1_000, {"510300.SH": 100}, owned=["510300.SH"],
        execution_domain="live", account_alias="live-a",
    )
    _seed_instance(
        sf, "live_b", 2_000, {"510300.SH": 200}, owned=["510300.SH"],
        execution_domain="live", account_alias="live-b",
    )

    result = ReconcileService(sf).reconcile_total(
        {"510300.SH": 100}, 1_000, "t",
        execution_domain="live", account_alias="live-a",
    )

    assert result.n_mismatched == 0
    assert result.ledger_cash_total == 1_000
