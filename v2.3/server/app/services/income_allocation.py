"""Conserve received income across explicit strategy owners in one transaction.

No trading switches, current target, pending orders or current-position split.
The caller supplies entitlement evidence; this is not automatic ex-date accrual.
"""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.exceptions import APIError, ErrorCode
from app.models import (
    AccountCashObservation,
    CashFlowJournal,
    IncomeAllocationReceipt,
    InstanceState,
)
from app.schemas.income_allocation import IncomeAllocationRequest, IncomeAllocationResponse
from app.services.ledger_transaction import begin_ledger_transaction

SOURCE = "account-income-allocation"


def _conflict(message):
    raise APIError(ErrorCode.BAD_REQUEST, message, http_status=409)


class IncomeAllocationService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def apply(self, req: IncomeAllocationRequest) -> IncomeAllocationResponse:
        payload = req.model_dump(mode="json")
        payload["allocations"] = [
            {"instance_id": item.instance_id, "amount": format(item.amount, ".2f")}
            for item in sorted(req.allocations, key=lambda item: item.instance_id)
        ]
        digest = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        with self.session_factory() as session:
            begin_ledger_transaction(session, req.execution_domain, req.account_alias)
            observation = session.get(AccountCashObservation, req.observation_id)
            if observation is None:
                _conflict("账户现金事实不存在；先记录实际到账事件")
            if (observation.execution_domain, observation.account_alias) != (
                req.execution_domain,
                req.account_alias,
            ):
                raise APIError(ErrorCode.AUTH_FAILED, "收入归属跨域/账户", http_status=403)
            receipt = session.get(IncomeAllocationReceipt, req.observation_id)
            if receipt is not None:
                if receipt.request_sha256 != digest:
                    _conflict("这笔收入已经分配，不能覆盖原归属；更正必须另走冲正流程")
                return IncomeAllocationResponse(
                    **(receipt.response_payload | {"already_applied": True})
                )
            if (
                observation.event_type not in {"DIVIDEND", "INTEREST", "OTHER"}
                or observation.amount <= 0
            ):
                _conflict("本接口只分配已到账正收入；入金增资走 capital-movements")
            total = sum((share.amount for share in req.allocations), Decimal(0))
            if total != Decimal(str(observation.amount)):
                _conflict("各策略分配金额合计必须等于该笔实际收入，不允许增发现金或遗留未解释差额")
            legacy = session.execute(
                select(CashFlowJournal.id)
                .where(
                    CashFlowJournal.execution_domain == req.execution_domain,
                    CashFlowJournal.account_alias == req.account_alias,
                    CashFlowJournal.source == observation.source,
                    CashFlowJournal.source_event_id == observation.source_event_id,
                )
                .limit(1)
            ).first()
            if legacy:
                _conflict("原始现金事件已有旧版入账记录；先迁移其归属回执，不能再加一次现金")

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            allocations = []
            for share in sorted(req.allocations, key=lambda item: item.instance_id):
                state = session.get(InstanceState, share.instance_id)
                if state is None or state.ledger_mode != "attributed":
                    _conflict(f"策略 {share.instance_id} 尚无独立子账本")
                if (state.execution_domain, state.account_alias) != (
                    req.execution_domain,
                    req.account_alias,
                ):
                    raise APIError(ErrorCode.AUTH_FAILED, "策略收入归属跨域/账户", http_status=403)
                cash = Decimal(str(state.virtual_cash)) + share.amount
                if not cash.is_finite():
                    _conflict("策略原账本现金无效，收入事实保留但不能覆盖坏账本")
                row = CashFlowJournal(
                    execution_domain=req.execution_domain,
                    account_alias=req.account_alias,
                    instance_id=share.instance_id,
                    event_date=req.event_date,
                    event_type=observation.event_type,
                    amount=float(share.amount),
                    source=SOURCE,
                    source_event_id=f"{observation.id}/{share.instance_id}",
                    evidence_sha256=req.evidence_sha256,
                    description=f"Allocated account observation {observation.id}; entitlement evidence supplied explicitly",
                    currency="CNY",
                    status="APPLIED",
                    created_at=now,
                    applied_at=now,
                )
                session.add(row)
                state.virtual_cash = float(cash)
                state.last_update = now
                session.flush()
                allocations.append(
                    {
                        "instance_id": share.instance_id,
                        "amount": format(share.amount, ".2f"),
                        "journal_id": row.id,
                        "cash_after_at_allocation": str(cash),
                    }
                )
            response = IncomeAllocationResponse(
                observation_id=observation.id,
                execution_domain=req.execution_domain,
                account_alias=req.account_alias,
                request_sha256=digest,
                already_applied=False,
                allocations=allocations,
            )
            session.add(
                IncomeAllocationReceipt(
                    observation_id=observation.id,
                    request_sha256=digest,
                    request_payload=payload,
                    response_payload=response.model_dump(mode="json"),
                )
            )
            session.commit()
            return response
