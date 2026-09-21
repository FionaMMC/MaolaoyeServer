"""Real SQLite recovery/crash tests. No production, broker, or HTTP network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import select

from app.models import InstanceState, Order, PipelineJob
from app.scheduler.pipeline_lock import pipeline_mutex
from app.services.pipeline_jobs import MAX_ATTEMPTS, PipelineJobService
from app.strategy.base import StrategyInputNotReady
from tests.unit import test_pipeline as pipeline_fixtures
from tests.unit.test_pipeline import _write_yaml, _bar, AlwaysBuyStrategy

DAY = 20260430


@pytest.fixture
def setup(tmp_path):
    return pipeline_fixtures.setup.__wrapped__(tmp_path)


@pytest.fixture
def recovery(setup, monkeypatch):
    pipeline, sf, store, path = setup
    _write_yaml(path, {"account_groups": [
        {"group_id": name, "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 100000}]}
        for name in ("paper_a", "paper_b")]})
    clock = [datetime.fromisoformat("2026-04-29T18:00:00+08:00").timestamp()]
    monkeypatch.setattr("app.scheduler.pipeline._today_int", lambda: 20260429)
    store.append("indexes", "000852.SH", pd.DataFrame([_bar(20260429)]))
    return pipeline, sf, PipelineJobService(sf, path, clock=lambda: clock[0]), clock


def test_duplicates_coalesce_and_manual_promotes(recovery):
    _, sf, jobs, _ = recovery
    first = jobs.enqueue("paper_a", DAY, manual=False)
    with ThreadPoolExecutor(max_workers=5) as executor:
        receipts = list(executor.map(lambda _: jobs.enqueue("paper_a", DAY), range(10)))
    assert {r["job_id"] for r in receipts} == {first["job_id"]}
    with sf() as session:
        rows = session.scalars(select(PipelineJob)).all()
        assert len(rows) == 1 and rows[0].priority == 100


def test_manual_priority_and_scope(recovery):
    pipeline, sf, jobs, _ = recovery
    ordinary = jobs.enqueue("paper_a", DAY, manual=False)
    manual = jobs.enqueue("paper_b", DAY)
    result = jobs.run_once(pipeline)
    assert result["job_id"] == manual["job_id"] and result["status"] == "READY"
    assert jobs.get(ordinary["job_id"])["status"] == "QUEUED"
    with sf() as session:
        assert {r.account_group for r in session.scalars(select(Order))} == {"paper_b"}
        assert all(r.fetched_at is None for r in session.scalars(select(Order)))


def test_recovery_reuses_fetched_order_ids_without_valuation(recovery, monkeypatch):
    pipeline, sf, jobs, _ = recovery
    pipeline.run(DAY, account_group="paper_a")
    original = pipeline.orders_queue.list_pending(str(DAY))
    monkeypatch.setattr(pipeline.perf, "snapshot_all", lambda *a, **k: pytest.fail("unrelated valuation"))
    jobs.enqueue("paper_a", DAY)
    receipt = jobs.run_once(pipeline)
    assert receipt["status"] == "REUSED"
    assert receipt["result"]["order_ids"] == [r.order_id for r in original]


def test_worker_death_after_commit_reuses_publication(recovery, monkeypatch):
    pipeline, sf, jobs, _ = recovery
    first = jobs.enqueue("paper_a", DAY)
    finish = jobs._finish
    monkeypatch.setattr(jobs, "_finish", lambda *a: (_ for _ in ()).throw(SystemExit("killed")))
    with pytest.raises(SystemExit):
        jobs.run_once(pipeline)
    published = pipeline._order_summary(DAY, "paper", "paper_a")["order_ids"]
    assert published
    assert jobs.get(first["job_id"])["status"] == "RUNNING"
    assert pipeline.run(DAY, force=True)["skipped"] == "recovery_batch_published"
    monkeypatch.setattr(jobs, "_finish", finish)
    result = jobs.run_once(pipeline)
    assert result["status"] == "REUSED" and result["result"]["order_ids"] == published


def test_legacy_force_cannot_replace_recovery_publication(recovery):
    pipeline, _, jobs, _ = recovery
    jobs.enqueue("paper_a", DAY)
    result = jobs.run_once(pipeline)
    assert pipeline.run(DAY, force=True)["skipped"] == "recovery_batch_published"
    assert pipeline._order_summary(DAY, "paper", "paper_a")["order_ids"] == result["result"]["order_ids"]
    assert pipeline.run(DAY, account_group="paper_b")["orders"] == 1


def test_order_publication_failure_does_not_advance_strategy_state(setup, monkeypatch):
    pipeline, sf, _, path = setup
    class Stateful(AlwaysBuyStrategy):
        def run(self, ctx, trade_date):
            ctx.set_strategy_state({"month_done": "202604"})
            return super().run(ctx, trade_date)
    pipeline.registry["stateful"] = Stateful
    _write_yaml(path, {"account_groups": [{"group_id": "paper_a", "strategies": [
        {"strategy_id": "stateful", "virtual_initial_cash": 100000}]}]})
    def interrupted(*a, **k):
        raise ConnectionError("before order transaction")
    monkeypatch.setattr(pipeline.orders_queue, "write_aggregated", interrupted)
    with pytest.raises(ConnectionError):
        pipeline.run(20260430)
    with sf() as session:
        assert session.get(InstanceState, "paper_a_stateful").strategy_state is None
        assert not session.scalars(select(Order)).all()


@pytest.mark.parametrize("status", ["PARTIAL", "CANCELLED", "EXPIRED", "REJECTED"])
def test_existing_unfinished_orders_never_regenerated(recovery, status):
    pipeline, sf, jobs, _ = recovery
    pipeline.run(DAY, account_group="paper_a")
    with sf() as session:
        row = session.scalar(select(Order))
        original = row.order_id
        row.status = status
        session.commit()
    jobs.enqueue("paper_a", DAY)
    result = jobs.run_once(pipeline)
    assert result["status"] == "BLOCKED"
    assert result["result"]["order_ids"] == [original]


def test_missing_data_waits_then_recovers_same_job(recovery):
    pipeline, _, jobs, clock = recovery
    class Waiting(AlwaysBuyStrategy):
        def run(self, *args):
            raise StrategyInputNotReady("input temporarily absent")
    pipeline.registry["always_buy"] = Waiting
    first = jobs.enqueue("paper_a", DAY)
    assert jobs.run_once(pipeline)["status"] == "WAITING_INPUT"
    assert jobs.run_once(pipeline)["status"] == "IDLE"
    pipeline.registry["always_buy"] = AlwaysBuyStrategy
    clock[0] += 61
    result = jobs.run_once(pipeline)
    assert result["status"] == "READY" and result["job_id"] == first["job_id"]


def test_transient_failures_are_bounded_and_manual_can_resume(recovery, monkeypatch):
    pipeline, _, jobs, clock = recovery
    run = pipeline.run
    monkeypatch.setattr(pipeline, "run", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    first = jobs.enqueue("paper_a", DAY)
    for _ in range(MAX_ATTEMPTS):
        result = jobs.run_once(pipeline)
        clock[0] += 301
    assert result["status"] == "FAILED"
    assert jobs.run_once(pipeline)["status"] == "IDLE"
    monkeypatch.setattr(pipeline, "run", run)
    assert jobs.enqueue("paper_a", DAY)["job_id"] == first["job_id"]
    assert jobs.run_once(pipeline)["status"] == "READY"


def test_old_day_and_deadline_never_reissue(recovery):
    pipeline, _, jobs, clock = recovery
    with pytest.raises(ValueError):
        jobs.enqueue("paper_a", 20260999)
    with pytest.raises(ValueError):
        jobs.enqueue("paper_a", 20260428)
    job = jobs.enqueue("paper_a", DAY)
    clock[0] = job["deadline"]
    assert jobs.run_once(pipeline)["status"] == "EXPIRED"
    assert not pipeline._order_summary(DAY, "paper")["orders"]


def test_real_thread_mutex_keeps_http_run_from_double_publication(recovery):
    pipeline, sf, jobs, _ = recovery
    jobs.enqueue("paper_a", DAY)
    with pipeline_mutex(sf):
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(jobs.run_once, pipeline).result(timeout=3)
        assert result["status"] == "BUSY"
    assert jobs.run_once(pipeline)["status"] == "READY"


def test_unknown_strategy_error_is_not_false_no_orders(recovery):
    pipeline, _, jobs, _ = recovery
    pipeline.registry.clear()
    jobs.enqueue("paper_a", DAY)
    result = jobs.run_once(pipeline)
    assert result["status"] == "FAILED"
    assert result["result"]["strategy_errors"]


def test_noop_is_explicit_no_orders(recovery):
    pipeline, _, jobs, _ = recovery
    class Noop(AlwaysBuyStrategy):
        def run(self, *a):
            return []
    pipeline.registry["always_buy"] = Noop
    jobs.enqueue("paper_a", DAY)
    assert jobs.run_once(pipeline)["status"] == "NO_ORDERS"


def test_other_group_unresolved_does_not_block_or_expire(recovery):
    pipeline, sf, jobs, _ = recovery
    import yaml
    config = yaml.safe_load(jobs.strategies_file.read_text())
    config["account_groups"][1]["qmt_account_id"] = "synthetic-b"
    config["account_groups"][1]["strategies"][0].update(
        account_isolation="dedicated", requires_reconciled_rebalance=True)
    _write_yaml(jobs.strategies_file, config)
    pipeline.run(DAY, account_group="paper_b")
    old = pipeline._order_summary(DAY, "paper", "paper_b")["order_ids"]
    jobs.enqueue("paper_a", DAY)
    assert jobs.run_once(pipeline)["status"] == "READY"
    assert pipeline._order_summary(DAY, "paper", "paper_b")["order_ids"] == old


def test_missing_worker_visible_in_readonly_alerts(recovery):
    from app.services.ops_monitor import OpsMonitorService
    pipeline, sf, jobs, clock = recovery
    job = jobs.enqueue("paper_a", DAY)
    # Fixed historical clock makes this queue visibly overdue without mutations.
    issues = OpsMonitorService(sf).pipeline_recovery_issues(today=str(DAY))
    assert issues[0]["job_id"] == job["job_id"]
    assert issues[0]["reason"] == "worker_delayed_or_missing"
    assert jobs.get(job["job_id"])["status"] == "QUEUED"


def test_job_api_is_fast_enqueue_only(client, recovery, monkeypatch):
    from app.api.pipeline_jobs import service
    pipeline, _, jobs, _ = recovery
    client.app.dependency_overrides[service] = lambda: jobs
    monkeypatch.setattr(pipeline, "run", lambda *a, **k: pytest.fail("HTTP must not compute"))
    payload = {"account_group": "paper_a", "trade_date": DAY}
    assert client.post("/admin/pipeline-jobs", json=payload).status_code == 401
    headers = {"Authorization": "Bearer TEST_KEY"}
    first = client.post("/admin/pipeline-jobs", json=payload, headers=headers)
    assert first.status_code == 202
    job = first.json()["data"]
    assert job["status"] == "QUEUED"
    again = client.post("/admin/pipeline-jobs", json=payload, headers=headers)
    assert again.json()["data"]["job_id"] == job["job_id"]
    assert client.get("/admin/pipeline-jobs/" + job["job_id"], headers=headers).json()["data"] == job


def test_paper_alias_scope_is_enforced(client, recovery):
    from app.api.pipeline_jobs import service
    from app.auth import AuthContext, verify_api_key
    _, _, jobs, _ = recovery
    client.app.dependency_overrides[service] = lambda: jobs
    client.app.dependency_overrides[verify_api_key] = lambda: AuthContext("paper", "limited", ("different",))
    result = client.post("/admin/pipeline-jobs", json={"account_group": "paper_a", "trade_date": DAY})
    assert result.status_code == 403


def test_os_lock_serializes_separate_process(recovery):
    import subprocess
    import sys
    _, sf, _, _ = recovery
    with sf() as session:
        database = str(session.get_bind().url)
    program = '''
import sys
from app.db import make_engine, make_session_factory
from app.scheduler.pipeline_lock import pipeline_mutex, PipelineBusy
try:
    with pipeline_mutex(make_session_factory(make_engine(sys.argv[1]))):
        print("ACQUIRED")
except PipelineBusy:
    print("BUSY")
'''
    with pipeline_mutex(sf):
        result = subprocess.run([sys.executable, "-c", program, database], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == "BUSY"
    result = subprocess.run([sys.executable, "-c", program, database], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == "ACQUIRED"


def test_legacy_pipeline_no_longer_blocks_health_event_loop(client):
    import asyncio
    import time
    import httpx
    from app.dependencies import get_strategy_pipeline
    class SlowPipeline:
        def run(self, *a, **k):
            time.sleep(0.3)
            return {"orders": 0}
    client.app.dependency_overrides[get_strategy_pipeline] = lambda: SlowPipeline()
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as http:
            started = time.monotonic()
            task = asyncio.create_task(http.post("/admin/run-pipeline?trade_date=20260430",
                                                headers={"Authorization": "Bearer TEST_KEY"}))
            await asyncio.sleep(0.03)
            health = await http.get("/healthz")
            elapsed = time.monotonic() - started
            await task
            assert health.status_code == 200 and elapsed < 0.25
    asyncio.run(check())


def test_concurrent_cash_change_is_kept_and_job_recomputes(recovery, monkeypatch):
    pipeline, sf, jobs, clock = recovery
    jobs.enqueue("paper_a", DAY)
    publish = pipeline.orders_queue.write_aggregated
    def arrival(*args, **kwargs):
        with sf() as session:
            row = session.get(InstanceState, "paper_a_always_buy")
            row.virtual_cash += 100
            session.commit()
        return publish(*args, **kwargs)
    monkeypatch.setattr(pipeline.orders_queue, "write_aggregated", arrival)
    result = jobs.run_once(pipeline)
    assert result["status"] == "RETRY_WAIT"
    assert result["result"]["error_type"] == "StrategyStateChanged"
    assert not pipeline._order_summary(DAY, "paper")["orders"]
    with sf() as session:
        assert session.get(InstanceState, "paper_a_always_buy").virtual_cash == 100100
    monkeypatch.setattr(pipeline.orders_queue, "write_aggregated", publish)
    clock[0] += 61
    assert jobs.run_once(pipeline)["status"] == "READY"


def test_state_and_orders_roll_back_together_on_sql_error(recovery):
    from app.services.aggregate import AggregatedOrder
    from sqlalchemy.exc import IntegrityError
    pipeline, sf, _, _ = recovery
    pipeline._ensure_instance_states(pipeline._load_instances())
    order = AggregatedOrder(order_id="duplicate", account_group="paper_a", symbol="600519.SH",
                            direction="BUY", quantity=100, limit_price=10, valid_date=str(DAY))
    with pytest.raises(IntegrityError):
        pipeline.orders_queue.write_aggregated([order, order], [],
            strategy_states={"paper_a_always_buy": {"advanced": True}})
    with sf() as session:
        assert session.get(InstanceState, "paper_a_always_buy").strategy_state is None
        assert not session.scalars(select(Order)).all()


def test_job_status_is_readonly_and_shows_current_order_state(recovery):
    pipeline, sf, jobs, _ = recovery
    job = jobs.enqueue("paper_a", DAY)
    jobs.run_once(pipeline)
    with sf() as session:
        row = session.scalar(select(Order))
        assert row.fetched_at is None
        row.status = "FILLED"
        session.commit()
    result = jobs.get(job["job_id"])
    assert result["current_order_status_counts"] == {"FILLED": 1}
    assert result["result"]["order_status_counts"] == {"PENDING": 1}  # original receipt preserved
    with sf() as session:
        assert session.scalar(select(Order)).fetched_at is None
