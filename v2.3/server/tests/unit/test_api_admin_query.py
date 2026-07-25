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


def test_shadow_summary_is_read_only_and_marks_no_order_boundary(client, settings_for_test):
    from app.db import init_db, make_engine, make_session_factory
    from app.models import ShadowInstanceState, ShadowNavSnapshot

    engine = make_engine(settings_for_test.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    now = _now()
    with sf() as s:
        s.add(ShadowInstanceState(
            shadow_id="Shadow_Base", initial_cash=1_000_000,
            virtual_cash=100_000, virtual_positions={"000001.SZ": 90_000},
            status="active", decision_date="20260701", as_of_date="20260630",
            state_reason="base", source_version="v7.13-base@2538554",
            input_hash="a" * 64, target_hash="b" * 64,
            cumulative_cost=10.0, last_turnover=0.45, last_update=now,
        ))
        s.add(ShadowNavSnapshot(
            shadow_id="Shadow_Base", date="20260701", nav=999_990,
            daily_return=None, virtual_cash=100_000,
            positions_snapshot={"000001.SZ": 90_000}, transaction_cost=10,
            turnover=0.45, decision_date="20260701", as_of_date="20260630",
            state_reason="base", source_version="v7.13-base@2538554",
            input_hash="a" * 64, target_hash="b" * 64, created_at=now,
        ))
        s.commit()

    body = client.get("/admin/shadow/summary", headers=_AUTH).json()["data"]
    assert body["items"][0]["instance_type"] == "shadow_no_order"
    assert body["items"][0]["orders_enabled"] is False
    assert body["items"][0]["nav"] == 999_990

    history = client.get(
        "/admin/shadow/nav-history?shadow_id=Shadow_Base", headers=_AUTH
    ).json()["data"]["items"]
    assert history[0]["input_hash"] == "a" * 64


def test_shadow_ledgers_join_portfolio_selector_and_v79_is_not_shadow(
    client, settings_for_test,
):
    from app.db import init_db, make_engine, make_session_factory
    from app.models import (
        InstanceState,
        PerfSnapshot,
        ShadowInstanceState,
        ShadowNavSnapshot,
    )

    engine = make_engine(settings_for_test.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    now = _now()
    with sf() as s:
        s.add(InstanceState(
            instance_id="paper_v79_v79_relay", virtual_cash=10_000_000,
            virtual_positions={}, owned_symbols=[], last_update=now,
        ))
        s.add(PerfSnapshot(
            instance_id="paper_v79_v79_relay", date="20260724",
            nav=10_000_000, daily_return=0.0, positions_snapshot={},
        ))
        s.add(ShadowInstanceState(
            shadow_id="Shadow_Hydra_V481_RB", initial_cash=10_000_000,
            virtual_cash=100_000, virtual_positions={"510300.SH": 100},
            status="active", cumulative_cost=10.0, last_turnover=0.5,
            last_update=now,
        ))
        s.add(ShadowNavSnapshot(
            shadow_id="Shadow_Hydra_V481_RB", date="20260725",
            nav=9_999_000, daily_return=-0.0001, virtual_cash=100_000,
            positions_snapshot={"510300.SH": 100},
            transaction_cost=10.0, turnover=0.5, created_at=now,
        ))
        s.commit()

    portfolio = client.get("/admin/portfolio-overview", headers=_AUTH).json()["data"]
    by_id = {item["instance_id"]: item for item in portfolio["items"]}
    assert by_id["paper_v79_v79_relay"]["is_shadow"] is False
    assert by_id["Shadow_Hydra_V481_RB"]["is_shadow"] is True
    assert by_id["Shadow_Hydra_V481_RB"]["orders_enabled"] is False

    health = client.get("/admin/health", headers=_AUTH).json()["data"]
    health_by_id = {item["instance_id"]: item for item in health["instances"]}
    assert health_by_id["Shadow_Hydra_V481_RB"]["is_shadow"] is True

    history = client.get(
        "/admin/nav-history?instance_id=Shadow_Hydra_V481_RB&period=all",
        headers=_AUTH,
    ).json()["data"]
    assert history["items"][0]["nav"] == 9_999_000

    summary = client.get(
        "/admin/metrics/summary?instance_id=Shadow_Hydra_V481_RB&period=all",
        headers=_AUTH,
    ).json()["data"]
    assert summary["n_days"] == 1


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
