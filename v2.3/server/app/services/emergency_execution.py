"""Manual emergency orders through the normal ledger and broker-report lineage."""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.exceptions import APIError, ErrorCode
from app.models import (EmergencyExecution, HydraExecutionAttempt, HydraExecutionPlan,
                        HydraRebalance, HydraTarget, Order, OrderSignalMap, RawSignal)
from app.services.emergency_guard import active_emergency
from app.services.hydra_closure import TERMINAL_ORDER_STATUSES, has_policy_expired_orders
from app.services.hydra_late_fills import unresolved_late_fill_review
from app.services.hydra_relay import _hash, _now_iso
from app.services.ledger_transaction import begin_ledger_transaction


def authorization_id(req):
    return _hash("emergency", {"account_alias": req.account_alias, "request_id": req.request_id})


def _now():
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _fail(message):
    raise APIError(ErrorCode.BAD_REQUEST, message, http_status=409)


def _replay(session, key, digest):
    prior = session.get(EmergencyExecution, key)
    if prior:
        if prior.request_sha256 != digest:
            _fail("request_id 已存在且内容不同，禁止更改紧急指令")
        return {**prior.response_payload, "idempotent_replay": True, "authorization_status": prior.status}


def stage_emergency(service, req, authenticated_client):
    payload = req.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key = authorization_id(req)
    with service.session_factory() as session:
        replay = _replay(session, key, digest)
        if replay:
            return replay
    service._gate_live()
    now = _now()
    today = now.strftime("%Y%m%d")
    if req.trade_date < today or (req.trade_date == today and now.hour >= 15):
        _fail("当日指令须在 15:00 前生成；过期指令不能新建")
    raw, manifest = service.data_store.load("hydra_execution_raw", req.execution_raw_sha256)
    service._validate_execution_raw_universe(raw, manifest)
    calendar, _ = service.data_store.load("hydra_trading_calendar", req.execution_calendar_sha256)
    days = sorted(set(calendar["trade_date"].astype(str).str.replace("-", "", regex=False)))
    if req.trade_date not in days:
        _fail("紧急指令执行日不在已冻结交易日历中")
    next_days = [day for day in days if day > today]
    if req.trade_date != today and (not next_days or req.trade_date != next_days[0]):
        _fail("紧急指令只允许当日或下一个交易日")
    prior_days = [day for day in days if day < req.trade_date]
    if not prior_days or manifest.as_of_date != prior_days[-1]:
        _fail("紧急指令需要上一个交易日的冻结原价")
    requested = {order.symbol for order in req.orders}
    if requested - service.allowed_symbols:
        _fail("紧急指令超出账户证券白名单")
    if service.blacklist_service and requested & service.blacklist_service.compute(execution_domain="live"):
        _fail("紧急指令命中执行风险黑名单")

    with service.session_factory() as session:
        begin_ledger_transaction(session, "live", req.account_alias)
        replay = _replay(session, key, digest)
        if replay:
            return replay
        if active_emergency(session, req.account_alias):
            _fail("本账户已有紧急执行待完成，请先结算和恢复")
        state = service._validated_state(session, req.instance_id, "live", req.account_alias)
        if unresolved_late_fill_review(session, req.instance_id, "live", req.account_alias):
            _fail("迟到成交影响的执行包仍待核实")
        positions = service._validate_positions(dict(state.virtual_positions or {}))
        rows = service._require_as_of_coverage(raw, manifest.as_of_date, requested | set(positions), "execution_raw")
        if (rows.loc[rows["symbol"].isin(requested), "suspendFlag"].astype(int) != 0).any():
            _fail("紧急指令证券停牌")
        prices = rows.set_index("symbol")["close"].astype(float).to_dict()
        limits = {order.symbol: order.limit_price for order in req.orders}
        offset = max(abs(order.limit_price / prices[order.symbol] - 1) * 10_000 for order in req.orders)
        if offset > service.live_limits.effective_price_offset_bps + 1e-7:
            _fail("紧急指令限价超过原价偏移上限")
        owned_ids = set(session.scalars(select(Order.order_id)
            .join(OrderSignalMap, OrderSignalMap.order_id == Order.order_id)
            .join(RawSignal, RawSignal.signal_id == OrderSignalMap.signal_id)
            .where(RawSignal.instance_id == req.instance_id, RawSignal.execution_domain == "live")))
        old_orders = list(session.scalars(select(Order).where(
            Order.execution_domain == "live", Order.qmt_account_alias == req.account_alias)))
        retired = []
        for order in old_orders:
            if order.valid_date == req.trade_date and order.fetched_at is not None:
                _fail("当日已有客户端领取的冻结批次，不能替换其订单信号；需先处理客户端批次衔接")
            if order.bookkeeping_divergence:
                _fail("账户存在成交记账差异，先核实资金与持仓")
            if order.status in TERMINAL_ORDER_STATUSES:
                continue
            if order.order_id in owned_ids and order.status == "PENDING" and order.fetched_at is None:
                order.status = "CANCELLED"  # Never delivered: no broker cancellation is inferred.
                retired.append(order.order_id)
            else:
                _fail("账户仍有已领取、活动或未知委托；先撤单、结算并对账")
        prior_rebalances = {order.rebalance_id for order in old_orders if order.order_id in owned_ids and order.rebalance_id}
        for rebalance_id in prior_rebalances:
            if has_policy_expired_orders(session, rebalance_id):
                _fail("旧委托只有政策到期证据，仍需券商最终状态")
            attempts = list(session.scalars(select(HydraExecutionAttempt).where(
                HydraExecutionAttempt.rebalance_id == rebalance_id)))
            for attempt in attempts:
                delivered = any(order.attempt_id == attempt.attempt_id and order.fetched_at for order in old_orders)
                if delivered and attempt.status not in {"COMPLETE", "RESIDUAL"}:
                    _fail("已领取的旧执行批次尚未完成成交结算和对账")
        target_shares = dict(positions)
        for order in req.orders:
            target_shares[order.symbol] = positions.get(order.symbol, 0) + (
                order.quantity if order.direction == "BUY" else -order.quantity)
            if target_shares[order.symbol] < 0:
                _fail("卖出数量超过策略归属持仓")
        stamp = _now_iso()
        target = HydraTarget(target_id=key, execution_domain="live", account_alias=req.account_alias,
            strategy_version="MANUAL_EMERGENCY_V1", publisher_source_commit="manual-operator",
            decision_date=req.trade_date, as_of_date=manifest.as_of_date, execution_date=req.trade_date,
            basket_sha256=digest, research_input_hashes={},
            input_hashes={"execution_raw": req.execution_raw_sha256, "trading_calendar": req.execution_calendar_sha256},
            weights={}, cash_buffer_weight=0, status="STAGED", created_at=stamp)
        rebalance = HydraRebalance(rebalance_id=_hash("her", {"authorization_id": key}),
            target_id=key, execution_domain="live", account_alias=req.account_alias,
            baseline_cash=state.virtual_cash, baseline_positions=positions, target_shares=target_shares,
            status="OPEN", reconciliation_status="MANUAL_EMERGENCY", created_at=stamp)
        audit = {"authorization_id": key, "request_sha256": digest, "operator": req.operator,
                 "authenticated_client": authenticated_client, "reason": req.reason,
                 "bypassed_rules": ["strategy_review", "monthly_rebalance_cadence", "adjacent_natural_day"],
                 "retired_undelivered_orders": retired, "superseded_rebalance_ids": sorted(prior_rebalances)}
        session.add_all([target, rebalance])
        result = service._create_attempt(session=session, target=target, rebalance=rebalance,
            instance_id=req.instance_id, trade_date=req.trade_date, actual_cash=state.virtual_cash,
            actual_positions=positions, prices=prices, buy_offset=min(offset, 50), sell_offset=min(offset, 50),
            explicit_limits=limits, emergency_audit=audit, reconciliation_evidence_sha256=digest)
        # Close old intent, preserving all orders/fills and their identities.
        for rebalance_id in prior_rebalances:
            old = session.get(HydraRebalance, rebalance_id)
            old.status = "SUPERSEDED_EMERGENCY"
            session.get(HydraTarget, old.target_id).status = "SUPERSEDED_EMERGENCY"
        plans = session.scalars(select(HydraExecutionPlan).where(
            HydraExecutionPlan.execution_domain == "live", HydraExecutionPlan.account_alias == req.account_alias,
            HydraExecutionPlan.instance_id == req.instance_id))
        for plan in plans:
            plan.status = "SUPERSEDED_EMERGENCY"
        response = {**result.model_dump(), "authorization_id": key, "authorization_status": "ACTIVE", "audit": audit}
        session.add(EmergencyExecution(authorization_id=key, account_alias=req.account_alias,
            instance_id=req.instance_id, request_sha256=digest, authenticated_client=authenticated_client,
            request_payload=payload, response_payload=response, status="ACTIVE", created_at=stamp))
        session.commit()
        return response


