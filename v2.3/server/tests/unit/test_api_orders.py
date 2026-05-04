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


def test_get_orders_returns_seeded_orders(client, settings_for_test):
    """e2e: 直接往 DB 里写一条订单，再 GET /orders 看能否返回。"""
    from datetime import datetime, timezone

    from app.db import make_session_factory
    from app.dependencies import _engine_for_url
    from app.models import Order

    engine = _engine_for_url(settings_for_test.db_url)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(Order(
            order_id="test-oid-1",
            account_group="real_A",
            symbol="600519.SH",
            direction="BUY",
            quantity=300,
            limit_price=10.05,
            valid_date="20260430",
            status="PENDING",
            created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        ))
        s.commit()

    r = client.get("/orders?date=20260430", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert len(body["data"]["orders"]) == 1
    o = body["data"]["orders"][0]
    assert o["order_id"] == "test-oid-1"
    assert o["account_group"] == "real_A"
    assert o["quantity"] == 300


def test_get_orders_filters_out_non_pending(client, settings_for_test):
    from datetime import datetime, timezone

    from app.db import make_session_factory
    from app.dependencies import _engine_for_url
    from app.models import Order

    engine = _engine_for_url(settings_for_test.db_url)
    sf = make_session_factory(engine)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with sf() as s:
        s.add(Order(order_id="pending-1", account_group="real_A",
                    symbol="A.SH", direction="BUY", quantity=100,
                    limit_price=1.0, valid_date="20260430",
                    status="PENDING", created_at=now))
        s.add(Order(order_id="filled-1", account_group="real_A",
                    symbol="B.SH", direction="BUY", quantity=100,
                    limit_price=1.0, valid_date="20260430",
                    status="FILLED", created_at=now))
        s.commit()

    r = client.get("/orders?date=20260430", headers=_AUTH)
    body = r.json()
    ids = {o["order_id"] for o in body["data"]["orders"]}
    assert ids == {"pending-1"}
