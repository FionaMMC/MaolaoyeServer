"""Admin 只读查询端点测试：/admin/orders /admin/nav-history /admin/instance-state 等"""
from datetime import datetime, timezone

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _seed_orders(client, settings_for_test):
    """往 test db 注入几条 orders + signals + trades。"""
    from app.db import init_db, make_engine, make_session_factory
    from app.models import Order, RawSignal, Trade

    engine = make_engine(settings_for_test.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        # 4 个 5/8 orders: 2 paper_v20h FILLED, 1 real_A REJECTED, 1 paper_v20h PENDING
        s.add(Order(order_id="o1", account_group="paper_v20h", symbol="600519.SH",
                    direction="BUY", quantity=100, limit_price=10.0,
                    valid_date="20260508", status="FILLED", created_at=_now()))
        s.add(Order(order_id="o2", account_group="paper_v20h", symbol="000001.SZ",
                    direction="BUY", quantity=200, limit_price=8.0,
                    valid_date="20260508", status="FILLED", created_at=_now()))
        s.add(Order(order_id="o3", account_group="real_A", symbol="002001.SZ",
                    direction="BUY", quantity=100, limit_price=15.0,
                    valid_date="20260508", status="REJECTED", created_at=_now()))
        s.add(Order(order_id="o4", account_group="paper_v20h", symbol="600519.SH",
                    direction="SELL", quantity=50, limit_price=10.5,
                    valid_date="20260509", status="PENDING", created_at=_now()))
        # raw_signals
        s.add(RawSignal(signal_id="s1", instance_id="paper_v20h_v20h_v1_3",
                        symbol="600519.SH", direction="BUY", quantity=100,
                        reference_price=10.0, price_offset=0.005, limit_price=10.05,
                        valid_date="20260508", signal_time=_now(),
                        precheck_status="PASS", precheck_reason=None))
        # trades
        s.add(Trade(order_id="o1", filled_quantity=100, filled_price=9.95,
                    filled_time="2026-05-08T09:25:00", status="FILLED",
                    received_at="2026-05-08T15:10:00+08:00"))
        s.commit()


# ── /admin/orders ──────────────────────────────────────────────
def test_admin_orders_no_filter_returns_all(client, settings_for_test):
    _seed_orders(client, settings_for_test)
    r = client.get("/admin/orders", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 4
    assert body["data"]["returned"] == 4


def test_admin_orders_filter_by_date(client, settings_for_test):
    _seed_orders(client, settings_for_test)
    r = client.get("/admin/orders?date=20260508", headers=_AUTH)
    body = r.json()
    assert body["data"]["total"] == 3


def test_admin_orders_filter_by_status(client, settings_for_test):
    _seed_orders(client, settings_for_test)
    r = client.get("/admin/orders?status=FILLED", headers=_AUTH)
    body = r.json()
    assert body["data"]["total"] == 2


def test_admin_orders_filter_combo(client, settings_for_test):
    _seed_orders(client, settings_for_test)
    r = client.get(
        "/admin/orders?date=20260508&account_group=paper_v20h&status=FILLED",
        headers=_AUTH,
    )
    body = r.json()
    assert body["data"]["total"] == 2


def test_admin_orders_no_auth(client):
    r = client.get("/admin/orders")
    assert r.status_code == 401


# ── /admin/orders-summary ─────────────────────────────────────
def test_admin_orders_summary(client, settings_for_test):
    _seed_orders(client, settings_for_test)
    r = client.get("/admin/orders-summary?date=20260508", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    by_date = body["data"]["by_date"]
    assert "20260508" in by_date
    assert by_date["20260508"]["paper_v20h"]["BUY"]["FILLED"] == 2
    assert by_date["20260508"]["real_A"]["BUY"]["REJECTED"] == 1


# ── /admin/nav-history ────────────────────────────────────────
def test_admin_nav_history(client, settings_for_test):
    from app.db import init_db, make_engine, make_session_factory
    from app.models import PerfSnapshot

    engine = make_engine(settings_for_test.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(PerfSnapshot(instance_id="paper_v20h_v20h_v1_3", date="20260508",
                           nav=10_000_000.0, daily_return=0.0,
                           positions_snapshot={"600519.SH": 100}))
        s.add(PerfSnapshot(instance_id="paper_v20h_v20h_v1_3", date="20260511",
                           nav=10_071_966.0, daily_return=0.007197,
                           positions_snapshot={"600519.SH": 100, "000001.SZ": 200}))
        s.commit()

    r = client.get("/admin/nav-history?instance_id=paper_v20h_v20h_v1_3", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["count"] == 2
    items = body["data"]["items"]
    # 倒序：5/11 排第一
    assert items[0]["date"] == "20260511"
    assert items[0]["nav"] == 10_071_966.0
    assert items[0]["positions_count"] == 2


def test_admin_nav_history_includes_selected_scope(client, settings_for_test):
    from app.db import init_db, make_engine, make_session_factory
    from app.models import PerfSnapshot

    engine = make_engine(settings_for_test.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(PerfSnapshot(instance_id="one", date="20260508", nav=100.0,
                           daily_return=0.0, positions_snapshot={}))
        s.add(PerfSnapshot(instance_id="two", date="20260508", nav=200.0,
                           daily_return=0.0, positions_snapshot={}))
        s.commit()

    r = client.get("/admin/nav-history?instance_id=two", headers=_AUTH)
    body = r.json()
    assert body["data"]["instance_id"] == "two"
    assert body["data"]["period"] is None
    assert [item["instance_id"] for item in body["data"]["items"]] == ["two"]


def test_admin_nav_history_applies_period(client, settings_for_test):
    from app.db import init_db, make_engine, make_session_factory
    from app.models import PerfSnapshot

    engine = make_engine(settings_for_test.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(PerfSnapshot(instance_id="scoped", date="20260102", nav=100.0,
                           daily_return=0.0, positions_snapshot={}))
        s.add(PerfSnapshot(instance_id="scoped", date="20260717", nav=110.0,
                           daily_return=0.1, positions_snapshot={}))
        s.commit()

    r = client.get("/admin/nav-history?instance_id=scoped&period=7d", headers=_AUTH)
    body = r.json()
    assert body["data"]["period"] == "7d"
    assert [item["date"] for item in body["data"]["items"]] == ["20260717"]


# ── /admin/instance-state ─────────────────────────────────────
def test_admin_instance_state(client, settings_for_test):
    from app.db import init_db, make_engine, make_session_factory
    from app.models import InstanceState

    engine = make_engine(settings_for_test.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(InstanceState(instance_id="paper_v20h_v20h_v1_3", virtual_cash=434_181.0,
                            virtual_positions={"600519.SH": 100, "000001.SZ": 200},
                            last_update=_now()))
        s.commit()

    r = client.get("/admin/instance-state", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["count"] == 1
    item = body["data"]["items"][0]
    assert item["holdings_count"] == 2
    assert item["total_shares"] == 300
    assert item["virtual_cash"] == 434_181.0
    # 默认不含 positions detail
    assert "virtual_positions" not in item


def test_admin_instance_state_with_positions(client, settings_for_test):
    from app.db import init_db, make_engine, make_session_factory
    from app.models import InstanceState

    engine = make_engine(settings_for_test.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(InstanceState(instance_id="x", virtual_cash=0.0,
                            virtual_positions={"a": 1}, last_update=_now()))
        s.commit()

    r = client.get("/admin/instance-state?include_positions=true", headers=_AUTH)
    body = r.json()
    assert body["data"]["items"][0]["virtual_positions"] == {"a": 1}


# ── /admin/trades ──────────────────────────────────────────────
def test_admin_trades(client, settings_for_test):
    _seed_orders(client, settings_for_test)
    r = client.get("/admin/trades", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["order_id"] == "o1"
    assert body["data"]["items"][0]["filled_quantity"] == 100


# ── /admin/signals ────────────────────────────────────────────
def test_admin_signals(client, settings_for_test):
    _seed_orders(client, settings_for_test)
    r = client.get("/admin/signals", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["precheck_status"] == "PASS"


def test_admin_signals_filter_by_precheck_status(client, settings_for_test):
    _seed_orders(client, settings_for_test)
    r = client.get("/admin/signals?precheck_status=FAIL", headers=_AUTH)
    body = r.json()
    assert body["data"]["total"] == 0


# ── /admin/health ─────────────────────────────────────────────
def test_admin_health_returns_snapshot(client, settings_for_test):
    """health 端点应返回 pred_status + blacklist + pending + instances 4 大块。"""
    r = client.get("/admin/health", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert "timestamp" in data
    assert "blacklist" in data
    assert "pending_orders_total" in data
    assert "orders_by_date_status_7d" in data
    assert "instances" in data


def test_admin_health_no_auth(client):
    r = client.get("/admin/health")
    assert r.status_code == 401
