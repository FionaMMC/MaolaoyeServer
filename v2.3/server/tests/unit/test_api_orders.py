"""GET /orders stub tests"""

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def test_get_orders_happy_path(client):
    r = client.get("/orders?date=20260430", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["date"] == "20260430"
    assert body["data"]["orders"] == []


def test_get_orders_no_auth_returns_401(client):
    r = client.get("/orders?date=20260430")
    assert r.status_code == 401


def test_get_orders_missing_date_returns_1002(client):
    r = client.get("/orders", headers=_AUTH)
    assert r.json()["code"] == 1002


def test_get_orders_bad_date_returns_1002(client):
    r = client.get("/orders?date=2026-04-30", headers=_AUTH)
    assert r.json()["code"] == 1002
