"""Daily EOD risk snapshot materialization and benchmark joins."""
from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import func, select

from app.db import init_db, make_engine, make_session_factory
from app.models import (
    CashFlowJournal,
    DailyRiskSnapshot,
    PerfSnapshot,
    ShadowNavSnapshot,
)
from app.services.daily_risk import DailyRiskSnapshotService
from app.storage.parquet import ParquetStore


def _service(settings):
    engine = make_engine(settings.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    store = ParquetStore(settings.parquet_root)
    return DailyRiskSnapshotService(sf, store), sf, store


def _bars(rows):
    return pd.DataFrame([
        {"trade_date": date, "open": close, "high": close, "low": close,
         "close": close, "volume": 1.0, "amount": close}
        for date, close in rows
    ])


def _seed_regular(sf, store):
    store.append("stocks", "AAA.SH", _bars([(20260430, 100.0), (20260501, 110.0)]))
    store.append("stocks", "BBB.SH", _bars([(20260430, 50.0)]))
    store.append("indexes", "000852.SH", _bars([(20260430, 100.0), (20260501, 101.0)]))
    with sf() as session:
        session.add_all([
            PerfSnapshot(
                instance_id="paper_alpha", date="20260430", nav=10_000.0,
                daily_return=None, positions_snapshot={"AAA.SH": 10, "BBB.SH": -5},
            ),
            PerfSnapshot(
                instance_id="paper_alpha", date="20260501", nav=11_000.0,
                daily_return=0.1, positions_snapshot={"AAA.SH": 10, "BBB.SH": -5},
            ),
            CashFlowJournal(
                execution_domain="paper", account_alias="paper", instance_id="paper_alpha",
                event_date="20260501", event_type="DEPOSIT", amount=500.0,
                currency="CNY", source="test", source_event_id="deposit-1",
                evidence_sha256="a" * 64, description=None, status="APPLIED",
                created_at="2026-05-01T16:00:00+08:00",
                applied_at="2026-05-01T16:00:00+08:00",
            ),
        ])
        session.commit()


def test_daily_risk_rebuild_is_idempotent_and_cash_flow_adjusted(settings_for_test):
    service, sf, store = _service(settings_for_test)
    _seed_regular(sf, store)

    first = service.rebuild(instance_id="paper_alpha")
    second = service.rebuild(instance_id="paper_alpha")
    assert first["written"] == second["written"] == 2

    with sf() as session:
        assert session.scalar(select(func.count()).select_from(DailyRiskSnapshot)) == 2
        latest = session.get(DailyRiskSnapshot, ("paper_alpha", "20260501"))
        assert latest.long_market_value == pytest.approx(1_100.0)
        assert latest.short_market_value == pytest.approx(250.0)
        assert latest.gross_market_value == pytest.approx(1_350.0)
        assert latest.net_market_value == pytest.approx(850.0)
        assert latest.cash == pytest.approx(10_150.0)
        assert latest.gross_exposure == pytest.approx(1_350 / 11_000)
        assert latest.net_exposure == pytest.approx(850 / 11_000)
        assert latest.stale_mark_count == 1
        assert latest.missing_mark_count == 0
        assert latest.pricing_coverage == 1.0
        assert latest.external_cash_flow == 500.0
        assert latest.cash_flow_status == "observed"
        assert latest.portfolio_return == pytest.approx(0.05)

    result = service.query("paper_alpha", benchmark_symbol="000852.SH")
    assert result["count"] == 2
    assert result["benchmark"]["available"] is True
    assert result["items"][1]["benchmark_return"] == pytest.approx(0.01)
    assert result["items"][1]["portfolio_cumulative_return"] == pytest.approx(0.05)
    assert result["items"][1]["excess_cumulative_return"] == pytest.approx(0.04)
    assert result["summary"]["portfolio_return"] == pytest.approx(0.05)
    assert result["summary"]["benchmark_return"] == pytest.approx(0.01)
    assert result["latest_positions"][0]["symbol"] == "AAA.SH"


def test_daily_risk_uses_shadow_cash_snapshot(settings_for_test):
    service, sf, store = _service(settings_for_test)
    store.append("etfs", "510300.SH", _bars([(20260501, 4.0)]))
    with sf() as session:
        session.add(ShadowNavSnapshot(
            shadow_id="Shadow_Test", date="20260501", nav=1_000.0,
            daily_return=None, virtual_cash=600.0,
            positions_snapshot={"510300.SH": 100}, transaction_cost=0.0,
            turnover=0.0, decision_date=None, as_of_date=None,
            state_reason=None, source_version=None, input_hash=None,
            target_hash=None, created_at="2026-05-01T16:00:00+08:00",
        ))
        session.commit()

    service.rebuild(instance_id="Shadow_Test")
    with sf() as session:
        row = session.get(DailyRiskSnapshot, ("Shadow_Test", "20260501"))
        assert row.cash == 600.0
        assert row.cash_source == "snapshot"
        assert row.cash_flow_status == "not_applicable_shadow"
        assert row.gross_exposure == pytest.approx(0.4)


def test_daily_risk_api(client, settings_for_test):
    service, sf, store = _service(settings_for_test)
    _seed_regular(sf, store)
    service.rebuild(instance_id="paper_alpha")

    response = client.get(
        "/admin/metrics/daily-risk?instance_id=paper_alpha&period=all"
        "&benchmark_symbol=000852.SH",
        headers={"Authorization": "Bearer TEST_KEY"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 2
    assert data["summary"]["latest"]["pricing_coverage"] == 1.0
