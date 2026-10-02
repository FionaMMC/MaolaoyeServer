import json
from datetime import datetime, timedelta, timezone

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.oms.models import OmsCycle, OmsReconciliation
from app.oms.schemas import SnapshotIn
from app.settings import Settings
from scripts import oms_ops

CN = timezone(timedelta(hours=8))
CAL = ["20260929", "20260930", "20261008", "20261009", "20261012", "20261013", "20261014", "20261015"]


def _env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/ops.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(InstanceState(instance_id="live_hydra_v481_rb", execution_domain="live", account_alias="hydra-live",
                            ledger_mode="attributed", virtual_cash=100000.0, virtual_positions={"510300.SH": 3000},
                            owned_symbols=["510300.SH", "513100.SH"], last_update="x"))
        s.commit()
    settings = Settings(db_url=f"sqlite:///{tmp_path}/ops.db", stock_commission_rate=0.0001,
                        stock_stamp_duty_sell=0.0, log_level="WARNING")
    files = {}
    for name, value in (("w", {"510300.SH": 0.05, "513100.SH": 0.95}), ("c", {"510300.SH": 4.6, "513100.SH": 2.2}),
                        ("cal", CAL)):
        files[name] = tmp_path / f"{name}.json"
        files[name].write_text(json.dumps(value))
    publish = ["publish", "--instance", "live_hydra_v481_rb", "--account", "hydra-live", "--signal-date", "20260930",
               "--weights", str(files["w"]), "--closes", str(files["c"]), "--calendar", str(files["cal"]),
               "--source-sha256", "s" * 64]
    return sf, settings, publish


def test_publish_dry_run_writes_nothing_and_prints_share_list(tmp_path, capsys):
    sf, settings, publish = _env(tmp_path)
    assert oms_ops.main(publish, session_factory=sf, settings=settings) == 0
    out = capsys.readouterr().out
    assert "510300.SH" in out and "DRY RUN" in out
    with sf() as s:
        assert s.query(OmsCycle).count() == 0


def test_publish_apply_then_approve_requires_yes(tmp_path):
    sf, settings, publish = _env(tmp_path)
    oms_ops.main(publish + ["--apply"], session_factory=sf, settings=settings)
    oms_ops.main(["approve", "--cycle-id", "C00001", "--approver", "ops"], session_factory=sf, settings=settings)
    with sf() as s:
        assert s.get(OmsCycle, "C00001").status == "PENDING_APPROVAL"
    oms_ops.main(["approve", "--cycle-id", "C00001", "--approver", "ops", "--yes"], session_factory=sf,
                 settings=settings)
    with sf() as s:
        assert s.get(OmsCycle, "C00001").status == "ACTIVE" and s.get(OmsCycle, "C00001").approved_by == "ops"


def test_resolve_records_decision_and_reactivates_held_cycle(tmp_path):
    sf, settings, publish = _env(tmp_path)
    oms_ops.main(publish + ["--apply"], session_factory=sf, settings=settings)
    oms_ops.main(["approve", "--cycle-id", "C00001", "--approver", "ops", "--yes"], session_factory=sf,
                 settings=settings)
    from app.dependencies import get_oms_cycle_service
    service = get_oms_cycle_service(sf, settings)
    service.ingest_snapshot(SnapshotIn(account_alias="hydra-live", kind="PRE", trade_date="20261008",
                                       taken_at=datetime(2026, 10, 8, 14, 45, tzinfo=CN), available_cash=100000.0,
                                       total_asset=0.0, positions={"510300.SH": 2900}, sellable={}, orders=[]), "n")
    with sf() as s:
        assert s.get(OmsCycle, "C00001").status == "HELD"
    resolve = ["resolve", "--cycle-id", "C00001", "--operator", "ops", "--reason", "broker statement checked"]
    oms_ops.main(resolve, session_factory=sf, settings=settings)
    with sf() as s:
        assert s.get(OmsCycle, "C00001").status == "HELD"
    oms_ops.main(resolve + ["--yes"], session_factory=sf, settings=settings)
    with sf() as s:
        assert s.get(OmsCycle, "C00001").status == "ACTIVE"
        recon = s.query(OmsReconciliation).filter_by(passed=False).one()
        assert recon.resolution == "broker statement checked" and recon.resolved_by == "ops"
