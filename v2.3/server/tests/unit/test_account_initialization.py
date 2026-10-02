"""QMT 首次只读快照只能初始化一次，不能伪装成重置工具。"""
from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.exceptions import APIError
from app.models import InstanceState
from app.schemas.account_initialization import AccountInitializationRequest
from app.services.account_initialization import AccountInitializationService


def _service(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/init.db")
    init_db(engine)
    sf = make_session_factory(engine)
    return AccountInitializationService(sf), sf


def _request(**changes):
    payload = {
        "execution_domain": "live",
        "account_alias": "hydra-live",
        "qmt_account_id": "PRIVATE_TEST_ACCOUNT",
        "instance_id": "live_hydra",
        "qmt_cash": 700_000.0,
        "qmt_total_asset": 700_000.0,
        "qmt_positions": {},
        "owned_symbols": ["510300.SH", "159915.SZ"],
        "snapshot_time": "2026-08-23T16:00:00+08:00",
        "evidence_sha256": "a" * 64,
    }
    payload.update(changes)
    return AccountInitializationRequest(**payload)


def test_initialization_creates_reconciled_state_without_storing_account_id(tmp_path):
    service, sf = _service(tmp_path)
    result = service.initialize(_request())
    assert result.idempotent_replay is False
    with sf() as session:
        state = session.get(InstanceState, "live_hydra")
        assert state.execution_domain == "live"
        assert state.virtual_cash == 700_000.0
        assert state.strategy_state["reconciliation_status"] == "ok"
        assert "qmt_account_id" not in state.strategy_state


def test_initialization_is_idempotent_but_never_overwrites(tmp_path):
    service, _ = _service(tmp_path)
    assert service.initialize(_request()).idempotent_replay is False
    assert service.initialize(_request()).idempotent_replay is True
    with pytest.raises(APIError) as captured:
        service.initialize(_request(qmt_cash=699_999.0))
    assert captured.value.http_status == 409


def test_initialization_records_external_positions_but_does_not_assign_them_to_hydra(tmp_path):
    service, sf = _service(tmp_path)
    result = service.initialize(_request(qmt_positions={
        "510300.SH": 100,
        "600000.SH": 200,
    }))
    assert result.positions == {"510300.SH": 100}
    assert result.external_position_count == 1
    with sf() as session:
        state = session.get(InstanceState, "live_hydra")
        assert state.virtual_positions == {"510300.SH": 100}
        assert state.strategy_state["external_positions_snapshot"] == {"600000.SH": 200}
        assert len(state.strategy_state["external_positions_sha256"]) == 64


def test_initialization_rejects_same_account_symbol_overlap_before_commit(tmp_path):
    service, sf = _service(tmp_path)
    with sf() as session:
        session.add(InstanceState(
            instance_id="other_live",
            execution_domain="live",
            account_alias="hydra-live",
            virtual_cash=1.0,
            virtual_positions={},
            owned_symbols=["510300.SH"],
            last_update="2026-09-03T00:00:00+08:00",
        ))
        session.commit()

    with pytest.raises(APIError) as captured:
        service.initialize(_request())

    assert captured.value.http_status == 409
    with sf() as session:
        assert session.get(InstanceState, "live_hydra") is None


def test_attributed_initialization_only_assigns_explicit_capital_and_positions(tmp_path):
    service, sf = _service(tmp_path)
    result = service.initialize(_request(
        qmt_cash=19_149_000.0,
        qmt_total_asset=19_153_000.0,
        qmt_positions={"510300.SH": 300, "600000.SH": 200},
        ledger_mode="attributed",
        allocated_cash=211_000.0,
        allocated_positions={"510300.SH": 100},
    ))

    assert result.ledger_mode == "attributed"
    assert result.virtual_cash == 211_000.0
    assert result.unallocated_cash == 18_938_000.0
    assert result.positions == {"510300.SH": 100}
    with sf() as session:
        state = session.get(InstanceState, "live_hydra")
        assert state.ledger_mode == "attributed"
        assert state.virtual_cash == 211_000.0
        assert state.virtual_positions == {"510300.SH": 100}
        assert state.strategy_state["initial_allocated_cash"] == 211_000.0
        assert state.strategy_state["external_positions_snapshot"] == {
            "510300.SH": 200,
            "600000.SH": 200,
        }


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"ledger_mode": "attributed"}, "必须显式提供"),
        ({
            "ledger_mode": "attributed",
            "allocated_cash": 700_001.0,
            "allocated_positions": {},
        }, "不能大于"),
        ({
            "ledger_mode": "attributed",
            "allocated_cash": 211_000.0,
            "allocated_positions": {"600000.SH": 1},
        }, "不在 owned_symbols"),
        ({
            "ledger_mode": "attributed",
            "allocated_cash": 211_000.0,
            "qmt_positions": {"510300.SH": 100},
            "allocated_positions": {"510300.SH": 101},
        }, "超过 QMT"),
    ],
)
def test_attributed_initialization_rejects_implicit_or_unfunded_allocation(
    changes, message,
):
    with pytest.raises(ValueError, match=message):
        _request(**changes)


def test_two_attributed_ledgers_can_share_a_symbol(tmp_path):
    service, sf = _service(tmp_path)
    with sf() as session:
        session.add(InstanceState(
            instance_id="other_live",
            execution_domain="live",
            account_alias="hydra-live",
            ledger_mode="attributed",
            virtual_cash=100_000.0,
            virtual_positions={"510300.SH": 100},
            owned_symbols=["510300.SH"],
            last_update="2026-09-03T00:00:00+08:00",
        ))
        session.commit()

    result = service.initialize(_request(
        qmt_positions={"510300.SH": 200},
        ledger_mode="attributed",
        allocated_cash=211_000.0,
        allocated_positions={"510300.SH": 100},
    ))

    assert result.positions == {"510300.SH": 100}


def test_attributed_initialization_rejects_double_allocation_across_strategies(
    tmp_path,
):
    service, sf = _service(tmp_path)
    with sf() as session:
        session.add(InstanceState(
            instance_id="other_live",
            execution_domain="live",
            account_alias="hydra-live",
            ledger_mode="attributed",
            virtual_cash=100_000.0,
            virtual_positions={"510300.SH": 100},
            owned_symbols=["510300.SH"],
            last_update="2026-09-03T00:00:00+08:00",
        ))
        session.commit()

    with pytest.raises(APIError, match="分配合计超过") as captured:
        service.initialize(_request(
            qmt_positions={"510300.SH": 100},
            ledger_mode="attributed",
            allocated_cash=211_000.0,
            allocated_positions={"510300.SH": 100},
        ))

    assert captured.value.http_status == 409
    with sf() as session:
        assert session.get(InstanceState, "live_hydra") is None
