"""Persist broker cash events even when trading or attribution is unavailable."""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.exceptions import APIError, ErrorCode
from app.models.account_cash_observation import AccountCashObservation
from app.schemas.account_cash_observation import (
    AccountCashObservationRequest,
    AccountCashObservationResponseData,
)


class AccountCashObservationService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def record(self, req: AccountCashObservationRequest) -> AccountCashObservationResponseData:
        payload = req.model_dump(mode="json")
        for field in ("amount", "qmt_cash_balance", "qmt_available_cash"):
            value = getattr(req, field)
            if value is not None:
                payload[field] = format(value.quantize(Decimal("0.01")), "f")
        digest = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        identity = select(AccountCashObservation).where(
            AccountCashObservation.execution_domain == req.execution_domain,
            AccountCashObservation.account_alias == req.account_alias,
            AccountCashObservation.source == req.source,
            AccountCashObservation.source_event_id == req.source_event_id,
        )
        with self.session_factory() as session:
            existing = session.execute(identity).scalar_one_or_none()
            if existing is not None:
                return self._replay(existing, digest)
            row = AccountCashObservation(
                execution_domain=req.execution_domain,
                account_alias=req.account_alias,
                source=req.source, source_event_id=req.source_event_id,
                event_type=req.event_type, amount=float(req.amount),
                observed_at=req.observed_at, evidence_sha256=req.evidence_sha256,
                request_sha256=digest, payload=payload,
                recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            session.add(row)
            try:
                session.flush()
                result = self._response(row, replay=False)
                session.commit()
                return result
            except IntegrityError:
                # A concurrent delivery of the same source event is a replay,
                # not an unhandled server error. Do not suppress other failures.
                session.rollback()
                existing = session.execute(identity).scalar_one_or_none()
                if existing is None:
                    raise
                return self._replay(existing, digest)

    @classmethod
    def _replay(cls, row, digest):
        if row.request_sha256 != digest:
            raise APIError(
                ErrorCode.BAD_REQUEST,
                "同一 QMT 现金事实内容不同；保留原事实，请用新的冲正/更正事件",
                http_status=409,
            )
        return cls._response(row, replay=True)

    @staticmethod
    def _response(row, *, replay):
        return AccountCashObservationResponseData(
            observation_id=row.id, execution_domain=row.execution_domain,
            account_alias=row.account_alias, source=row.source,
            source_event_id=row.source_event_id, evidence_sha256=row.evidence_sha256,
            request_sha256=row.request_sha256, already_recorded=replay,
        )
