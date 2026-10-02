from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, HydraMonthlyCycle, HydraExecutionPlan, Order

HEADERS = {"Authorization": "Bearer TEST_KEY"}


def seed(settings):
    settings.strategies_file.write_text("account_groups: []\n")
    settings.hydra_monthly_enabled = True
    settings.hydra_monthly_account_alias = "scoped"
    engine = make_engine(settings.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as session:
        session.add_all([
            InstanceState(instance_id="live_hydra_v481_rb", execution_domain="live", account_alias="scoped",
                          ledger_mode="attributed", virtual_cash=1234.56, virtual_positions={"511260.SH":100}, last_update="2026-09-18T15:00:00+08:00"),
            InstanceState(instance_id="retired_paper", virtual_cash=99999999, virtual_positions={}, last_update="old"),
        ])
        session.commit()
    return engine, sf


def test_live_instance_is_visible_without_registering_it_in_paper_pipeline(client, settings_for_test):
    engine, sf = seed(settings_for_test)
    for route, key in (("/admin/health", "instances"), ("/admin/portfolio-overview", "items")):
        response = client.get(route, headers=HEADERS)
        assert response.status_code == 200
        rows = response.json()["data"][key]
        assert [r["instance_id"] for r in rows] == ["live_hydra_v481_rb"]
        assert rows[0]["execution_domain"] == "live"
        assert rows[0]["ledger_mode"] == "attributed"
        assert rows[0]["display_name"] == "Hydra 4.8 · v48.1-RB"
        assert rows[0]["latest_nav"] is None  # cash must not masquerade as total NAV
    assert settings_for_test.strategies_file.read_text() == "account_groups: []\n"
    engine.dispose()


def test_hydra_status_is_read_only_scoped_and_does_not_claim_client_ready(client, settings_for_test):
    engine, sf = seed(settings_for_test)
    with sf() as session:
        session.add(HydraMonthlyCycle(cycle_id="other", instance_id="live_hydra_v481_rb",
            execution_domain="live", account_alias="different", as_of_date="20991231", input_hashes={}, status="PLANNED", result={}, created_at="later"))
        session.commit()
    response = client.get("/admin/ops/live-snapshot?instance_id=live_hydra_v481_rb", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()["data"]
    h = result["hydra"]
    assert h["cash"] == 1234.56 and h["positions"] == {"511260.SH":100}
    assert h["monthly_status"] == "NO_MONTHLY_INPUT"
    assert h["client_readiness"] == "UNKNOWN"
    assert result["instance"]["nav"] is None
    with sf() as session:
        assert session.query(Order).count() == session.query(HydraExecutionPlan).count() == 0
        assert session.get(InstanceState, "live_hydra_v481_rb").virtual_cash == 1234.56
    engine.dispose()


def test_hydra_status_reports_received_not_completed(client, settings_for_test):
    engine, sf = seed(settings_for_test)
    with sf() as session:
        session.add(HydraMonthlyCycle(cycle_id="ours", instance_id="live_hydra_v481_rb",
            execution_domain="live", account_alias="scoped", as_of_date="20260930", input_hashes={}, status="RECEIVED", result={}, created_at="now"))
        session.commit()
    data = client.get("/admin/ops/live-snapshot?instance_id=live_hydra_v481_rb", headers=HEADERS).json()["data"]["hydra"]
    assert data["monthly_status"] == "RECEIVED" and data["plan_status"] is None
    assert data["client_readiness"] == "UNKNOWN"
    engine.dispose()


def test_dashboard_keeps_authentication_and_live_client_scope(client, settings_for_test):
    engine, sf = seed(settings_for_test)
    url = "/admin/ops/live-snapshot?instance_id=live_hydra_v481_rb"
    assert client.get(url).status_code == 401
    settings_for_test.live_api_key = "ONLY_EXECUTION_TEST"
    settings_for_test.live_client_id = "windows"
    settings_for_test.live_account_aliases_csv = "scoped"
    assert client.get(url, headers={"Authorization":"Bearer ONLY_EXECUTION_TEST"}).status_code == 403
    html = client.get("/dashboard").text
    assert 'id="hydra-summary"' in html
    assert "new URLSearchParams(location.search).get('instance_id')" in html
    assert "策略账本现金 · 非总资产" in html
    assert "V20H 策略状态" not in html
    assert client.get("/admin/ops/live-snapshot?instance_id=retired_paper",headers=HEADERS).json()["data"]["hydra"] is None
    engine.dispose()


def test_hydra_orders_use_live_alias_and_deduplicate_cumulative_fills(client,settings_for_test):
    from app.models import Trade
    engine,sf = seed(settings_for_test)
    with sf() as session:
        for order_id,domain,alias in [('ours','live','scoped'),('paper','paper','scoped'),('other','live','different')]:
            session.add(Order(order_id=order_id,execution_domain=domain,qmt_account_alias=alias,
                target_id='target',account_group=alias,symbol='511260.SH',direction='BUY',
                quantity=200,limit_price=100,valid_date='20990101',status='FILLED',created_at='2099-01-01T10:00:00+08:00'))
        for qty in [100,200]:
            session.add(Trade(order_id='ours',execution_domain='live',filled_quantity=qty,
                filled_price=100,received_at='2099-01-01T10:00:00+08:00',status='FILLED'))
        session.commit()
    data = client.get('/admin/ops/live-snapshot?instance_id=live_hydra_v481_rb',headers=HEADERS).json()['data']
    assert data['execution']['scope'] == 'hydra_live_account_alias'
    assert data['execution']['orders_total'] == 1
    assert data['execution']['filled_notional'] == 20000
    assert data['execution']['estimated_fees'] is None
    assert [o['order_id'] for o in data['recent_orders']] == ['ours']
    engine.dispose()


def test_retired_shadows_hidden_but_history_survives(client,settings_for_test):
    from app.models import ShadowInstanceState, ShadowNavSnapshot
    engine,sf = seed(settings_for_test)
    settings_for_test.strategies_file.write_text('account_groups: []\nshadow_instances:\n  - shadow_id: Shadow_ML_TOP2\n    enabled: false\n')
    with sf() as session:
        session.add(ShadowInstanceState(shadow_id='Shadow_ML_TOP2',initial_cash=100,virtual_cash=100,
            virtual_positions={},last_update='now'))
        session.add(ShadowNavSnapshot(shadow_id='Shadow_ML_TOP2',date='20260918',nav=100,virtual_cash=100,
            positions_snapshot={},transaction_cost=0,turnover=0,created_at='now'))
        session.commit()
    for endpoint,key in [('/admin/health','instances'),('/admin/portfolio-overview','items')]:
        rows = client.get(endpoint,headers=HEADERS).json()['data'][key]
        assert all(r['instance_id'] != 'Shadow_ML_TOP2' for r in rows)
    assert client.get('/admin/shadow/summary',headers=HEADERS).json()['data']['items'] == []
    history = client.get('/admin/shadow/nav-history?shadow_id=Shadow_ML_TOP2',headers=HEADERS).json()['data']['items']
    assert len(history) == 1
    engine.dispose()
