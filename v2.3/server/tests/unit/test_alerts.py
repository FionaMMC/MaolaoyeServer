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


def test_pipeline_no_signal_with_snapshot_not_flagged_critical(sf):
    """周更策略：调仓日(0622/0623)发了信号，其余交易日(0624-26)只标 NAV 快照 →
    管线确实跑了，不应误报 critical『管线缺失运行』。复刻 2026-06-26 v20h 现网误报。"""
    inst = "paper_v20h_v20h_v1_3"
    from app.models import RawSignal
    navs = {"20260622": 1_000_000, "20260623": 1_001_000, "20260624": 1_002_000,
            "20260625": 1_001_500, "20260626": 1_003_000}
    with sf() as s:
        for d, nav in navs.items():
            s.add(PerfSnapshot(instance_id=inst, date=d, nav=nav, daily_return=0.001,
                               positions_snapshot=json.dumps({"X": 100})))
        for d in ("20260622", "20260623"):   # 调仓日才发信号
            s.add(RawSignal(signal_id=f"s{d}", instance_id=inst, symbol="600000.SH",
                            direction="SELL", quantity=100, reference_price=1.0,
                            price_offset=0.0, limit_price=1.0, valid_date=d,
                            signal_time=f"{d[:4]}-{d[4:6]}-{d[6:8]}T16:00:00+08:00",
                            precheck_status="PASS"))
        s.commit()
    eng = AlertEngine(OpsMonitorService(sf), instances=[inst])
    alerts = eng.run_checks(today="20260626")
    assert not [a for a in alerts if a.category == "pipeline"]


def test_pipeline_no_snapshot_no_signal_still_flagged_critical(sf):
    """交易日既无信号又无快照（管线真没跑）→ 仍报 critical，确认修复没把告警整条关掉。"""
    eng = AlertEngine(OpsMonitorService(sf), instances=["paper_v20h_v20h_v1_3"])
    alerts = eng.run_checks(today="20260626")
    crit = [a for a in alerts if a.category == "pipeline" and a.severity == "critical"]
    assert crit
