"""The scheduled report reads existing evidence and cannot write the live database."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, PerfSnapshot, PerfValuation
from app.oms.models import V713StateRecord
from scripts import run_v713_synthetic_eod as job

POLICY = Path(__file__).resolve().parents[2] / "plugins/v713/synthetic_policy_v2.json"
NOW = datetime.fromisoformat("2026-10-28T18:25:00+08:00")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    sf = make_session_factory(engine)
    output, calendar = tmp_path / "report.json", tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"source": "test-exchange-evidence", "coverage_start": "20261001",
                                    "coverage_end": "20261105",
                                    "trading_dates": ["20261027", "20261028", "20261029", "20261030", "20261102"]}))
    monkeypatch.setattr("app.settings.get_settings", lambda: SimpleNamespace(
        db_url=f"sqlite:///{db}", v713_synthetic_output=output))
    yield sf, db, output, calendar
    engine.dispose()


def seed(sf, *, state=True, nav=True):
    with sf() as session:
        for instance in ("live_v713_stockleg", "live_hydra_v481_rb"):
            session.add(InstanceState(instance_id=instance, execution_domain="live", account_alias="shared",
                                      ledger_mode="attributed", virtual_cash=200_000, virtual_positions={},
                                      last_update="2026-10-28T16:00:00+08:00"))
            if nav:
                session.add(PerfSnapshot(instance_id=instance, date="20261028", execution_domain="live",
                                         nav=200_000, daily_return=None, positions_snapshot={}))
        if state:
            session.add(V713StateRecord(instance_id="live_v713_stockleg", signal_date="20260930", sleeve="TOP50",
                                        decision_date="20261028", basket_sha256="b" * 64,
                                        allocation_sha256="a" * 64, target_version_id=None,
                                        recorded_at="2026-10-28T16:00:00+08:00"))
        if nav:
            session.add(PerfValuation(instance_id="live_v713_stockleg", date="20261028", execution_domain="live",
                                      nav=200_000, cash=200_000, dividend_receivable=0))
        session.commit()


def run(environment):
    _, db, output, calendar = environment
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    assert job.main(["--policy", str(POLICY), "--calendar", str(calendar)], now=NOW) == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    return json.loads(output.read_text())


def test_connection_is_readonly_and_never_creates_a_missing_database(environment, tmp_path):
    _, db, _, _ = environment
    engine, _ = job.readonly_sessions(f"sqlite:///{db}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA query_only")) == 1
            with pytest.raises(OperationalError, match="readonly"):
                connection.execute(text("CREATE TABLE forbidden (id INTEGER)"))
    finally:
        engine.dispose()
    absent = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        job.readonly_sessions(f"sqlite:///{absent}")
    assert not absent.exists()


@pytest.mark.parametrize("dependency,status", [("instance", "WAITING_INSTANCE"),
                                               ("state", "WAITING_START"), ("nav", "WAITING_LEG_NAV")])
def test_missing_dependencies_publish_waiting_without_fabricated_nav(environment, dependency, status):
    if dependency != "instance":
        seed(environment[0], state=dependency != "state", nav=dependency != "nav")
    doc = run(environment)
    assert doc["status"] == status and doc["expected_date"] == "20261028"
    assert doc["as_of_date"] is None and doc["database_read_only"] is True
    assert all(line["rows"] == [] for line in doc["lines"].values())


def test_complete_evidence_publishes_v2_lines_without_mutation(environment):
    seed(environment[0])
    doc = run(environment)
    assert doc["status"] == "CURRENT" and doc["as_of_date"] == "20261028"
    assert doc["policy_id"] == "V713_SYNTHETIC_V2" and doc["primary_bps"] == 13
    assert set(doc["lines"]) == {"0", "13", "18", "20", "23"}
    assert doc["calendar"]["sha256"] == hashlib.sha256(environment[3].read_bytes()).hexdigest()


def test_report_never_uses_nav_after_the_expected_session(environment):
    seed(environment[0])
    with environment[0]() as session:
        for instance in ("live_v713_stockleg", "live_hydra_v481_rb"):
            session.add(PerfSnapshot(instance_id=instance, date="20261029", execution_domain="live",
                                     nav=300_000, daily_return=.5, positions_snapshot={}))
        session.commit()
    doc = run(environment)
    assert doc["status"] == "CURRENT" and doc["as_of_date"] == doc["expected_date"] == "20261028"
    assert all([row["date"] for row in line["rows"]] == ["20261028"] for line in doc["lines"].values())
    assert doc["lines"]["13"]["rows"][-1]["value"] == 400_000


def test_uncovered_calendar_replaces_old_current_report_with_waiting(environment):
    _, _, output, calendar = environment
    output.write_text('{"status":"CURRENT"}')
    payload = json.loads(calendar.read_text())
    payload["coverage_end"] = "20261027"
    calendar.write_text(json.dumps(payload))
    doc = run(environment)
    assert doc["status"] == "WAITING_TRADING_CALENDAR"
    assert doc["expected_date"] is None and doc["as_of_date"] is None


def test_exchange_calendar_handles_weekends_and_before_close(environment, monkeypatch):
    calendar = environment[3]
    assert job.expected_session(datetime.fromisoformat("2026-10-31T18:25:00+08:00"), calendar)[0] == "20261030"
    assert job.expected_session(NOW.replace(hour=10), calendar)[0] == "20261027"
    called = []
    monkeypatch.setattr("scripts.v713_cycle.exchange_dates", lambda day: called.append(day) or ["20261027", "20261028"])
    assert job.expected_session(NOW)[0] == "20261028" and called == ["20261028"]


def test_service_pins_code_and_runs_after_existing_valuation():
    deploy = Path(__file__).resolve().parents[2] / "deploy"
    service = (deploy / "qmt-v713-synthetic.service.in").read_text()
    timer = (deploy / "qmt-v713-synthetic.timer").read_text()
    assert "After=qmt-live-performance.service" in service
    assert "Environment=PYTHONPATH=@V713_RELEASE_SERVER@" in service
    assert "python @V713_RELEASE_SERVER@/scripts/run_v713_synthetic_eod.py" in service
    assert "synthetic_policy_v2.json" in service and " -m " not in service
    assert "18:25:00 Asia/Shanghai" in timer and "20:55:00 Asia/Shanghai" in timer
