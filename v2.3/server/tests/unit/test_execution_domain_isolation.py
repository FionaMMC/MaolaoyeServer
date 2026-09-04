"""Paper/live API 不可跨域读取或回报订单。"""
import hashlib
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db import make_session_factory
from app.dependencies import _engine_for_url
from app.main import create_app
from app.models import CashFlowJournal, InstanceState, Order
from app.settings import Settings


def _client_and_sf(tmp_path):
    live_account_id = "LIVE_ACCOUNT_FOR_TEST"
    settings = Settings(
        paper_api_key="PAPER_ONLY",
        live_api_key="LIVE_ONLY",
        live_client_id="hydra-live-client",
        live_account_aliases_csv="hydra-live",
        live_qmt_account_sha256=hashlib.sha256(
            live_account_id.encode()
        ).hexdigest(),
        live_order_delivery_enabled=True,
        db_url=f"sqlite:///{tmp_path}/domain.db",
        parquet_root=tmp_path / "data",
        plugins_dir=tmp_path / "plugins",
        strategies_file=tmp_path / "strategies.yaml",
        log_level="WARNING",
    )
    app = create_app(settings_override=settings)
    engine = _engine_for_url(settings.db_url)
    return TestClient(app), make_session_factory(engine)


def _seed_orders(sf):
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with sf() as session:
        session.add_all([
            Order(
                order_id="paper-order",
                execution_domain="paper",
                account_group="paper_hydra",
                qmt_account_alias="hydra-paper",
                symbol="510300.SH",
                direction="BUY",
                quantity=100,
                limit_price=4.0,
                valid_date="20260824",
                status="PENDING",
                created_at=now,
            ),
            Order(
                order_id="live-order",
                execution_domain="live",
                account_group="live_hydra",
                qmt_account_alias="hydra-live",
                symbol="510300.SH",
                direction="BUY",
                quantity=100,
                limit_price=4.0,
                valid_date="20260824",
                status="PENDING",
                created_at=now,
            ),
        ])
        session.commit()


def test_order_tokens_only_read_their_own_domain(tmp_path):
    client, sf = _client_and_sf(tmp_path)
    _seed_orders(sf)

    paper = client.get(
        "/orders?date=20260824",
        headers={"Authorization": "Bearer PAPER_ONLY"},
    ).json()["data"]["orders"]
    live = client.get(
        "/orders?date=20260824",
        headers={"Authorization": "Bearer LIVE_ONLY"},
    ).json()["data"]["orders"]

    assert [row["order_id"] for row in paper] == ["paper-order"]
    assert [row["execution_domain"] for row in paper] == ["paper"]
    assert [row["order_id"] for row in live] == ["live-order"]
    assert [row["execution_domain"] for row in live] == ["live"]


