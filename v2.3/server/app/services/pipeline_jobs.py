"""Durable, account-group scoped recovery for the paper pipeline only.

Manual priority affects ready queued work, never interrupts a publication/fill.
No broker submission, force regeneration, or synthetic terminal report here.
"""
from datetime import datetime, time as daytime
from hashlib import sha256
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.models import Order, PipelineJob
from app.scheduler.pipeline_lock import PipelineBusy, pipeline_mutex
from app.services.orders_queue import StrategyStateChanged

ACTIVE = {"QUEUED", "RUNNING", "WAITING_INPUT", "RETRY_WAIT"}
TRANSIENT_SKIPS = {"pipeline_busy", "stale_market_data", "market_data_not_ready_for_future_batch"}
MAX_ATTEMPTS = 6
CHINA = ZoneInfo("Asia/Shanghai")


def job_identity(group, trade_date):
    return sha256(f"paper|{group}|{trade_date}".encode()).hexdigest()


def serialize(job):
    return {key: getattr(job, key) for key in (
        "job_id", "execution_domain", "account_group", "trade_date", "priority",
        "status", "attempts", "created_at", "updated_at", "next_attempt_at",
        "deadline", "result",
    )}


class PipelineJobService:
    def __init__(self, session_factory, strategies_file, clock=time.time):
        self.sf = session_factory
        self.strategies_file = Path(strategies_file)
        self.clock = clock

    def account_alias(self, group):
        config = yaml.safe_load(self.strategies_file.read_text(encoding="utf-8")) or {}
        groups = [g for g in config.get("account_groups", []) if g["group_id"] == group
                  and g.get("execution_domain", "paper") == "paper" and g.get("strategies")]
        if len(groups) != 1:
            raise ValueError("unknown or ambiguous paper account group")
        return groups[0].get("qmt_account_alias")

    def get(self, job_id):
        with self.sf() as session:
            row = session.get(PipelineJob, job_id)
            if row is None:
                return None
            result = serialize(row)
            ids = row.result.get("order_ids", [])
            if ids:
                orders = session.scalars(select(Order).where(
                    Order.order_id.in_(ids), Order.execution_domain == "paper",
                    Order.account_group == row.account_group,
                    Order.valid_date == str(row.trade_date),
                )).all()
                counts = {}
                for order in orders:
                    counts[order.status] = counts.get(order.status, 0) + 1
                result["current_order_status_counts"] = counts
                result["current_order_ids_match"] = {o.order_id for o in orders} == set(ids)
            return result

    def enqueue(self, group, trade_date, *, manual=True):
        self.account_alias(group)
        day = datetime.strptime(str(trade_date), "%Y%m%d").date()
        now = self.clock()
        deadline = datetime.combine(day, daytime(15, 0), tzinfo=CHINA).timestamp()
        if now >= deadline or deadline - now > 14 * 86400:
            raise ValueError("recovery must be within 14 days and before execution-day 15:00 Asia/Shanghai")
        identity = job_identity(group, trade_date)
        with self.sf() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PipelineJob, identity)
            if row is None:
                row = PipelineJob(job_id=identity, execution_domain="paper", account_group=group,
                                  trade_date=trade_date, priority=100 if manual else 10,
                                  status="QUEUED", attempts=0, created_at=now, updated_at=now,
                                  next_attempt_at=now, deadline=deadline, result={})
                session.add(row)
            else:
                # Promotion does not reset an in-progress retry budget. Only a
                # deliberate new manual request after a terminal failure resumes.
                if manual:
                    row.priority = 100
                    if row.status in {"FAILED", "BLOCKED"}:
                        row.status = "QUEUED"
                        row.attempts = 0
                    if row.status != "RUNNING":
                        row.next_attempt_at = now
                row.updated_at = now
            session.commit()
            identity = row.job_id
        return self.get(identity)

    def _finish(self, identity, status, result):
        now = self.clock()
        with self.sf() as session:
            row = session.get(PipelineJob, identity)
            if status in {"WAITING_INPUT", "RETRY_WAIT"}:
                if row.attempts >= MAX_ATTEMPTS:
                    status = "FAILED"
                    result = {**result, "reason": "retry_budget_exhausted"}
                else:
                    row.next_attempt_at = now + min(60 * 2 ** (row.attempts - 1), 300)
            row.status, row.result, row.updated_at = status, result, now
            session.commit()
            return serialize(row)

    def run_once(self, pipeline):
        try:
            with pipeline_mutex(self.sf):
                return self._run_locked(pipeline)
        except PipelineBusy:
            return {"status": "BUSY", "reason": "another_pipeline_is_running"}

    def _run_locked(self, pipeline):
        now = self.clock()
        with self.sf() as session:
            # A RUNNING row here is orphaned: this worker owns the OS mutex.
            row = session.scalar(select(PipelineJob).where(
                PipelineJob.status.in_(ACTIVE), PipelineJob.next_attempt_at <= now,
            ).order_by(PipelineJob.priority.desc(), PipelineJob.created_at).limit(1))
            if row is None:
                return {"status": "IDLE"}
            identity, group, day = row.job_id, row.account_group, row.trade_date
            if now >= row.deadline:
                row.status, row.updated_at = "EXPIRED", now
                row.result = {"reason": "execution_cutoff_passed", "orders_reissued": False}
                session.commit()
                return serialize(row)
            # Check durable publication before consuming retries, including
            # worker death after order commit but before writing job result.
            existing = pipeline._order_summary(day, "paper", group)
            if not existing["orders"] and row.attempts >= MAX_ATTEMPTS:
                row.status, row.updated_at = "FAILED", now
                row.result = {"reason": "retry_budget_exhausted"}
                session.commit()
                return serialize(row)
            row.status, row.updated_at = "RUNNING", now
            row.attempts += 1
            session.commit()
        try:
            summary = pipeline.run(day, account_group=group, recovery=True)
        except (ConnectionError, TimeoutError, OperationalError, StrategyStateChanged) as exc:
            # Retrying is safe because every invocation first checks publication.
            return self._finish(identity, "RETRY_WAIT", {"error_type": type(exc).__name__})
        except Exception as exc:
            return self._finish(identity, "FAILED", {"error_type": type(exc).__name__})

        if summary.get("reused"):
            counts = summary["order_status_counts"]
            status = "REUSED"
            if summary.get("bookkeeping_divergence") or set(counts) - {"PENDING", "FILLED"}:
                status = "BLOCKED"
                summary["reason"] = "existing_orders_need_settlement_not_regeneration"
        elif summary.get("waiting_input"):
            status = "WAITING_INPUT" if not summary["orders"] else "READY_WITH_WARNINGS"
        elif summary.get("strategy_errors"):
            status = "FAILED" if not summary["orders"] else "READY_WITH_WARNINGS"
        elif summary.get("skipped"):
            status = "WAITING_INPUT" if summary["skipped"] in TRANSIENT_SKIPS else "BLOCKED"
        elif summary["orders"]:
            status = "READY" if summary["passed"] == summary["signals"] else "READY_WITH_WARNINGS"
        elif summary.get("signals"):
            status = "BLOCKED"
            summary["reason"] = "all_signals_failed_precheck"
        else:
            status = "NO_ORDERS"
            # A frozen V53 target awaiting terminal reports is NOT a completed
            # rebalance. Expose that distinction without blocking other groups.
            for instance, guard in summary.get("execution_guards", {}).items():
                if not guard["allowed"]:
                    status = "BLOCKED"
                if instance.endswith("_v53") and not guard["residual_retry_allowed"]:
                    status = "BLOCKED"
                    summary["reason"] = "v53_target_awaits_execution_confirmation"
        return self._finish(identity, status, summary)
