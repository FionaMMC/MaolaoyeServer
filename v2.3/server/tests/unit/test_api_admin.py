"""POST /admin/run-pipeline 测试"""

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def test_admin_run_pipeline_no_yaml(client, settings_for_test):
    """无 strategies.yaml 时管线应正常退出。"""
    r = client.post("/admin/run-pipeline?trade_date=20260430", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["instances"] == 0


def test_admin_run_pipeline_no_auth_returns_401(client):
    r = client.post("/admin/run-pipeline?trade_date=20260430")
    assert r.status_code == 401


def test_admin_run_pipeline_bad_date(client):
    r = client.post("/admin/run-pipeline?trade_date=abc", headers=_AUTH)
    assert r.json()["code"] == 1002
