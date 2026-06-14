import json, pytest
from app.db import init_db, make_engine, make_session_factory
from app.models import PerfSnapshot
from app.services.ops_monitor import OpsMonitorService
from app.services.alerts import AlertEngine, DashboardSink, Alert

@pytest.fixture
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/t.db"); init_db(eng)
    return make_session_factory(eng)

def test_alert_engine_flags_overnight_doubling_critical(sf):
    inst = "paper_v53_v53"
    with sf() as s:
        s.add(PerfSnapshot(instance_id=inst, date="20260608", nav=9_888_426, daily_return=-0.0036,
                           positions_snapshot=json.dumps({"511260.SH": 49500})))
        s.add(PerfSnapshot(instance_id=inst, date="20260609", nav=16_608_072, daily_return=0.68,
                           positions_snapshot=json.dumps({"511260.SH": 99000})))
        s.commit()
    eng = AlertEngine(OpsMonitorService(sf), instances=[inst])
    alerts = eng.run_checks(today="20260609")
    crit = [a for a in alerts if a.severity == "critical" and a.category == "position_anomaly"]
    assert crit and "511260.SH" in crit[0].message

def test_dashboard_sink_stores_latest():
    sink = DashboardSink()
    sink.emit([Alert(id="x", severity="warn", category="c", message="m", as_of="t")])
    assert len(sink.latest()) == 1
