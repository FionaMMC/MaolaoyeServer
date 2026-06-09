import json
from pathlib import Path
import pytest
from app.db import init_db, make_engine, make_session_factory
from app.models import PerfSnapshot
from app.services.ops_monitor import OpsMonitorService

@pytest.fixture
def sf(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/t.db"); init_db(eng)
    return make_session_factory(eng)

def _snap(s, inst, date, nav, ret, pos):
    s.add(PerfSnapshot(instance_id=inst, date=date, nav=nav, daily_return=ret,
                       positions_snapshot=json.dumps(pos)))

def test_snapshot_integrity_flags_frozen(sf):
    inst = "paper_v53_v53"
    with sf() as s:
        _snap(s, inst, "20260522", 9_951_130, 0.013, {"X": 100})
        _snap(s, inst, "20260525", 9_951_130, 0.0, {"X": 100})  # frozen: same nav, ret 0 on trading day
        s.commit()
    svc = OpsMonitorService(sf)
    issues = svc.snapshot_integrity(inst, lookback=30)["issues"]
    assert any(i["type"] == "frozen" and i["date"] == "20260525" for i in issues)

def test_overnight_position_anomaly_flags_doubling(sf):
    inst = "paper_v53_v53"
    with sf() as s:
        _snap(s, inst, "20260608", 9_888_426, -0.0036, {"511260.SH": 49500, "510300.SH": 118500})
        _snap(s, inst, "20260609", 16_608_072, 0.68, {"511260.SH": 99000, "510300.SH": 118500})
        s.commit()
    svc = OpsMonitorService(sf)
    an = svc.overnight_position_anomalies(inst, threshold=0.5)
    assert any(a["symbol"] == "511260.SH" and round(a["ratio"], 2) == 2.0 for a in an)
    assert all(a["symbol"] != "510300.SH" for a in an)  # unchanged not flagged
