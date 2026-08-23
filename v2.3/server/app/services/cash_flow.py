"""幂等地登记外部现金流并原子更新策略虚拟现金。"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from sqlalchemy import select

from app.exceptions import APIError, ErrorCode
from app.models import CashFlowJournal, InstanceState
from app.schemas.cash_flow import CashFlowRequest, CashFlowResponseData


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class CashFlowService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def apply(self, req: CashFlowRequest) -> CashFlowResponseData:
        if not math.isfinite(req.amount):
            raise APIError(ErrorCode.BAD_REQUEST, "cash flow amount 必须为有限数")

        with self.session_factory() as session:
            existing = session.execute(
                select(CashFlowJournal).where(
                    CashFlowJournal.execution_domain == req.execution_domain,
                    CashFlowJournal.account_alias == req.account_alias,
                    CashFlowJournal.source == req.source,
                    CashFlowJournal.source_event_id == req.source_event_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                immutable = (
                    existing.instance_id == req.instance_id
                    and existing.event_date == req.event_date
                    and existing.event_type == req.event_type
                    and existing.amount == req.amount
                    and existing.evidence_sha256 == req.evidence_sha256
                )
                if not immutable:
                    raise APIError(
                        ErrorCode.BAD_REQUEST,
                        "同一 source_event_id 的现金流内容发生变化，拒绝覆盖",
                        http_status=409,
                    )
                state = session.get(InstanceState, existing.instance_id)
                if state is None:
                    raise APIError(
                        ErrorCode.BAD_REQUEST,
                        "现金流 journal 对应的 instance_state 不存在，拒绝返回不完整账本",
                        http_status=409,
                    )
                return CashFlowResponseData(
                    journal_id=existing.id,
                    execution_domain=existing.execution_domain,
                    instance_id=existing.instance_id,
                    event_date=existing.event_date,
                    amount=existing.amount,
                    already_applied=True,
                    virtual_cash_after=float(state.virtual_cash),
                )

            state = session.get(InstanceState, req.instance_id)
            if state is None:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    f"instance_state 不存在: {req.instance_id}",
                    http_status=404,
                )
            if state.execution_domain != req.execution_domain:
                raise APIError(
                    ErrorCode.AUTH_FAILED,
                    "现金流与 instance_state execution_domain 不一致",
                    http_status=403,
                )
            if state.account_alias not in (None, req.account_alias):
                raise APIError(
                    ErrorCode.AUTH_FAILED,
                    "现金流与 instance_state account_alias 不一致",
                    http_status=403,
                )

            cash_after = float(state.virtual_cash) + req.amount
            if cash_after < 0:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    "现金流会令 virtual_cash 小于 0，拒绝入账",
                    http_status=409,
                )

            now = _now_iso()
            row = CashFlowJournal(
                execution_domain=req.execution_domain,
                account_alias=req.account_alias,
                instance_id=req.instance_id,
                event_date=req.event_date,
                event_type=req.event_type,
                amount=req.amount,
                currency=req.currency,
                source=req.source,
                source_event_id=req.source_event_id,
                evidence_sha256=req.evidence_sha256,
                description=req.description,
                status="APPLIED",
                created_at=now,
                applied_at=now,
            )
            session.add(row)
            state.virtual_cash = cash_after
            state.last_update = now
            session.flush()
            journal_id = row.id
            session.commit()
            return CashFlowResponseData(
                journal_id=journal_id,
                execution_domain=req.execution_domain,
                instance_id=req.instance_id,
                event_date=req.event_date,
                amount=req.amount,
                already_applied=False,
                virtual_cash_after=cash_after,
            )
