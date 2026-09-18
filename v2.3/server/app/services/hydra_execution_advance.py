"""Evening control plane: resume approved plans and closed server residuals.

No strategy recomputation, account initialization, order copying or QMT I/O.
Each stage has its own short transaction; published parquet loading is outside.
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import (
    HydraExecutionPlan,
    HydraMonthlyCycle,
    HydraExecutionAttempt,
    HydraRebalance,
    HydraTarget,
    Order,
    OrderSignalMap,
    RawSignal,
)
from app.exceptions import APIError, ErrorCode
from app.schemas.hydra_relay import HydraTargetRequest, HydraRetryRequest, hydra_basket_hash
from app.services.hydra_execution_policy import eligible, wait_result
from app.services.hydra_execution_publish import latest_execution_publication
from app.services.hydra_relay import unresolved_late_fill_review


def advance_execution(service, req):
    if req.execution_domain != "live":
        raise APIError(ErrorCode.BAD_REQUEST, "该执行政策仅适用于 live Hydra")
    service._gate_live()
    publication = latest_execution_publication(service, req.account_alias, req.reference_date)
    if publication is None:
        return {
            "status": "WAITING_EXECUTION_DATA",
            "reference_date": req.reference_date,
            "reason": "等待该参考日已发布的原价与冻结日历；不会沿用旧价",
            "order_count": 0,
        }
    raw, raw_manifest = service.data_store.load(
        "hydra_execution_raw", publication.execution_raw_sha256
    )
    calendar, calendar_manifest = service.data_store.load(
        "hydra_trading_calendar", publication.execution_calendar_sha256
    )
    execution_date = (datetime.strptime(req.reference_date, "%Y%m%d") + timedelta(days=1)).strftime(
        "%Y%m%d"
    )
    if not eligible(calendar, req.reference_date, execution_date):
        return wait_result(
            "live", calendar, req.reference_date, reason="今日不是合格参考日，等待下一交易对"
        ).model_dump(mode="json")

    # Physical positions are facts, not permission to assign manual trades to
    # Hydra. An unexplained manual top-up must be journaled before new orders.
    with service.session_factory() as session:
        state = service._validated_state(session, req.instance_id, "live", req.account_alias)
        if unresolved_late_fill_review(session, req.instance_id, "live", req.account_alias):
            return {
                "status": "WAITING_RECONCILIATION",
                "reason": "迟到成交影响的旧执行包仍待核实",
                "order_count": 0,
            }
        research_pending = session.scalar(select(HydraMonthlyCycle).where(
            HydraMonthlyCycle.execution_domain == "live",
            HydraMonthlyCycle.account_alias == req.account_alias,
            HydraMonthlyCycle.instance_id == req.instance_id,
            HydraMonthlyCycle.as_of_date <= req.reference_date,
            HydraMonthlyCycle.status != "PLANNED",
        ))
        if research_pending:
            return {
                "status": "WAITING_EXECUTION_DATA",
                "reason": "月度冻结包已收到，独立研究进程尚未发布新目标；稍后重跑晚间拉单，不沿用旧目标",
                "cycle_id": research_pending.cycle_id,
                "order_count": 0,
            }
        if state.ledger_mode == "attributed":
            try:
                service._assert_attributed_portfolio_snapshot(
                    "live", req.account_alias, req.actual_cash, req.actual_positions
                )
            except APIError as exc:
                if exc.http_status != 409:
                    raise
                return {"status": "WAITING_RECONCILIATION", "reason": exc.message, "order_count": 0}
        elif (
            service._validate_positions(req.actual_positions) != dict(state.virtual_positions or {})
            or abs(req.actual_cash - state.virtual_cash) > 1
        ):
            return {
                "status": "WAITING_RECONCILIATION",
                "reason": "实际账户与策略账本未对齐；先接入人工/迟到成交，不生成重复补单",
                "order_count": 0,
            }
        plans = list(
            session.scalars(
                select(HydraExecutionPlan)
                .where(
                    HydraExecutionPlan.execution_domain == "live",
                    HydraExecutionPlan.account_alias == req.account_alias,
                    HydraExecutionPlan.instance_id == req.instance_id,
                    HydraExecutionPlan.status != "STAGED",
                )
                .order_by(HydraExecutionPlan.created_at)
            )
        )
        owned = (
            select(Order.rebalance_id)
            .join(OrderSignalMap, OrderSignalMap.order_id == Order.order_id)
            .join(
                RawSignal,
                RawSignal.signal_id == OrderSignalMap.signal_id,
            )
            .where(
                Order.execution_domain == "live",
                Order.qmt_account_alias == req.account_alias,
                RawSignal.execution_domain == "live",
                RawSignal.instance_id == req.instance_id,
            )
            .distinct()
        )
        rebalances = list(
            session.scalars(
                select(HydraRebalance).where(
                    HydraRebalance.rebalance_id.in_(owned),
                    HydraRebalance.execution_domain == "live",
                    HydraRebalance.account_alias == req.account_alias,
                )
            )
        )
        # Captured identity only: stage_retry rechecks current lifecycle/holdings
        # under its monetary-write lock, including any concurrent late fills.
        residual_ids = []
        existing = []
        for rebalance in rebalances:
            latest = session.scalar(
                select(HydraExecutionAttempt)
                .where(
                    HydraExecutionAttempt.rebalance_id == rebalance.rebalance_id,
                )
                .order_by(HydraExecutionAttempt.attempt_number.desc())
                .limit(1)
            )
            if latest is None:
                continue
            if latest.trade_date == execution_date and latest.status in {"PENDING", "NOOP"}:
                existing.append(
                    service._response(
                        session.get(HydraTarget, rebalance.target_id), latest, idempotent=True
                    ).model_dump()
                )
            elif latest.status == "RESIDUAL":
                residual_ids.append(rebalance.rebalance_id)

    results = list(existing)
    for plan in plans:
        # If publication succeeded but the plan receipt write was interrupted,
        # recover by source_plan_id, not by generating another target/version.
        with service.session_factory() as session:
            attempts = session.scalars(
                select(HydraExecutionAttempt).where(
                    HydraExecutionAttempt.execution_domain == "live",
                    HydraExecutionAttempt.account_alias == req.account_alias,
                )
            ).all()
            prior = next(
                (
                    a
                    for a in attempts
                    if (a.risk_snapshot.get("execution_policy") or {}).get("source_plan_id")
                    == plan.plan_id
                ),
                None,
            )
            if prior:
                rebalance = session.get(HydraRebalance, prior.rebalance_id)
                staged = service._response(
                    session.get(HydraTarget, rebalance.target_id), prior, idempotent=True
                )
            else:
                staged = None
        if staged is None:
            payload = dict(plan.request_payload)
            if (
                payload["decision_date"] >= execution_date
                or payload["execution_date"] > execution_date
            ):
                continue  # This request belongs to a later approved cycle.
            payload.update(
                execution_date=execution_date,
                execution_raw_sha256=raw_manifest.file_sha256,
                execution_calendar_sha256=calendar_manifest.file_sha256,
            )
            payload["basket_sha256"] = hydra_basket_hash(payload)
            staged = service.stage_initial(
                HydraTargetRequest(**payload), source_plan_id=plan.plan_id, physical_snapshot=req
            )
        with service.session_factory() as session:
            row = session.get(HydraExecutionPlan, plan.plan_id)
            row.status = "STAGED"
            row.response_payload = staged.model_dump(mode="json")
            session.commit()
        results.append(staged.model_dump(mode="json"))
    for rebalance_id in residual_ids:
        staged = service.stage_retry(
            HydraRetryRequest(
                execution_domain="live",
                account_alias=req.account_alias,
                rebalance_id=rebalance_id,
                trade_date=execution_date,
                execution_raw_sha256=raw_manifest.file_sha256,
                execution_calendar_sha256=calendar_manifest.file_sha256,
                actual_cash=req.actual_cash,
                actual_positions=req.actual_positions,
                reconciliation_evidence_sha256=req.reconciliation_evidence_sha256,
            )
        )
        results.append(staged.model_dump(mode="json"))
    results = list({result["attempt_id"]: result for result in results}.values())
    return {
        "status": "EXECUTION_ADVANCED" if results else "NO_PENDING_EXECUTION",
        "reference_date": req.reference_date,
        "execution_date": execution_date,
        "results": results,
    }
