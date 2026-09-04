"""Hydra target/rebalance/attempt 状态机与 residual retry。"""
from __future__ import annotations

import hashlib
import io

import pandas as pd
import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.exceptions import APIError
from app.models import HydraExecutionAttempt, HydraRebalance, HydraTarget, InstanceState, Order
from app.schemas.hydra_data import HydraDataManifest
from app.schemas.hydra_relay import (
    HydraAttemptCloseRequest,
    HydraRetryRequest,
    HydraTargetRequest,
    hydra_basket_hash,
)
from app.services.hydra_data import HydraDataStore
from app.services.hydra_relay import HydraRelayService, HydraRiskLimits

PUBLISHER = "1" * 40
SYMBOLS = {"510300.SH", "159915.SZ"}


def _bytes(frame: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    frame.to_parquet(output, index=False)
    return output.getvalue()


def _price_frame(as_of="20260731"):
    return pd.DataFrame([
        {
            "symbol": "510300.SH", "trade_date": as_of,
            "open": 4.0, "high": 4.1, "low": 3.9, "close": 4.0,
            "volume": 1000, "amount": 4000.0, "suspendFlag": 0,
        },
        {
            "symbol": "159915.SZ", "trade_date": as_of,
            "open": 2.0, "high": 2.1, "low": 1.9, "close": 2.0,
            "volume": 1000, "amount": 2000.0, "suspendFlag": 0,
        },
    ])


def _install(store, stream, frame, adjustment, as_of="20260731"):
    body = _bytes(frame)
    manifest = HydraDataManifest(
        stream=stream,
        source="mock_qmt",
        adjustment=adjustment,
        as_of_date=as_of,
        fetched_at=f"{as_of}T16:10:00+08:00",
        producer_commit="2" * 40,
        file_sha256=hashlib.sha256(body).hexdigest(),
        row_count=len(frame),
        symbol_count=frame["symbol"].nunique(),
    )
    return store.install(body, manifest).file_sha256


def _install_calendar(store):
    frame = pd.DataFrame({"trade_date": ["20260731", "20260803", "20260804"]})
    body = _bytes(frame)
    manifest = HydraDataManifest(
        stream="hydra_trading_calendar",
        source="mock_qmt",
        adjustment="calendar",
        as_of_date="20260731",
        fetched_at="2026-07-31T16:10:00+08:00",
        producer_commit="2" * 40,
        file_sha256=hashlib.sha256(body).hexdigest(),
        row_count=len(frame),
        symbol_count=0,
    )
    return store.install(body, manifest).file_sha256


def _setup(
    tmp_path, *, live_enabled=False, state_domain="paper", risk_mode="static",
):
    engine = make_engine(f"sqlite:///{tmp_path}/relay.db")
    init_db(engine)
    sf = make_session_factory(engine)
    store = HydraDataStore(tmp_path / "data")
    model_sha = _install(store, "hydra_model_hfq", _price_frame(), "back")
    raw_sha = _install(store, "hydra_execution_raw", _price_frame(), "none")
    actions = pd.DataFrame([{
        "symbol": "510300.SH", "event_date": "20260115", "event_type": "DIVIDEND",
        "cash_per_share": 0.01, "share_factor": 1.0, "source_event_id": "ca-1",
    }])
    actions_sha = _install(
        store, "hydra_corporate_actions", actions, "corporate_actions",
    )
    calendar_sha = _install_calendar(store)
    with sf() as session:
        session.add(InstanceState(
            instance_id=f"{state_domain}_hydra",
            execution_domain=state_domain,
            account_alias=f"hydra-{state_domain}",
            virtual_cash=1_000_000.0,
            virtual_positions={},
            strategy_state={"reconciliation_status": "ok"},
            last_update="2026-07-31T16:30:00+08:00",
        ))
        session.commit()
    service = HydraRelayService(
        sf,
        store,
        allowed_symbols=SYMBOLS,
        allowed_publisher_commits={PUBLISHER},
        live_enabled=live_enabled,
        live_limits=HydraRiskLimits(
            max_daily_orders=10,
            max_single_order_notional=(1_000_000 if risk_mode == "static" else 0),
            max_daily_buy_notional=(2_000_000 if risk_mode == "static" else 0),
            max_daily_sell_notional=(2_000_000 if risk_mode == "static" else 0),
            max_daily_turnover_notional=(3_000_000 if risk_mode == "static" else 0),
            max_price_offset_bps=(50 if risk_mode == "static" else 0),
            mode=risk_mode,
            auto_max_daily_orders=10,
            auto_buffer_bps=100,
        ),
    )
    return service, sf, store, model_sha, raw_sha, actions_sha, calendar_sha


def _target(model_sha, raw_sha, actions_sha, calendar_sha, **changes):
    payload = {
        "execution_domain": "paper",
        "account_alias": "hydra-paper",
        "instance_id": "paper_hydra",
        "strategy_version": "v48.1-RB@test",
        "publisher_source_commit": PUBLISHER,
        "decision_date": "20260731",
        "as_of_date": "20260731",
        "execution_date": "20260803",
        "research_input_hashes": {"weights": "9" * 64},
        "input_hashes": {
            "model_hfq": model_sha,
            "execution_raw": raw_sha,
            "corporate_actions": actions_sha,
            "trading_calendar": calendar_sha,
        },
        "weights": [
            {"code": "510300.SH", "weight": 0.6},
            {"code": "159915.SZ", "weight": 0.4},
        ],
        "cash_buffer_weight": 0.01,
    }
    payload.update(changes)
    payload["basket_sha256"] = hydra_basket_hash(payload)
    return HydraTargetRequest(**payload)


def test_stage_initial_is_content_idempotent_and_auditable(tmp_path):
    service, sf, _, model_sha, raw_sha, actions_sha, calendar_sha = _setup(tmp_path)
    req = _target(model_sha, raw_sha, actions_sha, calendar_sha)
    first = service.stage_initial(req)
    second = service.stage_initial(req)

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert first.batch_sha256 == second.batch_sha256
    assert first.order_count == 2
    with sf() as session:
        assert len(session.execute(select(HydraTarget)).scalars().all()) == 1
        assert len(session.execute(select(HydraRebalance)).scalars().all()) == 1
        assert len(session.execute(select(HydraExecutionAttempt)).scalars().all()) == 1
        orders = session.execute(select(Order).order_by(Order.symbol)).scalars().all()
        assert len(orders) == 2
        assert all(order.execution_domain == "paper" for order in orders)
        assert all(order.batch_sha256 == first.batch_sha256 for order in orders)
        assert all(order.execution_reference_price in {2.0, 4.0} for order in orders)
        # 买入限价按 0.001 tick 向下保守舍入。
        assert {order.limit_price for order in orders} == {2.01, 4.02}


def test_live_auto_risk_allows_zero_static_caps_and_persists_nav_snapshot(tmp_path):
    service, sf, _, model_sha, raw_sha, actions_sha, calendar_sha = _setup(
        tmp_path, live_enabled=True, state_domain="live", risk_mode="auto",
    )
    req = _target(
        model_sha,
        raw_sha,
        actions_sha,
        calendar_sha,
        execution_domain="live",
        account_alias="hydra-live",
        instance_id="live_hydra",
        cash_buffer_weight=0.0,
    )
    response = service.stage_initial(req)
    with sf() as session:
        attempt = session.get(HydraExecutionAttempt, response.attempt_id)
        assert attempt.risk_snapshot["mode"] == "auto"
        assert attempt.risk_snapshot["nav"] == 1_000_000
        assert attempt.risk_snapshot["max_single_order_notional"] == 1_010_000
        assert attempt.risk_snapshot["max_price_offset_bps"] == 50


def test_stage_rejects_research_bridge_from_execution_raw(tmp_path):
    service, _, store, model_sha, _, actions_sha, calendar_sha = _setup(tmp_path)
    raw = pd.concat([
        _price_frame(),
        pd.DataFrame([{
            "symbol": "511010.SH", "trade_date": "20260731",
            "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0,
            "volume": 1000, "amount": 100_000.0, "suspendFlag": 0,
        }]),
    ], ignore_index=True)
    raw_sha = _install(store, "hydra_execution_raw", raw, "none")
    with pytest.raises(APIError, match="execution_raw 与 live 白名单不一致"):
        service.stage_initial(_target(
            model_sha, raw_sha, actions_sha, calendar_sha,
        ))


def test_retry_reuses_target_shares_and_only_orders_residual(tmp_path):
    service, sf, store, model_sha, raw_sha, actions_sha, calendar_sha = _setup(tmp_path)
    first = service.stage_initial(_target(
        model_sha, raw_sha, actions_sha, calendar_sha,
    ))
    with sf() as session:
        initial_orders = session.execute(
            select(Order).where(Order.rebalance_id == first.rebalance_id)
        ).scalars().all()
        target_positions = {
            order.symbol: order.quantity for order in initial_orders
        }
        for order in initial_orders:
            order.status = "CANCELLED"
        session.commit()

    retry_frame = _price_frame(as_of="20260803")
    retry_row = retry_frame["symbol"] == "510300.SH"
    retry_frame.loc[retry_row, ["open", "high", "low", "close"]] = 2.5
    retry_frame.loc[retry_row, "amount"] = 2500.0
    retry_raw_sha = _install(
        store, "hydra_execution_raw", retry_frame, "none", as_of="20260803",
    )
    partial_positions = dict(target_positions)
    partial_positions["510300.SH"] -= 100
    with sf() as session:
        state = session.get(InstanceState, "paper_hydra")
        state.virtual_cash = 100_000
        state.virtual_positions = partial_positions
        session.commit()
    closed = service.close_attempt(HydraAttemptCloseRequest(
        execution_domain="paper",
        account_alias="hydra-paper",
        attempt_id=first.attempt_id,
        actual_cash=100_000,
        actual_positions=partial_positions,
        reconciliation_evidence_sha256="4" * 64,
    ))
    assert closed.status == "RESIDUAL"
    assert closed.residual_after == {"510300.SH": 100}
    retry = service.stage_retry(HydraRetryRequest(
        execution_domain="paper",
        account_alias="hydra-paper",
        rebalance_id=first.rebalance_id,
        trade_date="20260804",
        execution_raw_sha256=retry_raw_sha,
        actual_cash=100_000,
        actual_positions=partial_positions,
        reconciliation_evidence_sha256="4" * 64,
    ))
    assert retry.attempt_id != first.attempt_id
    assert retry.order_count == 1
    with sf() as session:
        orders = session.execute(
            select(Order).where(Order.attempt_id == retry.attempt_id)
        ).scalars().all()
        assert [(order.symbol, order.direction, order.quantity) for order in orders] == [
            ("510300.SH", "BUY", 100),
        ]
        # The client supplies neither the residual nor its prices.  The server
        # recomputes the delta and prices it from the approved frozen raw stream.
        assert orders[0].execution_reference_price == 2.5
        assert orders[0].limit_price == 2.512


def test_retry_blocked_while_prior_attempt_is_pending(tmp_path):
    service, _, store, model_sha, raw_sha, actions_sha, calendar_sha = _setup(tmp_path)
    first = service.stage_initial(_target(
        model_sha, raw_sha, actions_sha, calendar_sha,
    ))
    retry_raw_sha = _install(
        store, "hydra_execution_raw", _price_frame("20260803"), "none", "20260803",
    )
    with pytest.raises(APIError, match="未决订单"):
        service.stage_retry(HydraRetryRequest(
            execution_domain="paper",
            account_alias="hydra-paper",
            rebalance_id=first.rebalance_id,
            trade_date="20260804",
            execution_raw_sha256=retry_raw_sha,
            actual_cash=100_000,
            actual_positions={},
            reconciliation_evidence_sha256="4" * 64,
        ))


def test_live_stage_is_blocked_when_generation_switch_is_off(tmp_path):
    service, _, _, model_sha, raw_sha, actions_sha, calendar_sha = _setup(
        tmp_path, live_enabled=False, state_domain="live",
    )
    req = _target(
        model_sha,
        raw_sha,
        actions_sha,
        calendar_sha,
        execution_domain="live",
        account_alias="hydra-live",
        instance_id="live_hydra",
    )
    with pytest.raises(APIError) as captured:
        service.stage_initial(req)
    assert captured.value.http_status == 423


def test_close_attempt_requires_virtual_ledger_to_match_qmt(tmp_path):
    service, sf, _, model_sha, raw_sha, actions_sha, calendar_sha = _setup(tmp_path)
    first = service.stage_initial(_target(
        model_sha, raw_sha, actions_sha, calendar_sha,
    ))
    with sf() as session:
        for order in session.execute(select(Order)).scalars().all():
            order.status = "CANCELLED"
        session.commit()
    with pytest.raises(APIError, match="持仓与虚拟账本不一致"):
        service.close_attempt(HydraAttemptCloseRequest(
            execution_domain="paper",
            account_alias="hydra-paper",
            attempt_id=first.attempt_id,
            actual_cash=1_000_000,
            actual_positions={"510300.SH": 100},
            reconciliation_evidence_sha256="5" * 64,
        ))


def test_attributed_hydra_uses_only_managed_capital_across_stage_close_and_retry(
    tmp_path,
):
    service, sf, store, model_sha, raw_sha, actions_sha, calendar_sha = _setup(
        tmp_path, live_enabled=True, state_domain="live", risk_mode="auto",
    )
    with sf() as session:
        state = session.get(InstanceState, "live_hydra")
        state.ledger_mode = "attributed"
        state.virtual_cash = 211_000.0
        state.owned_symbols = sorted(SYMBOLS)
        session.commit()

    first = service.stage_initial(_target(
        model_sha, raw_sha, actions_sha, calendar_sha,
        execution_domain="live",
        account_alias="hydra-live",
        instance_id="live_hydra",
    ))
    with sf() as session:
        attempt = session.get(HydraExecutionAttempt, first.attempt_id)
        assert attempt.risk_snapshot["nav"] == 211_000.0
        initial_orders = session.execute(
            select(Order).where(Order.attempt_id == first.attempt_id)
        ).scalars().all()
        assert sum(
            order.quantity * order.limit_price for order in initial_orders
        ) < 211_000.0
        for order in initial_orders:
            order.status = "CANCELLED"
        session.commit()

    closed = service.close_attempt(HydraAttemptCloseRequest(
        execution_domain="live",
        account_alias="hydra-live",
        attempt_id=first.attempt_id,
        actual_cash=19_149_000.0,
        actual_positions={"600000.SH": 200},
        reconciliation_evidence_sha256="5" * 64,
    ))
    assert closed.status == "RESIDUAL"
    with sf() as session:
        attempt = session.get(HydraExecutionAttempt, first.attempt_id)
        assert attempt.reconciled_cash == 211_000.0
        assert attempt.reconciled_positions == {}

    retry_raw_sha = _install(
        store, "hydra_execution_raw", _price_frame("20260803"), "none", "20260803",
    )
    retry = service.stage_retry(HydraRetryRequest(
        execution_domain="live",
        account_alias="hydra-live",
        rebalance_id=first.rebalance_id,
        trade_date="20260804",
        execution_raw_sha256=retry_raw_sha,
        actual_cash=19_149_000.0,
        actual_positions={"600000.SH": 200},
        reconciliation_evidence_sha256="6" * 64,
    ))
    with sf() as session:
        attempt = session.get(HydraExecutionAttempt, retry.attempt_id)
        assert attempt.risk_snapshot["nav"] == 211_000.0


def test_target_rejects_unapproved_publisher(tmp_path):
    service, _, _, model_sha, raw_sha, actions_sha, calendar_sha = _setup(tmp_path)
    req = _target(
        model_sha,
        raw_sha,
        actions_sha,
        calendar_sha,
        publisher_source_commit="9" * 40,
    )
    with pytest.raises(APIError, match="未获批准"):
        service.stage_initial(req)


def test_target_execution_date_must_be_next_trading_day(tmp_path):
    service, _, _, model_sha, raw_sha, actions_sha, calendar_sha = _setup(tmp_path)
    req = _target(
        model_sha,
        raw_sha,
        actions_sha,
        calendar_sha,
        execution_date="20260804",
    )
    with pytest.raises(APIError, match="第一个交易日"):
        service.stage_initial(req)
