"""Adjacent-day waiting, fresh publications and server-owned resumption."""

import pandas as pd
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from app.exceptions import APIError
from app.main import create_app
from app.dependencies import get_hydra_relay_service
from app.models import (
    HydraExecutionPlan,
    HydraExecutionAttempt,
    HydraExecutionPublication,
    HydraRebalance,
    InstanceState,
    Order,
)
from app.schemas.hydra_relay import (
    HydraAdvanceRequest,
    HydraExecutionPublishRequest,
    HydraAttemptCloseRequest,
    HydraRetryRequest,
)
from app.services.hydra_execution_policy import next_pair, eligible
from app.services.hydra_execution_publish import publish_execution
from app.services.hydra_execution_advance import advance_execution
from app.services.orders_queue import OrdersQueueService
from tests.unit.test_hydra_relay import _setup, _target, _price_frame

DATES = [
    "20260731",
    "20260803",
    "20260804",
    "20260805",
    "20260807",
    "20260810",
    "20260811",
    "20260930",
    "20261008",
    "20261009",
]


@pytest.fixture
def live(tmp_path):
    service, sf, store, model, raw, actions, calendar = _setup(
        tmp_path, live_enabled=True, state_domain="live"
    )
    req = _target(
        model,
        raw,
        actions,
        calendar,
        execution_domain="live",
        account_alias="hydra-live",
        instance_id="live_hydra",
        execution_date="20260803",
        execution_raw_sha256=None,
    )
    return service, sf, req


def publication(service, date, *, account="hydra-live", multiplier=1):
    frame = _price_frame(date)
    for field in ("open", "high", "low", "close"):
        frame[field] *= multiplier
    return publish_execution(
        service,
        HydraExecutionPublishRequest(
            account_alias=account,
            reference_date=date,
            producer_commit="a" * 40,
            bars=frame.to_dict("records"),
            calendar_dates=DATES,
        ),
    )


def advance(service, date, **changes):
    payload = dict(
        account_alias="hydra-live",
        instance_id="live_hydra",
        reference_date=date,
        actual_cash=1_000_000,
        actual_positions={},
        reconciliation_evidence_sha256="b" * 64,
    )
    payload.update(changes)
    return advance_execution(service, HydraAdvanceRequest(**payload))


@pytest.mark.parametrize(
    "start,expected",
    [
        ("20260731", ("20260803", "20260804")),
        ("20260803", ("20260803", "20260804")),
        ("20260807", ("20260810", "20260811")),
        ("20260930", ("20261008", "20261009")),
        ("20261009", (None, None)),
    ],
)
def test_next_eligible_pair(start, expected):
    calendar = pd.DataFrame({"trade_date": DATES})
    assert next_pair(calendar, start) == expected
    assert not eligible(calendar, "20260731", "20260803")


def test_wait_is_durable_idempotent_and_never_creates_orders(live):
    service, sf, req = live
    first = service.stage_initial(req)
    assert first.status == "WAITING_EXECUTION_DATE"
    assert (first.next_reference_date, first.next_execution_date) == ("20260803", "20260804")
    assert service.stage_initial(req) == first
    with sf() as session:
        assert session.get(HydraExecutionPlan, first.plan_id).request_payload == req.model_dump(
            mode="json"
        )
        assert session.scalars(select(Order)).all() == []
        assert len(session.scalars(select(HydraExecutionPlan)).all()) == 1


