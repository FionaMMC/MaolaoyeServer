"""One received dividend can belong to several strategies, never to all twice."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.exceptions import APIError
from app.models import CashFlowJournal, IncomeAllocationReceipt, InstanceState, Order
from app.schemas.cash_flow import CashFlowRequest
from app.schemas.income_allocation import IncomeAllocationRequest
from app.services.cash_flow import CashFlowService
from app.services.income_allocation import IncomeAllocationService, SOURCE
from app.services.strategy_capital import StrategyCapitalService
from tests.unit.test_account_cash_observations import (
    observations as observations,
    request as fact_request,
    live_client,
)


def request(observation_id, **changes):
    data = dict(
        execution_domain="live",
        account_alias="test-live",
        observation_id=observation_id,
        event_date="20260908",
        evidence_sha256="b" * 64,
        allocations=[
            {"instance_id": "hydra", "amount": "10.00"},
            {"instance_id": "kobold", "amount": "20.00"},
        ],
    )
    return IncomeAllocationRequest(**(data | changes))


@pytest.fixture
def income(observations):
    facts, sf = observations
    fact = facts.record(fact_request(event_type="DIVIDEND", amount=30))
    with sf() as session:
        for owner in ("hydra", "kobold"):
            session.add(
                InstanceState(
                    instance_id=owner,
                    execution_domain="live",
                    account_alias="test-live",
                    ledger_mode="attributed",
                    virtual_cash=100,
                    virtual_positions={"510300.SH": 100},
                    strategy_state={"keep": True},
                    last_update="2026-09-08T09:00:00+08:00",
                )
            )
        session.add(
            Order(
                order_id="pending",
                execution_domain="live",
                qmt_account_alias="test-live",
                account_group="hydra",
                symbol="510300.SH",
                direction="BUY",
                quantity=100,
                limit_price=4,
                valid_date="20260908",
                status="PENDING",
                created_at="2026-09-08T09:00:00+08:00",
            )
        )
        session.commit()
    return IncomeAllocationService(sf), sf, fact.observation_id


def test_explicit_shared_income_conserves_cash_and_releases_suspense(income):
    service, sf, oid = income
    req = request(oid)
    with sf() as session:
        assert StrategyCapitalService.unattributed_income_cash(session, req) == Decimal(30)
    result = service.apply(req)
    assert result.status == "ALLOCATED"
    with sf() as session:
        assert StrategyCapitalService.unattributed_income_cash(session, req) == 0
        for owner, cash in (("hydra", 110), ("kobold", 120)):
            state = session.get(InstanceState, owner)
            assert state.virtual_cash == cash
            assert state.virtual_positions == {"510300.SH": 100}
            assert state.strategy_state == {"keep": True}
        assert session.get(Order, "pending").status == "PENDING"
        assert session.query(Order).count() == 1
        rows = session.query(CashFlowJournal).all()
        assert sum(row.amount for row in rows) == 30
        assert all(row.event_type == "DIVIDEND" for row in rows)  # Income, not added principal.
        assert (
            session.get(IncomeAllocationReceipt, oid).request_payload["evidence_sha256"] == "b" * 64
        )


def test_income_replay_is_canonical_and_parallel_safe(income):
    service, sf, oid = income
    with ThreadPoolExecutor(max_workers=4) as executor:
        replies = list(executor.map(lambda _: service.apply(request(oid)), range(4)))
    assert sum(not result.already_applied for result in replies) == 1
    replay = service.apply(
        request(
            oid,
            allocations=[
                {"instance_id": "kobold", "amount": 20},
                {"instance_id": "hydra", "amount": 10},
            ],
        )
    )
    assert replay.already_applied
    with sf() as session:
        assert session.query(CashFlowJournal).count() == 2
        assert session.get(InstanceState, "hydra").virtual_cash == 110
    with pytest.raises(APIError) as exc:
        service.apply(request(oid, allocations=[{"instance_id": "hydra", "amount": 30}]))
    assert exc.value.http_status == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"allocations": [{"instance_id": "hydra", "amount": 29}]},
        {"allocations": [{"instance_id": "hydra", "amount": 31}]},
        # hydra flushes first; the missing second owner must roll that write back.
        {
            "allocations": [
                {"instance_id": "hydra", "amount": 10},
                {"instance_id": "z-missing", "amount": 20},
            ]
        },
        {"account_alias": "wrong"},
        {"execution_domain": "paper"},
        {"observation_id": 999},
    ],
)
def test_invalid_allocation_is_atomic_and_leaves_income_available(income, changes):
    service, sf, oid = income
    req = request(oid).model_copy(update={})
    req = IncomeAllocationRequest(**(req.model_dump() | changes))
    with pytest.raises(APIError):
        service.apply(req)
    with sf() as session:
        assert session.query(CashFlowJournal).count() == 0
        assert session.query(IncomeAllocationReceipt).count() == 0
        assert session.get(InstanceState, "hydra").virtual_cash == 100
        assert StrategyCapitalService.unattributed_income_cash(session, request(oid)) == 30


def test_cross_account_strategy_cannot_receive_income(income):
    service, sf, oid = income
    with sf() as session:
        session.get(InstanceState, "kobold").account_alias = "wrong"
        session.commit()
    with pytest.raises(APIError) as exc:
        service.apply(request(oid))
    assert exc.value.http_status == 403
    with sf() as session:
        assert session.get(InstanceState, "hydra").virtual_cash == 100


def test_deposit_is_not_disguised_as_income(observations):
    facts, sf = observations
    fact = facts.record(fact_request(event_type="DEPOSIT", amount=30))
    with pytest.raises(APIError, match="capital-movements"):
        IncomeAllocationService(sf).apply(request(fact.observation_id))


def legacy_request(**changes):
    return CashFlowRequest(
        **(
            dict(
                execution_domain="live",
                account_alias="test-live",
                instance_id="hydra",
                event_date="20260908",
                event_type="DIVIDEND",
                amount=30,
                source="qmt-cash-ledger",
                source_event_id="bank-transfer-1",
                evidence_sha256="a" * 64,
            )
            | changes
        )
    )


def test_legacy_cash_flow_cannot_double_book_an_observed_income(income):
    service, sf, oid = income
    for req in (legacy_request(), legacy_request(source=SOURCE)):
        with pytest.raises(APIError):
            CashFlowService(sf).apply(req)
    service.apply(request(oid))
    with pytest.raises(APIError):
        CashFlowService(sf).apply(legacy_request())


def test_already_booked_legacy_income_requires_receipt_migration(income):
    service, sf, oid = income
    with sf() as session:
        session.add(
            CashFlowJournal(
                execution_domain="live",
                account_alias="test-live",
                instance_id="hydra",
                event_date="20260908",
                event_type="DIVIDEND",
                amount=30,
                source="qmt-cash-ledger",
                source_event_id="bank-transfer-1",
                evidence_sha256="a" * 64,
                status="APPLIED",
                created_at="2026-09-08",
                applied_at="2026-09-08",
            )
        )
        session.commit()
    with pytest.raises(APIError, match="旧版入账"):
        service.apply(request(oid))


@pytest.mark.parametrize(
    "allocations",
    [
        [],
        [{"instance_id": "hydra", "amount": "NaN"}],
        [{"instance_id": "hydra", "amount": "0.001"}],
        [{"instance_id": "hydra", "amount": 10}, {"instance_id": "hydra", "amount": 20}],
    ],
)
def test_income_schema_rejects_ambiguous_money(allocations):
    with pytest.raises(ValidationError):
        request(1, allocations=allocations)


def test_income_api_no_trading_gate_but_still_has_account_auth(settings_for_test):
    client = live_client(settings_for_test)
    # Missing observation returns the domain error, not a trading-gate failure.
    response = client.post(
        "/accounts/income-allocations",
        json=request(1).model_dump(mode="json"),
        headers={"Authorization": "Bearer TEST_LIVE_KEY"},
    )
    assert response.status_code == 409, response.text
    response = client.post(
        "/accounts/income-allocations",
        json=request(1, account_alias="wrong").model_dump(mode="json"),
        headers={"Authorization": "Bearer TEST_LIVE_KEY"},
    )
    assert response.status_code == 403, response.text


def test_income_api_commits_and_returns_original_receipt_on_retry(income, settings_for_test):
    from app.dependencies import get_session_factory

    _, sf, oid = income
    client = live_client(settings_for_test)
    client.app.dependency_overrides[get_session_factory] = lambda: sf
    for repeated in (False, True):
        response = client.post(
            "/accounts/income-allocations",
            json=request(oid).model_dump(mode="json"),
            headers={"Authorization": "Bearer TEST_LIVE_KEY"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["already_applied"] is repeated
        assert len(response.json()["data"]["allocations"]) == 2
