"""外部现金流 journal：原子入账、幂等、篡改和跨域防护。"""
from app.db import init_db, make_engine, make_session_factory
from app.exceptions import APIError
from app.models import CashFlowJournal, InstanceState
from app.schemas.cash_flow import CashFlowRequest
from app.services.cash_flow import CashFlowService


def _service(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/cash-flow.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as session:
        session.add(InstanceState(
            instance_id="paper_hydra",
            execution_domain="paper",
            account_alias="hydra-paper",
            virtual_cash=1000.0,
            virtual_positions={},
            last_update="2026-08-23T00:00:00+10:00",
        ))
        session.commit()
    return CashFlowService(sf), sf


def _request(**changes):
    values = {
        "execution_domain": "paper",
        "account_alias": "hydra-paper",
        "instance_id": "paper_hydra",
        "event_date": "20260823",
        "event_type": "DIVIDEND",
        "amount": 12.34,
        "source": "mock_qmt",
        "source_event_id": "cash-event-1",
        "evidence_sha256": "a" * 64,
    }
    values.update(changes)
    return CashFlowRequest(**values)


def test_cash_flow_applies_once_and_is_idempotent(tmp_path):
    service, sf = _service(tmp_path)
    first = service.apply(_request())
    second = service.apply(_request())
    assert first.already_applied is False
    assert second.already_applied is True
    assert first.journal_id == second.journal_id
    assert first.virtual_cash_after == 1012.34
    assert second.virtual_cash_after == 1012.34
    with sf() as session:
        assert session.query(CashFlowJournal).count() == 1
        assert session.get(InstanceState, "paper_hydra").virtual_cash == 1012.34


def test_cash_flow_same_source_id_cannot_be_mutated(tmp_path):
    service, _ = _service(tmp_path)
    service.apply(_request())
    try:
        service.apply(_request(amount=99.0))
    except APIError as exc:
        assert exc.http_status == 409
    else:
        raise AssertionError("mutated cash flow must fail")


def test_cash_flow_cannot_cross_instance_domain(tmp_path):
    service, _ = _service(tmp_path)
    try:
        service.apply(_request(execution_domain="live"))
    except APIError as exc:
        assert exc.http_status == 403
    else:
        raise AssertionError("cross-domain cash flow must fail")


def test_withdrawal_cannot_make_cash_negative(tmp_path):
    service, _ = _service(tmp_path)
    try:
        service.apply(_request(
            event_type="WITHDRAWAL",
            amount=-1001.0,
            source_event_id="withdrawal-1",
        ))
    except APIError as exc:
        assert exc.http_status == 409
    else:
        raise AssertionError("negative virtual cash must fail")


def test_capital_allocation_uses_unallocated_physical_cash(tmp_path):
    service, sf = _service(tmp_path)
    with sf() as session:
        state = session.get(InstanceState, "paper_hydra")
        state.ledger_mode = "attributed"
        session.commit()

    result = service.apply(_request(
        event_type="CAPITAL_ALLOCATION",
        amount=500.0,
        qmt_cash=10_000.0,
        snapshot_time="2026-09-04T09:00:00+08:00",
        source_event_id="allocation-1",
    ))

    assert result.virtual_cash_after == 1_500.0
    assert result.account_ledger_cash_after == 1_500.0
    assert result.unallocated_cash_after == 8_500.0
    with sf() as session:
        row = session.query(CashFlowJournal).filter_by(
            source_event_id="allocation-1"
        ).one()
        assert row.qmt_cash_snapshot == 10_000.0
        assert row.snapshot_time == "2026-09-04T09:00:00+08:00"


def test_capital_allocation_cannot_overallocate_shared_account(tmp_path):
    service, sf = _service(tmp_path)
    with sf() as session:
        state = session.get(InstanceState, "paper_hydra")
        state.ledger_mode = "attributed"
        session.add(InstanceState(
            instance_id="paper_other",
            execution_domain="paper",
            account_alias="hydra-paper",
            ledger_mode="attributed",
            virtual_cash=8_500.0,
            virtual_positions={},
            last_update="2026-09-04T00:00:00+08:00",
        ))
        session.commit()

    try:
        service.apply(_request(
            event_type="CAPITAL_ALLOCATION",
            amount=1_000.0,
            qmt_cash=10_000.0,
            snapshot_time="2026-09-04T09:00:00+08:00",
            source_event_id="allocation-over",
        ))
    except APIError as exc:
        assert exc.http_status == 409
    else:
        raise AssertionError("over-allocation must fail")


def test_capital_deallocation_returns_cash_to_unallocated_reserve(tmp_path):
    service, sf = _service(tmp_path)
    with sf() as session:
        state = session.get(InstanceState, "paper_hydra")
        state.ledger_mode = "attributed"
        session.commit()

    result = service.apply(_request(
        event_type="CAPITAL_DEALLOCATION",
        amount=-600.0,
        qmt_cash=10_000.0,
        snapshot_time="2026-09-04T09:00:00+08:00",
        source_event_id="deallocation-1",
    ))

    assert result.virtual_cash_after == 400.0
    assert result.unallocated_cash_after == 9_600.0


def test_existing_instance_can_transition_with_audited_capital_deallocation(tmp_path):
    service, sf = _service(tmp_path)

    result = service.apply(_request(
        event_type="CAPITAL_DEALLOCATION",
        amount=-600.0,
        qmt_cash=10_000.0,
        snapshot_time="2026-09-04T09:00:00+08:00",
        source_event_id="transition-deallocation-1",
        transition_to_attributed=True,
    ))

    assert result.virtual_cash_after == 400.0
    assert result.ledger_mode_after == "attributed"
    with sf() as session:
        state = session.get(InstanceState, "paper_hydra")
        assert state.ledger_mode == "attributed"
        assert state.strategy_state["reconciliation_status"] == "attributed_ledger"
        row = session.query(CashFlowJournal).filter_by(
            source_event_id="transition-deallocation-1"
        ).one()
        assert row.transition_to_attributed is True
