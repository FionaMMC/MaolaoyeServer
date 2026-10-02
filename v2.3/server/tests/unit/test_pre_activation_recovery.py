from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.models import HydraTarget, InstanceState, Order, Trade
from scripts.recover_hydra_pre_activation_baseline import rebase


def _setup(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/recovery.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as session:
        session.add(InstanceState(
            instance_id="live_hydra",
            execution_domain="live",
            account_alias="hydra-live",
            virtual_cash=72_057.27,
            virtual_positions={},
            owned_symbols=["510300.SH", "511260.SH"],
            strategy_state={
                "reconciliation_status": "ok",
                "initialization_evidence_sha256": "a" * 64,
            },
            last_update="2026-09-02T19:50:15+08:00",
        ))
        session.add_all([
            Order(
                order_id="canary-pending", execution_domain="live",
                qmt_account_alias="hydra-live", target_id="server_canary:old",
                account_group="hydra-live", symbol="510300.SH", direction="BUY",
                quantity=100, limit_price=4.62, valid_date="20260831",
                status="PENDING", created_at="2026-08-29T10:00:00+08:00",
            ),
            Order(
                order_id="canary-filled", execution_domain="live",
                qmt_account_alias="hydra-live", target_id="server_canary:new",
                account_group="hydra-live", symbol="510300.SH", direction="BUY",
                quantity=100, limit_price=4.643, valid_date="20260903",
                status="FILLED", created_at="2026-09-02T19:51:37+08:00",
            ),
        ])
        session.add_all([
            Trade(
                order_id="canary-filled", execution_domain="live",
                filled_quantity=100, filled_price=4.638, filled_time=None,
                status="FILLED", received_at="2026-09-03T10:45:26+08:00",
            ),
            Trade(
                order_id="canary-filled", execution_domain="live",
                filled_quantity=100, filled_price=4.638,
                filled_time="2026-09-03T09:33:49+08:00", status="FILLED",
                received_at="2026-09-03T17:45:05+08:00",
            ),
        ])
        session.commit()
    return sf


def _snapshot():
    return {
        "instance_id": "live_hydra",
        "account_alias": "hydra-live",
        "qmt_cash": 210_589.85,
        "qmt_total_asset": 211_051.95,
        "qmt_positions": {
            "510300.SH": 100,
            "920268.BJ": 100,
            "920269.BJ": 200,
        },
        "snapshot_time": "2026-09-03T19:25:00+08:00",
    }


def test_rebase_uses_qmt_as_opening_truth_and_records_canaries(tmp_path):
    sf = _setup(tmp_path)
    preview = rebase(sf, _snapshot(), "c" * 64, apply=False)
    assert preview["recovered_positions"] == {"510300.SH": 100}
    assert preview["external_positions"] == {"920268.BJ": 100, "920269.BJ": 200}
    assert [row["status"] for row in preview["observed_canary_orders"]] == [
        "PENDING", "FILLED",
    ]

    result = rebase(sf, _snapshot(), "c" * 64, apply=True)
    assert result["idempotent_replay"] is False
    with sf() as session:
        state = session.get(InstanceState, "live_hydra")
        assert state.virtual_cash == 210_589.85
        assert state.virtual_positions == {"510300.SH": 100}
        assert state.strategy_state["opening_baseline_rebase"]["previous_cash"] == 72_057.27

    assert rebase(sf, _snapshot(), "c" * 64, apply=True)["idempotent_replay"] is True


def test_rebase_checks_instance_account(tmp_path):
    sf = _setup(tmp_path)
    bad = {**_snapshot(), "account_alias": "wrong"}
    with pytest.raises(RuntimeError, match="账户不一致"):
        rebase(sf, bad, "c" * 64, apply=False)


def test_rebase_stops_if_formal_target_already_exists(tmp_path):
    sf = _setup(tmp_path)
    with sf() as session:
        session.add(HydraTarget(
            target_id="target-1", execution_domain="live",
            account_alias="hydra-live", strategy_version="v48.1-RB",
            publisher_source_commit="d" * 40, decision_date="20260903",
            as_of_date="20260903", execution_date="20260904",
            basket_sha256="e" * 64, research_input_hashes={}, input_hashes={},
            weights={"510300.SH": 1.0}, cash_buffer_weight=0.0,
            status="STAGED", created_at="2026-09-03T20:00:00+08:00",
        ))
        session.commit()
    with pytest.raises(RuntimeError, match="正式 Hydra target"):
        rebase(sf, _snapshot(), "c" * 64, apply=False)
