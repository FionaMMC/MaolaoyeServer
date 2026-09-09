"""End an execution window without manufacturing a broker terminal event.

No order generation switch, balance authorization, order rewrite, or broker call
belongs here. Final settlement continues through the existing close operation.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.exceptions import APIError, ErrorCode
from app.models import HydraWorkflowClosure, Order
from app.schemas.hydra_relay import (
    HydraAttemptCloseRequest,
    HydraAttemptCloseResponseData,
)

# Unknown statuses are obligations, not implicit permission to reuse resources.
TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED", "NOT_SUBMITTED", "EXPIRED_BY_POLICY"})


def has_policy_expired_orders(session, rebalance_id: str) -> bool:
    return session.execute(select(Order.order_id).where(
        Order.rebalance_id == rebalance_id, Order.status == "EXPIRED_BY_POLICY",
    ).limit(1)).first() is not None


def unresolved_orders(session, rebalance_id: str) -> list[str]:
    return list(session.execute(
        select(Order.order_id)
        .where(Order.rebalance_id == rebalance_id)
        .where(Order.status.not_in(TERMINAL_ORDER_STATUSES))
        .order_by(Order.order_id)
    ).scalars())


def close_execution_window(session, attempt, rebalance, req: HydraAttemptCloseRequest):
    payload = req.model_dump(mode="json")
    receipt_id = "hc_" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prior = session.get(HydraWorkflowClosure, receipt_id)
    if prior is not None:
        return HydraAttemptCloseResponseData(**prior.response_payload)

    deadline = req.execution_deadline_at
    now = datetime.now(timezone.utc)
    if deadline is None or deadline.utcoffset() is None or deadline > now:
        raise APIError(ErrorCode.BAD_REQUEST, "尚未到 execution_deadline_at", http_status=409)
    # The caller declares its operational window, not a universal exchange rule.
    # Closing this window NEVER certifies that old orders can no longer fill.
    if deadline.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d") != attempt.trade_date:
        raise APIError(ErrorCode.BAD_REQUEST, "execution_deadline_at 与 attempt 交易日不一致")
    positions = req.actual_positions or {}
    if any(isinstance(qty, bool) or not isinstance(qty, int) or qty < 0 for qty in positions.values()):
        raise APIError(ErrorCode.BAD_REQUEST, "非法实际持仓")

    unresolved = unresolved_orders(session, rebalance.rebalance_id)
    final = attempt.status in {"COMPLETE", "RESIDUAL"}
    status = attempt.status if final else (
        "CLOSED_PENDING_BROKER" if unresolved else "CLOSED_PENDING_RECONCILIATION"
    )
    response = HydraAttemptCloseResponseData(
        target_id=rebalance.target_id,
        rebalance_id=rebalance.rebalance_id,
        attempt_id=attempt.attempt_id,
        execution_domain=req.execution_domain,
        status=status,
        residual_after=dict(attempt.residual_after or {}) if final else {},
        broker_finalized=not unresolved and not has_policy_expired_orders(session, rebalance.rebalance_id),
        effective_finalized=not unresolved,
        retry_ready=final and status == "RESIDUAL" and not unresolved,
        unresolved_order_ids=unresolved,
        # An account snapshot may include several owners. Do not label this as
        # a strategy residual; only final close can calculate the attributed one.
        provisional_residual={},
        closure_receipt_id=receipt_id,
    )
    if not final:
        attempt.status = status
        attempt.closed_at = attempt.closed_at or now.isoformat(timespec="seconds")
        rebalance.reconciliation_status = status
    session.add(HydraWorkflowClosure(
        receipt_id=receipt_id,
        attempt_id=attempt.attempt_id,
        execution_domain=req.execution_domain,
        account_alias=req.account_alias,
        request_payload=payload,
        response_payload=response.model_dump(mode="json"),
        created_at=now.isoformat(timespec="seconds"),
    ))
    session.commit()
    return response