def test_paper_token_cannot_settle_live_order(tmp_path):
    client, sf = _client_and_sf(tmp_path)
    _seed_orders(sf)
    response = client.post(
        "/trade-result",
        headers={"Authorization": "Bearer PAPER_ONLY"},
        json={
            "trade_date": "20260824",
            "execution_domain": "paper",
            "results": [{
                "order_id": "live-order",
                "filled_quantity": 100,
                "filled_price": 4.0,
                "status": "FILLED",
            }],
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["unmatched_order_ids"] == ["live-order"]
    with sf() as session:
        assert session.get(Order, "live-order").status == "PENDING"


def test_payload_cannot_override_token_domain(tmp_path):
    client, _ = _client_and_sf(tmp_path)
    response = client.post(
        "/trade-result",
        headers={"Authorization": "Bearer PAPER_ONLY"},
        json={
            "trade_date": "20260824",
            "execution_domain": "live",
            "results": [],
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == 1001


def test_live_delivery_is_fail_closed_by_default(tmp_path):
    settings = Settings(
        paper_api_key="PAPER_ONLY",
        live_api_key="LIVE_ONLY",
        live_client_id="hydra-live-client",
        live_account_aliases_csv="hydra-live",
        db_url=f"sqlite:///{tmp_path}/closed.db",
        parquet_root=tmp_path / "data",
        plugins_dir=tmp_path / "plugins",
        strategies_file=tmp_path / "strategies.yaml",
        log_level="WARNING",
    )
    response = TestClient(create_app(settings_override=settings)).get(
        "/orders?date=20260824",
        headers={"Authorization": "Bearer LIVE_ONLY"},
    )
    assert response.status_code == 423
    assert response.json()["code"] == 3002


def test_live_token_cannot_read_legacy_admin_or_push_shared_market_data(tmp_path):
    client, _ = _client_and_sf(tmp_path)
    headers = {"Authorization": "Bearer LIVE_ONLY"}
    admin = client.get("/admin/dashboard-meta", headers=headers)
    market = client.post(
        "/market-data",
        headers=headers,
        json={"trade_date": "20260824", "stocks": [], "indexes": [], "etfs": []},
    )
    assert admin.status_code == 403
    assert market.status_code == 403
    assert admin.json()["code"] == 1001
    assert market.json()["code"] == 1001


def test_live_reconciliation_requires_account_fingerprint_and_read_only_mode(tmp_path):
    client, _ = _client_and_sf(tmp_path)
    headers = {"Authorization": "Bearer LIVE_ONLY"}
    payload = {
        "instance_id": "live_hydra",
        "execution_domain": "live",
        "account_alias": "hydra-live",
        "qmt_account_id": "WRONG_ACCOUNT",
        "qmt_cash": 1000,
        "qmt_positions": {},
        "snapshot_time": "2026-08-24T09:14:00+08:00",
        "dry_run": True,
        "force": False,
    }
    wrong = client.post("/admin/reconcile-positions", headers=headers, json=payload)
    assert wrong.status_code == 403
    assert "fingerprint" in wrong.json()["detail"]

    payload["qmt_account_id"] = "LIVE_ACCOUNT_FOR_TEST"
    payload["dry_run"] = False
    writable = client.post("/admin/reconcile-positions", headers=headers, json=payload)
    assert writable.status_code == 403
    assert "只读对账" in writable.json()["detail"]


def test_live_strategy_ledger_read_is_scoped_to_authorized_account(tmp_path):
    client, sf = _client_and_sf(tmp_path)
    with sf() as session:
        session.add(InstanceState(
            instance_id="live_hydra",
            execution_domain="live",
            account_alias="hydra-live",
            ledger_mode="attributed",
            virtual_cash=211_000.0,
            virtual_positions={"510300.SH": 100},
            owned_symbols=["510300.SH"],
            strategy_state={"initial_allocated_cash": 211_000.0},
            last_update="2026-09-04T09:00:00+08:00",
        ))
        session.add(CashFlowJournal(
            execution_domain="live",
            account_alias="hydra-live",
            instance_id="live_hydra",
            event_date="20260904",
            event_type="DIVIDEND",
            amount=12.0,
            currency="CNY",
            source="test",
            source_event_id="dividend-1",
            evidence_sha256="a" * 64,
            status="APPLIED",
            created_at="2026-09-04T09:00:00+08:00",
            applied_at="2026-09-04T09:00:00+08:00",
        ))
        session.commit()

    headers = {"Authorization": "Bearer LIVE_ONLY"}
    response = client.get(
        "/accounts/strategy-ledger",
        params={"instance_id": "live_hydra", "account_alias": "hydra-live"},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ledger_mode"] == "attributed"
    assert data["virtual_cash"] == 211_000.0
    assert data["cash_flow_totals"] == {"DIVIDEND": 12.0}

    denied = client.get(
        "/accounts/strategy-ledger",
        params={"instance_id": "live_hydra", "account_alias": "other-live"},
        headers=headers,
    )
    assert denied.status_code == 403
