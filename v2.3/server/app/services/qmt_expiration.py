"""User-approved current-broker Hydra day-order policy, not a universal QMT rule."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.models import ExecutionQualityObservation, HydraExecutionAttempt, OrderStatusEvidence

POLICY_ID = "QMT_DAY_ORDER_1500_V1"
CHINA_TIMEZONE = timezone(timedelta(hours=8))


def validate_policy_expiration(session, order, result, trade_date: str) -> str | None:
    """Validate a specific matched observation, without blocking unrelated facts."""
    if result.status != "EXPIRED_BY_POLICY":
        if result.expiration_policy_id is not None:
            return "EXPIRATION_POLICY_WITHOUT_EXPIRED_STATUS"
        return None
    if (
        order.execution_domain != "live" or not order.target_id
        or not order.rebalance_id or not order.attempt_id
        or trade_date != order.valid_date
    ):
        return "EXPIRATION_REQUIRES_MATCHED_LIVE_HYDRA_DAY_ORDER"
    attempt = session.get(HydraExecutionAttempt, order.attempt_id)
    if (
        attempt is None or attempt.execution_domain != order.execution_domain
        or attempt.account_alias != order.qmt_account_alias
        or attempt.rebalance_id != order.rebalance_id
        or attempt.trade_date != trade_date
        or attempt.batch_sha256 != order.batch_sha256
    ):
        return "EXPIRATION_ATTEMPT_IDENTITY_MISMATCH"
    if (
        result.expiration_policy_id != POLICY_ID or result.raw_qmt_status != 50
        or not result.qmt_order_id or result.symbol != order.symbol
        or result.direction != order.direction
        or result.filled_quantity >= order.quantity
        or (result.filled_quantity > 0 and result.filled_price <= 0)
    ):
        return "EXPIRATION_OBSERVATION_INVALID"
    observed = result.status_observed_at
    if observed is None or observed.utcoffset() is None:
        return "EXPIRATION_REQUIRES_ZONED_OBSERVATION"
    china_observed = observed.astimezone(CHINA_TIMEZONE)
    if (
        china_observed.strftime("%Y%m%d") != trade_date
        or china_observed.hour < 15
        or observed > datetime.now(timezone.utc)
    ):
        return "EXPIRATION_OUTSIDE_SAME_DAY_1500_WINDOW"
    known = session.get(ExecutionQualityObservation, order.order_id)
    if known is not None and known.qmt_order_id and known.qmt_order_id != result.qmt_order_id:
        return "EXPIRATION_BROKER_ORDER_ID_MISMATCH"
    return None


def record_status_evidence(session, order, result, received_at: str) -> None:
    if result.raw_qmt_status is None and result.expiration_policy_id is None:
        return
    payload = {
        "observation": result.model_dump(mode="json"),
        "execution_domain": order.execution_domain,
        "account_alias": order.qmt_account_alias,
        "target_id": order.target_id,
        "rebalance_id": order.rebalance_id,
        "attempt_id": order.attempt_id,
        "batch_sha256": order.batch_sha256,
        "valid_date": order.valid_date,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if session.get(OrderStatusEvidence, digest) is None:
        session.add(OrderStatusEvidence(
            evidence_sha256=digest, order_id=order.order_id,
            execution_domain=order.execution_domain, account_alias=order.qmt_account_alias,
            payload=payload, received_at=received_at,
        ))
