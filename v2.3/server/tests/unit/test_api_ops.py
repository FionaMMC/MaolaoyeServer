"""tests/unit/test_api_ops.py — /admin/ops/* + /admin/alerts + /admin/dashboard-meta"""
import json
from app.models import PerfSnapshot
from app.db import make_engine, make_session_factory, init_db


def _seed(settings):
    eng = make_engine(settings.db_url); init_db(eng); sf = make_session_factory(eng)
    with sf() as s:
        s.add(PerfSnapshot(instance_id="paper_v53_v53", date="20260608", nav=9_888_426,
                           daily_return=-0.0036, positions_snapshot=json.dumps({"511260.SH": 49500})))
        s.add(PerfSnapshot(instance_id="paper_v53_v53", date="20260609", nav=16_608_072,
                           daily_return=0.68, positions_snapshot=json.dumps({"511260.SH": 99000})))
        s.commit()


def test_dashboard_meta_and_alerts(client, settings_for_test):
    _seed(settings_for_test)
    h = {"Authorization": "Bearer TEST_KEY"}
    meta = client.get("/admin/dashboard-meta", headers=h).json()
    assert meta["code"] == 0
    assert "version" in meta["data"] and "alerts" in meta["data"]
    al = client.get("/admin/alerts", headers=h).json()
    cats = [a["category"] for a in al["data"]["alerts"]]
    assert "position_anomaly" in cats


def test_ops_endpoints_authed(client, settings_for_test):
    _seed(settings_for_test)
    h = {"Authorization": "Bearer TEST_KEY"}
    for path in ["/admin/ops/pipeline-runs", "/admin/ops/snapshot-integrity?instance_id=paper_v53_v53",
                 "/admin/ops/reconcile-anomalies?instance_id=paper_v53_v53"]:
        r = client.get(path, headers=h)
        assert r.status_code == 200 and r.json()["code"] == 0