def test_publication_resume_keeps_weights_uses_fresh_prices_and_cash(live):
    service, sf, req = live
    waiting = service.stage_initial(req)
    assert advance(service, "20260803")["status"] == "WAITING_EXECUTION_DATA"
    published = publication(service, "20260803", multiplier=0.9)
    with sf() as session:
        session.get(InstanceState, "live_hydra").virtual_cash = 211_000
        session.commit()
    result = advance(service, "20260803", actual_cash=211_000)
    assert result["status"] == "EXECUTION_ADVANCED"
    assert len(result["results"]) == 1
    staged = result["results"][0]
    assert staged["trade_date"] == "20260804"
    with sf() as session:
        plan = session.get(HydraExecutionPlan, waiting.plan_id)
        assert plan.status == "STAGED"
        assert plan.request_payload["weights"] == req.model_dump()["weights"]
        attempt = session.get(HydraExecutionAttempt, staged["attempt_id"])
        assert (
            attempt.risk_snapshot["execution_policy"]["execution_raw_sha256"]
            == published["execution_raw_sha256"]
        )
        assert session.get(HydraRebalance, staged["rebalance_id"]).baseline_cash == 211_000
    orders = OrdersQueueService(sf).list_pending("20260804", "live", ("hydra-live",))
    assert {round(row.execution_reference_price, 3) for row in orders} == {1.8, 3.6}
    assert all(row.execution_policy["reference_date"] == "20260803" for row in orders)
    advance(service, "20260803", actual_cash=211_000)
    assert service.stage_initial(req).attempt_id == staged["attempt_id"]
    with sf() as session:
        assert len(session.scalars(select(HydraExecutionAttempt)).all()) == 1


def test_plan_receipt_crash_recovers_existing_attempt(live):
    service, sf, req = live
    waiting = service.stage_initial(req)
    publication(service, "20260803")
    first = advance(service, "20260803")
    with sf() as session:
        session.get(HydraExecutionPlan, waiting.plan_id).status = "WAITING_EXECUTION_DATE"
        session.commit()
    advance(service, "20260803")
    with sf() as session:
        assert len(session.scalars(select(HydraExecutionAttempt)).all()) == 1
        assert (
            session.get(HydraExecutionPlan, waiting.plan_id).response_payload["attempt_id"]
            == first["results"][0]["attempt_id"]
        )


def test_friday_never_generates_monday_orders(live):
    service, sf, req = live
    service.stage_initial(req)
    publication(service, "20260731")
    result = advance(service, "20260731")
    assert result["status"] == "WAITING_EXECUTION_DATE"
    assert result["next_execution_date"] == "20260804"
    with sf() as session:
        assert session.scalars(select(Order)).all() == []


def test_unjournaled_manual_fill_waits_instead_of_duplicate_buy(live):
    service, sf, req = live
    service.stage_initial(req)
    publication(service, "20260803")
    assert (
        advance(service, "20260803", actual_positions={"510300.SH": 100})["status"]
        == "WAITING_RECONCILIATION"
    )
    with sf() as session:
        assert session.scalars(select(Order)).all() == []


def test_attributed_snapshot_mismatch_is_wait_not_order_failure(live):
    service, sf, req = live
    service.stage_initial(req)
    publication(service, "20260803")
    with sf() as session:
        session.get(InstanceState, "live_hydra").ledger_mode = "attributed"
        session.commit()
    assert (
        advance(service, "20260803", actual_positions={"510300.SH": 100})["status"]
        == "WAITING_RECONCILIATION"
    )
    with sf() as session:
        assert session.scalars(select(Order)).all() == []


def test_extra_account_cash_does_not_block_or_expand_strategy_budget(live):
    service, sf, req = live
    service.stage_initial(req)
    publication(service, "20260803")
    with sf() as session:
        state = session.get(InstanceState, "live_hydra")
        state.ledger_mode = "attributed"
        state.virtual_cash = 211_000
        session.commit()
    result = advance(service, "20260803", actual_cash=19_000_000)
    assert result["status"] == "EXECUTION_ADVANCED"
    with sf() as session:
        orders = session.scalars(select(Order)).all()
        assert sum(order.quantity * order.limit_price for order in orders) < 211_000


def test_advance_rechecks_changed_ledger_inside_stage_transaction(live, monkeypatch):
    service, sf, req = live
    service.stage_initial(req)
    publication(service, "20260803")
    stage = service.stage_initial

    def concurrent_capital_change(*args, **kwargs):
        with sf() as session:
            session.get(InstanceState, "live_hydra").virtual_cash += 100
            session.commit()
        return stage(*args, **kwargs)

    monkeypatch.setattr(service, "stage_initial", concurrent_capital_change)
    with pytest.raises(APIError, match="账户快照"):
        advance(service, "20260803")
    with sf() as session:
        assert session.scalars(select(Order)).all() == []