def resume_after_emergency(service, req, authenticated_client):
    with service.session_factory() as session:
        begin_ledger_transaction(session, "live", req.account_alias)
        entry = session.get(EmergencyExecution, authorization_id(req))
        if entry is None:
            if active_emergency(session, req.account_alias):
                _fail("账户存在另一紧急执行")
            return {"status": "NOT_STAGED", "new_plan_required": False, "idempotent_replay": True}
        if entry.instance_id != req.instance_id:
            _fail("紧急指令不存在或实例不匹配")
        if entry.status == "RELEASED":
            return {"status": "RELEASED", "new_plan_required": True, "idempotent_replay": True}
        service._validated_state(session, req.instance_id, "live", req.account_alias)
        service._assert_no_unresolved(session, req.instance_id, "live")
        if unresolved_late_fill_review(session, req.instance_id, "live", req.account_alias):
            _fail("迟到成交尚待处理")
        attempt = session.get(HydraExecutionAttempt, entry.response_payload["attempt_id"])
        if attempt.status not in {"COMPLETE", "RESIDUAL"} or has_policy_expired_orders(session, attempt.rebalance_id):
            _fail("紧急批次尚未完成券商最终结算和对账")
        entry.status = "RELEASED"
        rebalance = session.get(HydraRebalance, attempt.rebalance_id)
        rebalance.status = "SUPERSEDED_EMERGENCY"
        session.get(HydraTarget, rebalance.target_id).status = "SUPERSEDED_EMERGENCY"
        entry.release_evidence = {"authenticated_client": authenticated_client, "reason": req.reason,
                                  "released_at": _now_iso(), "new_plan_required": True}
        session.commit()
        return {"status": "RELEASED", "new_plan_required": True, "idempotent_replay": False}
