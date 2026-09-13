"""Additive strategy ownership changes on the existing attributed ledger.

This service neither transfers money at the broker nor approves a deposit which
has already happened. QMT remains the source of the physical cash snapshot.
Existing frozen targets are not resized, and unconfirmed order obligations are
not released by a calendar timeout. See the migration note for the boundaries
of the current legacy ledger / Float storage / snapshot contract.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from app.exceptions import APIError, ErrorCode
from app.models import CashFlowJournal, InstanceState, Order, OrderSignalMap, RawSignal, Trade
from app.models.capital_movement_receipt import CapitalMovementReceipt
from app.models.account_cash_observation import AccountCashObservation
from app.schemas.strategy_capital import CapitalMovementRequest, CapitalMovementResponseData
from app.services.ledger_transaction import begin_ledger_transaction
from app.services.hydra_closure import TERMINAL_ORDER_STATUSES

SOURCE = "strategy-capital-movement-v1"
# Workflow-only closure is absent; approved effective order expiry is included.
BROKER_TERMINAL = TERMINAL_ORDER_STATUSES


def _cash(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise APIError(ErrorCode.BAD_REQUEST, "账本金额不是有限数", http_status=409)
    return result


def _conflict(message: str) -> None:
    raise APIError(ErrorCode.BAD_REQUEST, message, http_status=409)


def _request_hash(req: CapitalMovementRequest) -> str:
    payload = req.model_dump(mode="json")
    # 100, 100.0 and 100.00 denote the same CNY command.
    for name in ("amount", "qmt_cash_balance"):
        payload[name] = format(getattr(req, name).quantize(Decimal("0.01")), "f")
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


class StrategyCapitalService:
    def __init__(self, session_factory, *, commission_rate=0.0003, min_commission=5.0):
        self.session_factory = session_factory
        self.commission_rate = _cash(commission_rate)
        self.min_commission = _cash(min_commission)
        if self.commission_rate < 0 or self.min_commission < 0:
            raise ValueError("commission budget cannot be negative")

    def apply(self, req: CapitalMovementRequest) -> CapitalMovementResponseData:
        digest = _request_hash(req)
        with self.session_factory() as session:
            begin_ledger_transaction(session, req.execution_domain, req.account_alias)
            existing = session.execute(select(CashFlowJournal).where(
                CashFlowJournal.execution_domain == req.execution_domain,
                CashFlowJournal.account_alias == req.account_alias,
                CashFlowJournal.source == SOURCE,
                CashFlowJournal.source_event_id == req.source_event_id,
            )).scalar_one_or_none()
            # A lost response must replay the original journal receipt before
            # checking current cash, pending orders or later ownership changes.
            if existing is not None:
                receipt = session.get(CapitalMovementReceipt, existing.id)
                if receipt is None:
                    _conflict("资本变动缺少原始请求回执，须恢复审计记录，不能再次划拨")
                if receipt.request_sha256 != digest:
                    _conflict("同一资本变动事件的内容不同，不能覆盖原分录")
                return self._receipt(existing, receipt, replay=True)

            state = session.get(InstanceState, req.instance_id)
            if state is None:
                raise APIError(ErrorCode.BAD_REQUEST, "策略账本尚未初始化", http_status=404)
            if (state.execution_domain, state.account_alias) != (
                req.execution_domain, req.account_alias,
            ):
                raise APIError(ErrorCode.AUTH_FAILED, "资本变动不能跨域或跨账户", http_status=403)
            if state.ledger_mode != "attributed":
                _conflict("增减资只适用于已初始化的 attributed 子账本；不能代替初始化")

            peers = session.execute(select(InstanceState).where(
                InstanceState.execution_domain == req.execution_domain,
                InstanceState.account_alias == req.account_alias,
            )).scalars().all()
            cash_before = _cash(state.virtual_cash)
            delta = req.amount if req.action == "INCREASE" else -req.amount
            cash_after = cash_before + delta
            protected = Decimal(0)
            unattributed_income = Decimal(0)
            physical_watermark = None
            if req.action == "INCREASE":
                physical_watermark = self.check_physical_snapshot(session, req)
                self.check_account_attribution(session, req)
                # A negative owner balance is a deficit to reconcile, not new
                # free money that another strategy may claim.
                total_owned_cash = sum((max(Decimal(0), _cash(peer.virtual_cash)) for peer in peers), Decimal(0))
                unattributed_income = self.unattributed_income_cash(session, req)
                if total_owned_cash + delta > req.qmt_cash_balance - unattributed_income:
                    _conflict("账户未分配现金不足；不能把其他策略的现金划入本策略")
            else:
                protected = self._protected_buy_cash(session, req)
                if cash_after < protected:
                    _conflict(
                        f"仅可减未占用现金；当前现金 {cash_before}，"
                        f"未终局买单保护金额 {protected}。等待成交/撤单事实后可重试，"
                        "不会自动卖出持仓或释放旧单义务"
                    )

            applied_at = datetime.now(timezone.utc)
            now = applied_at.isoformat(timespec="seconds")
            row = CashFlowJournal(
                execution_domain=req.execution_domain,
                account_alias=req.account_alias,
                instance_id=req.instance_id,
                # This endpoint applies immediately, not on a requested future
                # or historical date. Otherwise principal looks like today's P&L.
                event_date=applied_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"),
                event_type="CAPITAL_ALLOCATION" if delta > 0 else "CAPITAL_DEALLOCATION",
                amount=float(delta),
                qmt_cash_snapshot=float(req.qmt_cash_balance),
                snapshot_time=req.snapshot_time,
                transition_to_attributed=False,
                currency="CNY",
                source=SOURCE,
                source_event_id=req.source_event_id,
                evidence_sha256=req.evidence_sha256,
                description=req.description,
                status="APPLIED",
                created_at=now,
                applied_at=now,
            )
            session.add(row)
            state.virtual_cash = float(cash_after)
            state.last_update = now
            # Positions, initial principal and strategy state remain untouched.
            session.flush()
            receipt = CapitalMovementReceipt(
                journal_id=row.id, request_sha256=digest,
                request_payload=req.model_dump(mode="json"),
                calculation_snapshot={
                    "cash_before": str(cash_before), "cash_after": str(cash_after),
                    "protected_buy_cash": str(protected),
                    "unattributed_income_cash": str(unattributed_income),
                    "physical_event_watermark": physical_watermark.isoformat() if physical_watermark else None,
                    "target_policy": "NEXT_UNFROZEN_TARGET",
                },
            )
            session.add(receipt)
            result = self._receipt(row, receipt, replay=False)
            session.commit()
            return result

    @staticmethod
    def _receipt(row: CashFlowJournal, receipt: CapitalMovementReceipt, *, replay: bool) -> CapitalMovementResponseData:
        return CapitalMovementResponseData(
            journal_id=row.id, execution_domain=row.execution_domain,
            account_alias=row.account_alias, instance_id=row.instance_id,
            source_event_id=row.source_event_id, event_type=row.event_type,
            amount=row.amount, request_sha256=receipt.request_sha256,
            effective_date=row.event_date,
            already_applied=replay,
        )

    def _protected_buy_cash(self, session, req: CapitalMovementRequest) -> Decimal:
        orders = session.execute(select(Order).where(
            Order.execution_domain == req.execution_domain,
            Order.qmt_account_alias == req.account_alias,
            Order.direction == "BUY",
            or_(Order.status.not_in(BROKER_TERMINAL), Order.bookkeeping_divergence.is_(True)),
        )).scalars().all()
        if not orders:
            return Decimal(0)
        order_ids = [order.order_id for order in orders]
        mappings = session.execute(
            select(OrderSignalMap, RawSignal)
            .outerjoin(RawSignal, RawSignal.signal_id == OrderSignalMap.signal_id)
            .where(OrderSignalMap.order_id.in_(order_ids))
        ).all()
        by_order = defaultdict(list)
        for mapping, signal in mappings:
            by_order[mapping.order_id].append((mapping, signal))
        filled = dict(session.execute(
            select(Trade.order_id, func.max(Trade.filled_quantity))
            .where(Trade.order_id.in_(order_ids))
            .where(Trade.execution_domain == req.execution_domain)
            .group_by(Trade.order_id)
        ).all())
        protected = Decimal(0)
        for order in orders:
            rows = by_order[order.order_id]
            if not rows or any(signal is None or signal.execution_domain != req.execution_domain for _, signal in rows):
                _conflict("同账户有未能归属的买单；只暂停减资，须先补齐订单归属")
            own_weight = sum(mapping.signal_quantity for mapping, signal in rows
                             if signal.instance_id == req.instance_id)
            if not own_weight:
                continue
            if order.bookkeeping_divergence:
                _conflict("本策略买单已有账务差异；先接回成交事实再计算可减现金")
            total_weight = sum(mapping.signal_quantity for mapping, _ in rows)
            if total_weight <= 0 or any(mapping.signal_quantity < 0 for mapping, _ in rows):
                _conflict("订单归属数量无效；不能据此释放策略现金")
            remaining = max(0, order.quantity - int(filled.get(order.order_id, 0)))
            # Round this owner's outstanding quantity up, never below a share.
            own_remaining = (Decimal(remaining) * own_weight / total_weight).to_integral_value(
                rounding=ROUND_CEILING,
            )
            if len({signal.instance_id for _, signal in rows}) > 1:
                # Legacy settlement splits EACH fill delta by largest remainder.
                # Until owner-level cumulative allocations are persisted, the
                # simple pro-rata remainder can understate an owner's obligation.
                # Only that owner's deallocation uses this conservative bound.
                own_remaining = Decimal(remaining)
            if own_remaining == 0:
                continue
            limit = _cash(order.limit_price)
            if limit <= 0:
                _conflict("未终局买单缺少有效限价，无法计算可减现金")
            notional = own_remaining * limit
            fee = max(self.min_commission, notional * self.commission_rate)
            protected += notional + fee
        return protected.quantize(Decimal("0.01"), rounding=ROUND_CEILING)

    @classmethod
    def check_physical_snapshot(cls, session, req) -> datetime | None:
        watermark = cls._physical_watermark(session, req)
        try:
            snapshot = datetime.fromisoformat(req.snapshot_time.replace("Z", "+00:00"))
            if snapshot.tzinfo is None or snapshot.utcoffset() is None:
                raise ValueError("missing timezone")
        except (AttributeError, ValueError) as exc:
            raise APIError(ErrorCode.BAD_REQUEST, "资本快照时间必须为含时区的 ISO 时间", http_status=400) from exc
        if watermark is not None and snapshot < watermark:
            _conflict(
                "QMT 现金快照早于服务器已收到的账户资金/成交事实；"
                "仅本次增资需重新读取 QMT 快照后重试，事实接收不受影响"
            )
        return watermark

    @staticmethod
    def unattributed_income_cash(session, req) -> Decimal:
        """Unknown-owner income is suspense, not free account reserve.

        Explicit allocation receipts release suspense only after the same
        transaction credits all strategy owners. No income becomes free capital.
        Deposits are reserve; withdrawals already reduce QMT cash, not twice.
        """
        from app.models import IncomeAllocationReceipt
        values = session.execute(select(AccountCashObservation.amount).where(
            AccountCashObservation.execution_domain == req.execution_domain,
            AccountCashObservation.account_alias == req.account_alias,
            AccountCashObservation.event_type.in_(("DIVIDEND", "INTEREST", "OTHER")),
            AccountCashObservation.amount > 0,
            ~select(IncomeAllocationReceipt.observation_id).where(
                IncomeAllocationReceipt.observation_id == AccountCashObservation.id,
            ).exists(),
        )).scalars()
        return sum((_cash(amount) for amount in values), Decimal(0))

    @staticmethod
    def check_account_attribution(session, req) -> None:
        unmatched_ownership = session.execute(select(Order.order_id).where(
            Order.execution_domain == req.execution_domain,
            Order.qmt_account_alias == req.account_alias,
            Order.bookkeeping_divergence.is_(True),
        ).limit(1)).first()
        if unmatched_ownership:
            _conflict(
                "同账户有成交尚未正确归属；不能把原策略卖出所得当成未分配现金。"
                "先补齐归属后再增资，现金事实仍可继续接收"
            )
        ambiguous_owner = session.execute(select(InstanceState.instance_id).where(
            InstanceState.execution_domain == req.execution_domain,
            InstanceState.account_alias.is_(None),
        ).limit(1)).first()
        if ambiguous_owner:
            _conflict("旧账本尚无账户归属；先补齐归属再计算未分配现金")

    @staticmethod
    def _physical_watermark(session, req: CapitalMovementRequest) -> datetime | None:
        """Latest server-received physical event, not internal capital changes.

        Compare real instants: ISO lexical ordering is incorrect across offsets.
        This is a causal freshness floor, not proof of no unseen broker changes.
        """
        queries = (
            select(Trade.received_at).join(Order, Order.order_id == Trade.order_id).where(
                Trade.execution_domain == req.execution_domain,
                Order.execution_domain == req.execution_domain,
                Order.qmt_account_alias == req.account_alias,
            ),
            select(AccountCashObservation.recorded_at).where(
                AccountCashObservation.execution_domain == req.execution_domain,
                AccountCashObservation.account_alias == req.account_alias,
            ),
            select(CashFlowJournal.applied_at).where(
                CashFlowJournal.execution_domain == req.execution_domain,
                CashFlowJournal.account_alias == req.account_alias,
                CashFlowJournal.status == "APPLIED",
                CashFlowJournal.event_type.not_in(("CAPITAL_ALLOCATION", "CAPITAL_DEALLOCATION")),
            ),
        )
        latest = None
        for query in queries:
            for value in session.execute(query).scalars():
                try:
                    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if instant.tzinfo is None or instant.utcoffset() is None:
                        raise ValueError("missing timezone")
                except (AttributeError, ValueError) as exc:
                    raise APIError(
                        ErrorCode.BAD_REQUEST,
                        "账户历史事实缺少可比较的接收时间；先补齐时间证据再新增资本归属",
                        http_status=409,
                    ) from exc
                if latest is None or instant > latest:
                    latest = instant
        return latest