def test_calendar_rejects_impossible_dates():
    with pytest.raises(ValueError):
        HydraExecutionPublishRequest(
            account_alias="hydra-live",
            reference_date="20260230",
            producer_commit="a" * 40,
            bars=[{}],
            calendar_dates=["20260230", "20260231"],
        )


def test_concurrent_execution_publication_is_idempotent(live):
    from concurrent.futures import ThreadPoolExecutor

    service, sf, _ = live
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: publication(service, "20260803"), range(2)))
    assert results[0] == results[1]
    with sf() as session:
        assert len(session.scalars(select(HydraExecutionPublication)).all()) == 1


def test_server_residual_uses_new_prices_but_keeps_target_shares(live):
    service, sf, req = live
    service.stage_initial(req)
    publication(service, "20260803")
    staged = advance(service, "20260803")["results"][0]
    with sf() as session:
        target_shares = dict(session.get(HydraRebalance, staged["rebalance_id"]).target_shares)
        for order in session.scalars(select(Order)):
            order.status = "CANCELLED"
        session.commit()
    service.close_attempt(
        HydraAttemptCloseRequest(
            execution_domain="live",
            account_alias="hydra-live",
            attempt_id=staged["attempt_id"],
            actual_cash=1_000_000,
            actual_positions={},
            reconciliation_evidence_sha256="c" * 64,
        )
    )
    pub = publication(service, "20260804", multiplier=0.95)
    retry = advance(service, "20260804")["results"][0]
    assert retry["trade_date"] == "20260805"
    direct = service.stage_retry(
        HydraRetryRequest(
            execution_domain="live",
            account_alias="hydra-live",
            rebalance_id=staged["rebalance_id"],
            trade_date="20260805",
            execution_raw_sha256=pub["execution_raw_sha256"],
            execution_calendar_sha256=pub["execution_calendar_sha256"],
            actual_cash=1_000_000,
            actual_positions={},
            reconciliation_evidence_sha256="b" * 64,
        )
    )
    assert direct.idempotent_replay and direct.attempt_id == retry["attempt_id"]
    orders = OrdersQueueService(sf).list_pending("20260805", "live", ("hydra-live",))
    assert {row.symbol: row.quantity for row in orders} == target_shares
    assert {round(row.execution_reference_price, 2) for row in orders} == {1.9, 3.8}


def test_publication_scoped_and_idempotent(live):
    service, sf, _ = live
    first = publication(service, "20260803", account="other-account")
    assert publication(service, "20260803", account="other-account") == first
    assert advance(service, "20260803")["status"] == "WAITING_EXECUTION_DATA"
    with sf() as session:
        assert len(session.scalars(select(HydraExecutionPublication)).all()) == 1


def test_invalid_execution_bars(live):
    service, _, _ = live
    for bars in (_price_frame("20260803").iloc[:1], _price_frame("20260731")):
        with pytest.raises((APIError, ValueError)):
            publish_execution(
                service,
                HydraExecutionPublishRequest(
                    account_alias="hydra-live",
                    reference_date="20260803",
                    producer_commit="a" * 40,
                    bars=bars.to_dict("records"),
                    calendar_dates=DATES,
                ),
            )


def test_execution_api_scoping_and_wait_receipt(live, settings_for_test):
    service, _, req = live
    settings_for_test.live_api_key = "LIVE_TEST_KEY"
    settings_for_test.live_client_id = "test"
    settings_for_test.live_account_aliases_csv = "hydra-live"
    app = create_app(settings_override=settings_for_test)
    app.dependency_overrides[get_hydra_relay_service] = lambda: service
    client = TestClient(app)
    payload = HydraAdvanceRequest(
        account_alias="other",
        instance_id="live_hydra",
        reference_date="20260803",
        actual_cash=1_000_000,
        actual_positions={},
        reconciliation_evidence_sha256="a" * 64,
    ).model_dump()
    headers = {"Authorization": "Bearer LIVE_TEST_KEY"}
    assert client.post("/hydra/execution/advance", json=payload, headers=headers).status_code == 403
    payload.update(account_alias="hydra-live", execution_domain="paper")
    assert client.post("/hydra/execution/advance", json=payload, headers=headers).status_code == 403
    response = client.post("/hydra/targets/stage", json=req.model_dump(), headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "WAITING_EXECUTION_DATE"
