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
