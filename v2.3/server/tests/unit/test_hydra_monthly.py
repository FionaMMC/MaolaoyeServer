"""Monthly research inbox, date boundary and at-most-once plan publication."""

import base64
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.db import init_db, make_engine, make_session_factory
from app.exceptions import APIError
from app.models import HydraMonthlyCycle, HydraExecutionPlan, InstanceState, Order
from app.schemas.hydra_monthly import HydraMonthlySnapshotRequest
from app.services.hydra_data import HydraDataStore
from app.services.hydra_relay import HydraRelayService, HydraRiskLimits
from app.services.hydra_monthly import (
    receive_snapshot,
    publish_monthly_plan,
    month_end_next,
    SYMBOLS,
    RESEARCH_COMMIT,
)
from live_client.data_snapshot import _write_bundle

NOW = datetime.fromisoformat("2026-10-01T16:00:00+08:00")


@pytest.fixture
def monthly(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/monthly.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as session:
        session.add(
            InstanceState(
                instance_id="hydra",
                account_alias="live-test",
                execution_domain="live",
                ledger_mode="attributed",
                virtual_cash=100000,
                virtual_positions={},
                last_update="before",
                strategy_state={"reconciliation_status": "attributed_ledger"},
            )
        )
        session.commit()
    relay = HydraRelayService(
        sf,
        HydraDataStore(tmp_path / "store"),
        allowed_symbols=set(SYMBOLS),
        allowed_publisher_commits={RESEARCH_COMMIT},
        live_enabled=True,
        live_limits=HydraRiskLimits(
            mode="auto",
            auto_max_daily_orders=100,
            auto_buffer_bps=100,
            max_daily_orders=0,
            max_single_order_notional=0,
            max_daily_buy_notional=0,
            max_daily_sell_notional=0,
            max_daily_turnover_notional=0,
            max_price_offset_bps=50,
        ),
    )
    settings = SimpleNamespace(
        hydra_monthly_enabled=True,
        hydra_monthly_instance_id="hydra",
        hydra_monthly_account_alias="live-test",
        hydra_monthly_start_date="20260918",
    )
    frames = {}
    for key, symbols in (("model_hfq", SYMBOLS | {"511010.SH"}), ("execution_raw", SYMBOLS)):
        frames[key] = pd.DataFrame(
            [
                dict(
                    symbol=s,
                    trade_date="20260930",
                    open=2.0,
                    close=2.0,
                    high=2.0,
                    low=2.0,
                    volume=1000.0,
                    amount=2000.0,
                    suspendFlag=0,
                )
                for s in sorted(symbols)
            ]
        )
    frames["corporate_actions"] = pd.DataFrame(
        columns=[
            "symbol",
            "event_date",
            "event_type",
            "cash_per_share",
            "share_factor",
            "source_event_id",
        ]
    )
    frames["trading_calendar"] = pd.DataFrame(
        {"trade_date": ["20260929", "20260930", "20261008", "20261009"]}
    )
    streams = {}
    for key, frame in frames.items():
        adjustment = {
            "model_hfq": "back",
            "execution_raw": "none",
            "corporate_actions": "corporate_actions",
            "trading_calendar": "calendar",
        }[key]
        path = tmp_path / "input" / key
        manifest = _write_bundle(
            frame,
            path,
            stream="hydra_" + key,
            adjustment=adjustment,
            as_of_date="20260930",
            producer_commit="a" * 40,
            executable_symbols=sorted(SYMBOLS) if key in {"model_hfq", "execution_raw"} else None,
            research_only_symbols=["511010.SH"] if key == "model_hfq" else None,
        )
        streams[key] = dict(
            manifest=manifest,
            parquet_base64=base64.b64encode((path / "data.parquet").read_bytes()).decode(),
        )
    req = HydraMonthlySnapshotRequest(
        account_alias="live-test", instance_id="hydra", as_of_date="20260930", streams=streams
    )
    yield relay, settings, req
    engine.dispose()


def computed(req):
    return dict(
        research_commit=RESEARCH_COMMIT,
        as_of_date=req.as_of_date,
        input_hashes={key: item.manifest.file_sha256 for key, item in req.streams.items()},
        weights={s: 1 / 9 for s in SYMBOLS},
    )


def test_monthly_receive_then_plan_is_idempotent_and_never_changes_money(monthly):
    relay, settings, req = monthly
    first = receive_snapshot(relay, settings, req, now=NOW)
    assert not first["already_stored"]
    assert receive_snapshot(relay, settings, req, now=NOW)["already_stored"]
    result = publish_monthly_plan(relay, first["cycle_id"], computed(req))
    assert result["status"] == "MONTHLY_PLAN_READY"
    assert result == publish_monthly_plan(relay, first["cycle_id"], computed(req))
    with relay.session_factory() as session:
        assert session.query(Order).count() == 0
        assert (
            session.query(HydraMonthlyCycle).count()
            == session.query(HydraExecutionPlan).count()
            == 1
        )
        state = session.get(InstanceState, "hydra")
        assert (
            state.virtual_cash == 100000
            and state.virtual_positions == {}
            and state.last_update == "before"
        )


@pytest.mark.parametrize(
    "now,patch",
    [
        ("2026-09-30T14:59:00+08:00", {}),
        ("2026-09-29T16:00:00+08:00", {}),
        (NOW.isoformat(), {"account_alias": "other"}),
        (NOW.isoformat(), {"instance_id": "other"}),
    ],
)
def test_wrong_scope_and_unfinished_date_rejected(monthly, now, patch):
    relay, settings, req = monthly
    with pytest.raises(APIError):
        receive_snapshot(
            relay, settings, req.model_copy(update=patch), now=datetime.fromisoformat(now)
        )
    with relay.session_factory() as session:
        assert session.query(HydraMonthlyCycle).count() == 0


def test_bad_bytes_never_register_a_cycle(monthly):
    relay, settings, req = monthly
    req.streams["model_hfq"].parquet_base64 = base64.b64encode(b"broken").decode()
    with pytest.raises(ValueError, match="摘要"):
        receive_snapshot(relay, settings, req, now=NOW)
    with relay.session_factory() as session:
        assert session.query(HydraMonthlyCycle).count() == 0


def test_calendar_has_to_prove_month_end():
    with pytest.raises(ValueError, match="仅月末"):
        month_end_next(
            pd.DataFrame({"trade_date": ["20260929", "20260930", "20261008"]}), "20260929"
        )
    with pytest.raises(ValueError, match="缺下一"):
        month_end_next(pd.DataFrame({"trade_date": ["20260930"]}), "20260930")


def test_failure_before_publication_can_retry_same_cycle(monthly):
    relay, settings, req = monthly
    receipt = receive_snapshot(relay, settings, req, now=NOW)
    bad = computed(req) | {"research_commit": "f" * 40}
    with pytest.raises(APIError):
        publish_monthly_plan(relay, receipt["cycle_id"], bad)
    with relay.session_factory() as session:
        assert session.query(HydraExecutionPlan).count() == 0
        assert session.get(HydraMonthlyCycle, receipt["cycle_id"]).status == "RECEIVED"
    assert (
        publish_monthly_plan(relay, receipt["cycle_id"], computed(req))["status"]
        == "MONTHLY_PLAN_READY"
    )


def test_evening_advance_uses_fresh_prices_and_snapshot_not_monthly_price(monthly):
    from app.schemas.hydra_relay import HydraAdvanceRequest, HydraExecutionPublishRequest
    from app.services.hydra_execution_publish import publish_execution
    from app.services.hydra_execution_advance import advance_execution

    relay, settings, req = monthly
    receipt = receive_snapshot(relay, settings, req, now=NOW)
    bars = [
        dict(
            symbol=s,
            trade_date="20261008",
            open=3.0,
            high=3.0,
            low=3.0,
            close=3.0,
            volume=1000.0,
            amount=3000.0,
            suspendFlag=0,
        )
        for s in sorted(SYMBOLS)
    ]
    publish_execution(
        relay,
        HydraExecutionPublishRequest(
            execution_domain="live",
            account_alias="live-test",
            reference_date="20261008",
            producer_commit="a" * 40,
            bars=bars,
            calendar_dates=["20261008", "20261009"],
        ),
    )
    advance = HydraAdvanceRequest(
        execution_domain="live",
        account_alias="live-test",
        instance_id="hydra",
        reference_date="20261008",
        actual_cash=100000,
        actual_positions={},
        reconciliation_evidence_sha256="b" * 64,
    )
    assert advance_execution(relay, advance)["status"] == "WAITING_EXECUTION_DATA"
    publish_monthly_plan(relay, receipt["cycle_id"], computed(req))
    first = advance_execution(relay, advance)
    assert first["status"] == "EXECUTION_ADVANCED"
    second = advance_execution(relay, advance)
    assert first["results"][0]["attempt_id"] == second["results"][0]["attempt_id"]
    with relay.session_factory() as session:
        orders = session.query(Order).all()
        assert len(orders) == 9
        assert all(row.execution_reference_price == 3 for row in orders)
        assert all(row.valid_date == "20261009" for row in orders)
        assert session.get(InstanceState, "hydra").virtual_cash == 100000


def test_snapshot_api_is_account_scoped(monthly, settings_for_test):
    from app.main import create_app
    from app.dependencies import get_hydra_relay_service

    relay, settings, req = monthly
    appsettings = settings_for_test.model_copy(
        update={
            "live_api_key": "MONTHLY_TEST_KEY",
            "live_client_id": "monthly-test",
            "live_account_aliases_csv": "live-test",
            **vars(settings),
        }
    )
    app = create_app(settings_override=appsettings)
    app.dependency_overrides[get_hydra_relay_service] = lambda: relay
    client = TestClient(app)
    # Deliberately malformed scope: authentication must happen before installing.
    response = client.post(
        "/hydra/research/snapshots",
        json=req.model_copy(update={"account_alias": "other"}).model_dump(mode="json"),
        headers={"Authorization": "Bearer MONTHLY_TEST_KEY"},
    )
    assert response.status_code == 403


def test_worker_failure_is_retryable_without_mutating_existing_ledger(monthly, monkeypatch):
    import json
    from pathlib import Path
    from scripts.run_hydra_monthly import run_worker

    relay, settings, req = monthly
    settings.hydra_monthly_research_dir = Path("/unused-test-research")
    receipt = receive_snapshot(relay, settings, req, now=NOW)

    def broken(*args, **kwargs):
        raise TimeoutError("research timeout")

    monkeypatch.setattr("scripts.run_hydra_monthly.subprocess.run", broken)
    assert (
        run_worker(relay, settings, apply=True)["results"][0]["status"] == "WAITING_RESEARCH_REVIEW"
    )
    with relay.session_factory() as session:
        assert session.get(HydraMonthlyCycle, receipt["cycle_id"]).status == "RECEIVED"
        assert session.query(Order).count() == 0

    def good(args, **kwargs):
        Path(args[args.index("--output") + 1]).write_text(json.dumps(computed(req)))

    monkeypatch.setattr("scripts.run_hydra_monthly.subprocess.run", good)
    assert run_worker(relay, settings)["results"][0]["status"] == "DRY_RUN_COMPUTED"
    with relay.session_factory() as session:
        assert session.query(HydraExecutionPlan).count() == 0
    assert run_worker(relay, settings, apply=True)["results"][0]["status"] == "MONTHLY_PLAN_READY"
    assert run_worker(relay, settings, apply=True)["status"] == "IDLE_NO_NEW_MONTHLY_INPUT"
