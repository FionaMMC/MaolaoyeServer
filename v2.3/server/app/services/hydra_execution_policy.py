"""Adjacent natural-day policy. Waiting dates are workflow results, not errors."""

from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import select

from app.models import HydraExecutionPlan
from app.schemas.hydra_relay import HydraExecutionWaitResponseData
from app.services.ledger_transaction import begin_ledger_transaction
from app.exceptions import APIError, ErrorCode

POLICY_ID = "HYDRA_ADJACENT_DAY_50BP_V1"


def next_pair(calendar, not_before: str):
    days = sorted(set(calendar["trade_date"].astype(str).str.replace("-", "", regex=False)))
    known = set(days)
    for day in days:
        if day < not_before:
            continue
        following = (datetime.strptime(day, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        if following in known:
            return day, following
    return None, None


def eligible(calendar, reference_date, execution_date):
    return next_pair(calendar, reference_date) == (reference_date, execution_date)


def policy_evidence(reference_date, raw_sha, calendar_sha, source_plan_id=None):
    result = dict(
        policy_id=POLICY_ID,
        reference_date=reference_date,
        execution_raw_sha256=raw_sha,
        execution_calendar_sha256=calendar_sha,
        buy_max_bps=50,
        sell_max_bps=50,
    )
    if source_plan_id:
        result["source_plan_id"] = source_plan_id
    return result


def wait_result(domain, calendar, not_before, *, reason, rebalance_id=None, plan_id=None):
    reference, execution = next_pair(calendar, not_before)
    return HydraExecutionWaitResponseData(
        status="WAITING_EXECUTION_DATE",
        execution_domain=domain,
        next_reference_date=reference,
        next_execution_date=execution,
        reason=reason,
        rebalance_id=rebalance_id,
        plan_id=plan_id,
    )


def remember_initial_wait(sf, req, calendar):
    payload = req.model_dump(mode="json")
    plan_id = "hp_" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    response = wait_result(
        req.execution_domain,
        calendar,
        req.execution_date,
        reason="等待合格交易对及该参考日的新执行原价；批准权重保持不变",
        plan_id=plan_id,
    )
    with sf() as session:
        begin_ledger_transaction(session, req.execution_domain, req.account_alias)
        prior = session.get(HydraExecutionPlan, plan_id)
        if prior:
            if prior.status == "STAGED":
                from app.schemas.hydra_relay import HydraRelayResponseData

                return HydraRelayResponseData(**prior.response_payload)
            return HydraExecutionWaitResponseData(**prior.response_payload)
        pending = session.scalars(
            select(HydraExecutionPlan).where(
                HydraExecutionPlan.execution_domain == req.execution_domain,
                HydraExecutionPlan.account_alias == req.account_alias,
                HydraExecutionPlan.instance_id == req.instance_id,
                HydraExecutionPlan.status != "STAGED",
            )
        ).all()
        if pending:
            raise APIError(
                ErrorCode.BAD_REQUEST,
                "本策略已有不同待执行计划；请明确替换，而非静默覆盖",
                http_status=409,
            )
        session.add(
            HydraExecutionPlan(
                plan_id=plan_id,
                execution_domain=req.execution_domain,
                account_alias=req.account_alias,
                instance_id=req.instance_id,
                request_payload=payload,
                status=response.status,
                response_payload=response.model_dump(mode="json"),
                created_at=datetime.now().isoformat(),
            )
        )
        session.commit()
    return response
