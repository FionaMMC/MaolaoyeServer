"""tests/unit/test_api_ops.py — /admin/ops/* + /admin/alerts + /admin/dashboard-meta"""
import json
from app.models import InstanceState, PerfSnapshot
from app.db import make_engine, make_session_factory, init_db


def _seed(settings):
    eng = make_engine(settings.db_url)
    init_db(eng)
    sf = make_session_factory(eng)
    with sf() as s:
        s.add(InstanceState(
            instance_id="paper_v53_v53",
            virtual_cash=608_072,
            virtual_positions={"511260.SH": 99_000},
            last_update="2026-06-09T15:30:00+08:00",
        ))
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


def test_dashboard_contains_interactive_equity_curve(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.text
    assert "PORTFOLIO EQUITY CURVE" in html
    assert "data-trajectory-mode=\"drawdown\"" in html
    assert "data-trajectory-mode=\"exposure\"" in html
    assert "id=\"trajectory-benchmark\"" in html
    assert "BENCHMARK NOT INGESTED" not in html
    assert "renderCapitalTrajectory" in html
    assert "STRATEGY → FILL PRICE ATTRIBUTION" in html
    assert "/admin/metrics/execution-analysis?" in html


def test_ops_endpoints_authed(client, settings_for_test):
    _seed(settings_for_test)
    h = {"Authorization": "Bearer TEST_KEY"}
    for path in ["/admin/ops/pipeline-runs", "/admin/ops/snapshot-integrity?instance_id=paper_v53_v53",
                 "/admin/ops/reconcile-anomalies?instance_id=paper_v53_v53"]:
        r = client.get(path, headers=h)
        assert r.status_code == 200 and r.json()["code"] == 0


def test_live_snapshot_exposes_observed_metrics_and_coverage_gaps(client, settings_for_test):
    _seed(settings_for_test)
    response = client.get(
        "/admin/ops/live-snapshot?instance_id=paper_v53_v53&days=30",
        headers={"Authorization": "Bearer TEST_KEY"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["instance"]["nav"] == 16_608_072
    assert data["risk"]["daily_pnl"] == 16_608_072 - 9_888_426
    assert data["controls"]["overnight_position_anomalies"] == 1
    assert data["positions"] == [{"symbol": "511260.SH", "quantity": 99_000.0}]
    assert {item["key"] for item in data["coverage_gaps"]} >= {
        "broker_connection", "market_tick_age", "order_ack_latency",
        "intraday_marked_exposure",
    }
