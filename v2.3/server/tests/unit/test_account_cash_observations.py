"""Actual QMT cash facts persist without strategy authorization or projection."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, make_engine, make_session_factory
from app.exceptions import APIError
from app.main import create_app
from app.models import CashFlowJournal, InstanceState, Order
from app.models.account_cash_observation import AccountCashObservation
from app.schemas.account_cash_observation import AccountCashObservationRequest
from app.services.account_cash_observation import AccountCashObservationService


def request(**changes):
    values = dict(
        execution_domain="live", account_alias="test-live", source="qmt-cash-ledger",
        source_event_id="bank-transfer-1", event_type="DEPOSIT", amount="19000000.00",
        observed_at="2026-09-08T09:00:00+08:00", evidence_sha256="a" * 64,
        qmt_cash_balance="19211000.00", qmt_available_cash="19211000.00",
    )
    values.update(changes)
    return AccountCashObservationRequest(**values)


@pytest.fixture
def observations(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/observations.db")
    init_db(engine)
    sf = make_session_factory(engine)
    yield AccountCashObservationService(sf), sf
    engine.dispose()


def test_external_deposit_persists_without_a_strategy_or_allocation(observations):
    service, sf = observations
    result = service.record(request())
    assert result.status == "RECORDED_UNALLOCATED"
    assert not result.strategy_balance_changed
    with sf() as session:
        row = session.get(AccountCashObservation, result.observation_id)
        assert row.amount == 19_000_000
        assert row.evidence_sha256 == "a" * 64
        assert row.payload["qmt_available_cash"] == "19211000.00"
        assert session.query(InstanceState).count() == 0
        assert session.query(CashFlowJournal).count() == 0
        assert session.query(Order).count() == 0


def test_replay_is_immutable_and_retains_original_evidence(observations):
    service, sf = observations
    first = service.record(request())
    second = service.record(request(amount=19000000))
    assert second.already_recorded
    assert first.model_dump(exclude={"already_recorded"}) == second.model_dump(exclude={"already_recorded"})
    with pytest.raises(APIError) as error:
        service.record(request(qmt_available_cash=1))
    assert error.value.http_status == 409
    with sf() as session:
        assert session.query(AccountCashObservation).count() == 1


def test_withdrawal_and_negative_balance_fact_are_not_blocked(observations):
    service, sf = observations
    result = service.record(request(
        event_type="WITHDRAWAL", amount=-20000000,
        qmt_cash_balance=-789000, qmt_available_cash=-789000,
    ))
    assert result.status == "RECORDED_UNALLOCATED"
    with sf() as session:
        assert session.get(AccountCashObservation, result.observation_id).amount == -20000000


def test_events_in_other_account_or_domain_have_independent_identity(observations):
    service, sf = observations
    service.record(request())
    service.record(request(account_alias="second-account"))
    service.record(request(execution_domain="paper"))
    with sf() as session:
        assert session.query(AccountCashObservation).count() == 3


def test_parallel_delivery_has_one_durable_fact(observations):
    service, sf = observations
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(lambda _: service.record(request()), range(4)))
    assert len({row.observation_id for row in rows}) == 1
    assert sum(not row.already_recorded for row in rows) == 1
    with sf() as session:
        assert session.query(AccountCashObservation).count() == 1


def live_client(settings_for_test):
    settings = settings_for_test.model_copy(update={
        "live_api_key": "TEST_LIVE_KEY", "live_client_id": "test-live-client",
        "live_account_aliases_csv": "test-live",
        "live_cash_flow_ingest_enabled": False,
        "live_order_generation_enabled": False,
        "live_order_delivery_enabled": False,
        "live_account_initialization_enabled": False,
    })
    return TestClient(create_app(settings_override=settings))


def test_live_fact_api_does_not_require_legacy_cash_flow_or_trading_gates(settings_for_test):
    client = live_client(settings_for_test)
    response = client.post("/accounts/cash-observations", json=request().model_dump(mode="json"),
                           headers={"Authorization": "Bearer TEST_LIVE_KEY"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "RECORDED_UNALLOCATED"
    assert response.json()["data"]["strategy_balance_changed"] is False


@pytest.mark.parametrize("payload,key,status", [
    ({}, None, 401),
    ({}, "TEST_KEY", 403),
    ({"account_alias": "not-allowed"}, "TEST_LIVE_KEY", 403),
    ({"execution_domain": "paper"}, "TEST_LIVE_KEY", 403),
])
def test_fact_auth_retains_only_domain_and_account_boundary(settings_for_test, payload, key, status):
    client = live_client(settings_for_test)
    response = client.post("/accounts/cash-observations", json=request(**payload).model_dump(mode="json"),
                           headers={"Authorization": f"Bearer {key}"} if key else {})
    assert response.status_code == status, response.text


def test_fact_api_cannot_smuggle_strategy_balance_change(settings_for_test):
    client = live_client(settings_for_test)
    payload = request().model_dump(mode="json") | {"instance_id": "hydra", "virtual_cash": 19000000}
    response = client.post("/accounts/cash-observations", json=payload,
                           headers={"Authorization": "Bearer TEST_LIVE_KEY"})
    # Existing app contract wraps validation failures in HTTP 200 / code 1002.
    assert response.json()["code"] == 1002, response.text
    assert response.json()["data"] is None
