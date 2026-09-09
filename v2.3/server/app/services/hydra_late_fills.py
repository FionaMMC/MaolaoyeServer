"""Invalidate stale Hydra close snapshots after a newly received positive fill.

The caller must add the cumulative Trade row first and call this inside the
same settlement transaction, even if the owned-ledger projection diverged.
This helper neither commits nor rewrites/cancels frozen or broker orders.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import logging

from sqlalchemy import func, select

from app.models import (
    HydraExecutionAttempt, HydraRebalance, HydraTarget, Order,
    OrderSignalMap, RawSignal, Trade,
)

logger = logging.getLogger(__name__)
FINAL_CLOSE_STATUSES = frozenset({"COMPLETE", "RESIDUAL"})
REOPENED_STATUS = "CLOSED_PENDING_RECONCILIATION"


def _close_snapshot(attempt: HydraExecutionAttempt) -> dict:
    return {
        "status": attempt.status,
        "residual_after": deepcopy(attempt.residual_after),
        "posttrade_reconciliation_sha256": attempt.posttrade_reconciliation_sha256,
        "reconciled_cash": attempt.reconciled_cash,
        "reconciled_positions": deepcopy(attempt.reconciled_positions),
        "closed_at": attempt.closed_at,
    }


def _newer_owned_attempts(session, order: Order, source: HydraExecutionAttempt):
    """Follow actual order ownership across rebalances, never a version label.

    An orderless NOOP is included only when another order in its rebalance
    proves ownership. A wholly unmapped legacy NOOP has no deliverable order
    and cannot safely be assigned to a strategy by guessing its target name.
    """
    owners = sorted(set(session.execute(
        select(RawSignal.instance_id)
        .join(OrderSignalMap, OrderSignalMap.signal_id == RawSignal.signal_id)
        .where(OrderSignalMap.order_id == order.order_id)
        .where(OrderSignalMap.signal_quantity > 0)
        .where(RawSignal.execution_domain == order.execution_domain)
    ).scalars()))
    if not owners:
        return [], owners
    related_rebalances = select(Order.rebalance_id).join(
        OrderSignalMap, OrderSignalMap.order_id == Order.order_id,
    ).join(RawSignal, RawSignal.signal_id == OrderSignalMap.signal_id).join(
        HydraExecutionAttempt, HydraExecutionAttempt.attempt_id == Order.attempt_id,
    ).where(
        Order.execution_domain == order.execution_domain,
        Order.qmt_account_alias == order.qmt_account_alias,
        Order.rebalance_id == HydraExecutionAttempt.rebalance_id,
        HydraExecutionAttempt.execution_domain == order.execution_domain,
        HydraExecutionAttempt.account_alias == order.qmt_account_alias,
        RawSignal.execution_domain == order.execution_domain,
        RawSignal.instance_id.in_(owners),
        OrderSignalMap.signal_quantity > 0,
    ).distinct()
    candidates = session.execute(select(HydraExecutionAttempt).where(
        HydraExecutionAttempt.rebalance_id.in_(related_rebalances),
        HydraExecutionAttempt.rebalance_id != source.rebalance_id,
        HydraExecutionAttempt.execution_domain == order.execution_domain,
        HydraExecutionAttempt.account_alias == order.qmt_account_alias,
        HydraExecutionAttempt.trade_date >= source.trade_date,
    ).order_by(HydraExecutionAttempt.trade_date, HydraExecutionAttempt.created_at,
               HydraExecutionAttempt.attempt_number, HydraExecutionAttempt.attempt_id)).scalars()
    newer = []
    for candidate in candidates:
        if candidate.trade_date == source.trade_date:
            try:
                candidate_time = datetime.fromisoformat(candidate.created_at.replace("Z", "+00:00"))
                source_time = datetime.fromisoformat(source.created_at.replace("Z", "+00:00"))
                if candidate_time.utcoffset() is None or source_time.utcoffset() is None:
                    raise ValueError("unknown creation timezone")
                if candidate_time < source_time:
                    continue
            except (AttributeError, ValueError):
                # Same owner and day, but legacy timestamps cannot establish
                # order: require local review rather than assume it is older.
                pass
        newer.append(candidate)
    return newer, owners


def unresolved_late_fill_review(
    session, instance_id: str, execution_domain: str, account_alias: str,
) -> list[str]:
    """Return this owner's explicitly unresolved old-batch review attempts.

    This is not a policy-expiry gate: ordinary expiry has no manual-review
    marker. Account cash fact ingestion never needs this query. Rebalance-wide
    proven ownership also covers affected NOOP attempts with no own orders.
    """
    owned_rebalances = select(Order.rebalance_id).join(
        OrderSignalMap, OrderSignalMap.order_id == Order.order_id,
    ).join(RawSignal, RawSignal.signal_id == OrderSignalMap.signal_id).join(
        HydraExecutionAttempt, HydraExecutionAttempt.attempt_id == Order.attempt_id,
    ).where(
        Order.execution_domain == execution_domain,
        Order.qmt_account_alias == account_alias,
        RawSignal.execution_domain == execution_domain,
        RawSignal.instance_id == instance_id,
        OrderSignalMap.signal_quantity > 0,
        HydraExecutionAttempt.execution_domain == execution_domain,
        HydraExecutionAttempt.account_alias == account_alias,
        Order.rebalance_id == HydraExecutionAttempt.rebalance_id,
    ).distinct()
    attempts = session.execute(select(HydraExecutionAttempt).where(
        HydraExecutionAttempt.rebalance_id.in_(owned_rebalances),
        HydraExecutionAttempt.execution_domain == execution_domain,
        HydraExecutionAttempt.account_alias == account_alias,
    ).order_by(HydraExecutionAttempt.trade_date, HydraExecutionAttempt.created_at,
               HydraExecutionAttempt.attempt_number, HydraExecutionAttempt.attempt_id)).scalars()
    return [
        attempt.attempt_id for attempt in attempts
        if ((attempt.risk_snapshot or {}).get("late_fill_review") or {}).get("requires_manual_resolution")
    ]


def invalidate_hydra_close_after_late_fill(session, order: Order, received_at: str) -> list[str]:
    """Reopen a finalized source and quarantine already-created successors.

    Repeated calls for the same order/time/cumulative quantity are idempotent.
    Additional positive fills while review is pending append more evidence.
    Scope follows the stored account/domain and the same strategy's exact order
    ownership, including newer monthly/capital rebalances for that strategy.
    Return affected attempt IDs so the caller can surface local batch review.
    """
    if not order.attempt_id or not order.rebalance_id or not order.target_id:
        return []
    attempt = session.get(HydraExecutionAttempt, order.attempt_id)
    if attempt is None:
        logger.error("late Hydra fill lacks attempt lineage: order=%s", order.order_id)
        return []
    if (
        attempt.rebalance_id != order.rebalance_id
        or attempt.execution_domain != order.execution_domain
        or attempt.account_alias != order.qmt_account_alias
    ):
        logger.error("late Hydra fill has inconsistent attempt scope: order=%s", order.order_id)
        return []
    previous_review = (attempt.risk_snapshot or {}).get("late_fill_review") or {}
    if attempt.status not in FINAL_CLOSE_STATUSES and not previous_review.get("events"):
        return []

    rebalance = session.get(HydraRebalance, order.rebalance_id)
    target = session.get(HydraTarget, order.target_id)
    if (
        rebalance is None or target is None
        or rebalance.target_id != target.target_id
        or rebalance.execution_domain != order.execution_domain
        or rebalance.account_alias != order.qmt_account_alias
        or target.execution_domain != order.execution_domain
        or target.account_alias != order.qmt_account_alias
    ):
        logger.error("late Hydra fill lacks consistent rebalance lineage: order=%s", order.order_id)
        return []

    # Autoflush includes the new cumulative Trade added by the settlement
    # caller. Quantity disambiguates multiple positive fills in the same second.
    cumulative_quantity = session.execute(select(func.max(Trade.filled_quantity)).where(
        Trade.order_id == order.order_id,
        Trade.execution_domain == order.execution_domain,
    )).scalar_one()
    if not cumulative_quantity:
        return []
    identity = {
        "order_id": order.order_id,
        "received_at": received_at,
        "cumulative_filled_quantity": int(cumulative_quantity),
    }
    event_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if any(event.get("event_id") == event_id for event in previous_review.get("events", [])):
        return []

    successors = list(session.execute(select(HydraExecutionAttempt).where(
        HydraExecutionAttempt.rebalance_id == rebalance.rebalance_id,
        HydraExecutionAttempt.execution_domain == order.execution_domain,
        HydraExecutionAttempt.account_alias == order.qmt_account_alias,
        HydraExecutionAttempt.attempt_number > attempt.attempt_number,
    ).order_by(HydraExecutionAttempt.attempt_number)).scalars())
    cross_rebalance, owner_ids = _newer_owned_attempts(session, order, attempt)
    # Keep each affected parent's own prior snapshot. Do not overwrite an
    # unrelated strategy's target simply because it shares the broker account.
    parents = {rebalance.rebalance_id: (rebalance, target)}
    for candidate in cross_rebalance:
        related = session.get(HydraRebalance, candidate.rebalance_id)
        related_target = session.get(HydraTarget, related.target_id) if related else None
        if (
            related is None or related_target is None
            or related.execution_domain != order.execution_domain
            or related.account_alias != order.qmt_account_alias
            or related_target.execution_domain != order.execution_domain
            or related_target.account_alias != order.qmt_account_alias
        ):
            logger.error("late fill successor has inconsistent parent scope: attempt=%s", candidate.attempt_id)
            continue
        parents[related.rebalance_id] = (related, related_target)
        successors.append(candidate)
    successor_ids = [item.attempt_id for item in successors]
    delivered = list(session.execute(select(Order.order_id).where(
        Order.attempt_id.in_(successor_ids),
        Order.execution_domain == order.execution_domain,
        Order.qmt_account_alias == order.qmt_account_alias,
        Order.fetched_at.is_not(None),
    ).order_by(Order.order_id)).scalars()) if successor_ids else []
    parent_snapshots = {
        key: {
            "rebalance_id": related.rebalance_id,
            "target_id": related_target.target_id,
            "rebalance_status": related.status,
            "reconciliation_status": related.reconciliation_status,
            "rebalance_closed_at": related.closed_at,
            "target_status": related_target.status,
        }
        for key, (related, related_target) in parents.items()
    }
    for affected in (attempt, *successors):
        risk = deepcopy(affected.risk_snapshot or {})
        review = deepcopy(risk.get("late_fill_review") or {})
        events = list(review.get("events") or [])
        events.append({
            "event_id": event_id,
            **identity,
            "source_attempt_id": attempt.attempt_id,
            "source_rebalance_id": rebalance.rebalance_id,
            "source_instance_ids": owner_ids,
            "bookkeeping_divergence": bool(order.bookkeeping_divergence),
            "invalidated_close": _close_snapshot(affected),
            "invalidated_parent": deepcopy(parent_snapshots[affected.rebalance_id]),
        })
        review.update({
            "reason": "LATE_FILL_RECONCILIATION_REQUIRED",
            "events": events,
            "last_received_at": received_at,
            "requires_manual_resolution": bool(review.get("requires_manual_resolution")) or bool(successors),
            "affected_successor_attempt_ids": sorted(set(
                review.get("affected_successor_attempt_ids", []) + successor_ids
            )),
            "affected_successor_rebalance_ids": sorted(set(
                review.get("affected_successor_rebalance_ids", [])
                + [item.rebalance_id for item in successors]
            )),
            "possibly_delivered_successor_order_ids": sorted(set(
                review.get("possibly_delivered_successor_order_ids", []) + delivered
            )),
        })
        risk["late_fill_review"] = review
        affected.risk_snapshot = risk
        affected.status = REOPENED_STATUS
        affected.residual_after = None
        affected.posttrade_reconciliation_sha256 = None
        affected.reconciled_cash = None
        affected.reconciled_positions = None
        affected.closed_at = None

    for related, related_target in parents.values():
        related.status = "OPEN"
        related.reconciliation_status = "LATE_FILL_RECONCILIATION_REQUIRED"
        related.closed_at = None
        related_target.status = "ACTIVE"
    return [attempt.attempt_id, *successor_ids]
