from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState
from app.storage.parquet import ParquetStore
from scripts import run_v713_stock_valuation as job

NOW = datetime.fromisoformat("2026-10-28T18:20:00+08:00")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    sf = make_session_factory(engine)
    store = ParquetStore(tmp_path / "data")
    store.append("indexes", "000852.SH", pd.DataFrame({"trade_date": [20261028], "close": [100.]}))
    evidence = tmp_path / "raw-price-evidence.json"
    monkeypatch.setattr("app.settings.get_settings", lambda: SimpleNamespace(
        db_url=f"sqlite:///{db}", parquet_root=store.root))
    yield sf, db, store, evidence
    engine.dispose()


def seed(sf, positions=None, metadata=None, cash=200_000):
    with sf() as session:
        session.add(InstanceState(instance_id=job.INSTANCE, execution_domain="live", account_alias="shared",
                                  ledger_mode="attributed", virtual_cash=cash, virtual_positions=positions or {},
                                  strategy_state=metadata or {}, last_update="2026-10-28T16:00:00+08:00"))
        session.commit()


def contents(path):
    with sqlite3.connect(path) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
                for name in names}


def run(environment, capsys):
    assert job.main(["--raw-price-evidence", str(environment[3])], now=NOW) == 0
    return json.loads(capsys.readouterr().out.strip())


def test_missing_instance_has_no_database_writes(environment, capsys):
    before = contents(environment[1])
    result = run(environment, capsys)
    assert result["status"] == "WAITING_INSTANCE" and result["written"] == 0
    assert contents(environment[1]) == before


def test_zero_resource_instance_waits_for_capital_without_writes(environment, capsys):
    seed(environment[0], cash=0)
    before = contents(environment[1])
    result = run(environment, capsys)
    assert result["status"] == "WAITING_CAPITAL" and result["written"] == 0
    assert contents(environment[1]) == before


@pytest.mark.parametrize("evidence_kind", ["missing", "incomplete", "adjusted", "bad_hash"])
def test_stock_holdings_require_actual_raw_file_evidence(environment, capsys, evidence_kind):
    sf, db, store, evidence = environment
    seed(sf, {"600001.SH": 100}, {"valuation_price_evidence": {"600001.SH": {"source": "must-not-fallback"}}})
    store.append("stocks", "600001.SH", pd.DataFrame({"trade_date": [20261028], "close": [11.]}))
    path = store._file("stocks", "600001.SH")
    proof = {"category": "stocks", "source": "test-qmt-raw", "adjustment": "none",
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if evidence_kind == "adjusted":
        proof["adjustment"] = "front"
    if evidence_kind == "bad_hash":
        proof["sha256"] = "0" * 64
    if evidence_kind != "missing":
        evidence.write_text(json.dumps({job.INSTANCE: {} if evidence_kind == "incomplete" else {"600001.SH": proof}}))
    before = contents(db)
    result = run(environment, capsys)
    assert result["status"] == "WAITING_RAW_PRICE_EVIDENCE" and result["written"] == 0
    assert contents(db) == before


@pytest.mark.parametrize("with_stock", [False, True])
def test_success_changes_only_the_three_derived_tables(environment, capsys, with_stock):
    sf, db, store, evidence = environment
    seed(sf, {"600001.SH": 100} if with_stock else {})
    if with_stock:
        store.append("execution_raw", "600001.SH", pd.DataFrame({"trade_date": [20261028], "close": [11.]}))
        path = store._file("execution_raw", "600001.SH")
        evidence.write_text(json.dumps({job.INSTANCE: {"600001.SH": {
            "category": "execution_raw", "source": "test-qmt-raw", "adjustment": "none",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}))
    before = contents(db)
    result = run(environment, capsys)
    after = contents(db)
    assert result["status"] == "VALUED" and result["dates"] == ["20261028"]
    assert {table for table in before if before[table] != after[table]} == job.PERFORMANCE_TABLES
    assert len(after["perf_snapshots"]) == len(after["perf_valuations"]) == len(after["daily_risk_snapshots"]) == 1


def test_connection_enforces_no_account_or_order_writes(environment):
    sf, db, _, _ = environment
    seed(sf)
    engine, _ = job.performance_sessions(f"sqlite:///{db}")
    try:
        for statement in ("UPDATE instance_state SET virtual_cash=0", "DELETE FROM orders",
                          "CREATE TABLE forbidden(id INTEGER)"):
            with engine.connect() as connection:
                with pytest.raises(DatabaseError, match="not authorized"):
                    connection.execute(text(statement))
    finally:
        engine.dispose()


def test_cash_only_observation_cannot_fall_back_to_metadata_after_a_holdings_race(environment, monkeypatch):
    seed(environment[0])
    captured = []
    monkeypatch.setattr(job, "materialize_live_performance", lambda *args, **kwargs:
                        captured.append(kwargs["raw_price_evidence"]) or {"status": "TEST"})
    job.value_stock(environment[0], environment[2], environment[3], now=NOW)
    assert captured[0] and captured[0].get("600001.SH") is None


def test_stock_job_runs_before_report_with_fixed_release_paths():
    deploy = Path(__file__).resolve().parents[2] / "deploy"
    service = (deploy / "qmt-v713-stock-valuation.service.in").read_text()
    timer = (deploy / "qmt-v713-stock-valuation.timer").read_text()
    report_service = (deploy / "qmt-v713-synthetic.service.in").read_text()
    assert "python @V713_RELEASE_SERVER@/scripts/run_v713_stock_valuation.py" in service
    assert "Before=qmt-v713-synthetic.service" in service
    assert "After=qmt-live-performance.service qmt-v713-stock-valuation.service" in report_service
    assert "18:20:00 Asia/Shanghai" in timer and "20:50:00 Asia/Shanghai" in timer
