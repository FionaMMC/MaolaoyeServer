"""Explicit record-date rights and exact cash-event matching; never create cash.

An approved manifest supplies record-date ownership evidence. Current holdings
are deliberately not used: selling after record date does not erase the right.
There is no guess based on a price drop, a payment schedule, or account balance.
"""

import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from app.models import CashFlowJournal, DividendEntitlement, InstanceState
from app.services.ledger_transaction import begin_ledger_transaction


class DividendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_domain: Literal["paper", "live"]
    account_alias: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    record_date: str
    ex_date: str
    pay_date: str
    entitled_quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    cash_per_share: Decimal = Field(gt=0, allow_inf_nan=False)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    settlement_source: str = Field(min_length=1)
    settlement_event_id: str = Field(min_length=1)

    @field_validator("record_date", "ex_date", "pay_date")
    @classmethod
    def valid_date(cls, value):
        if len(value) != 8 or not value.isdigit():
            raise ValueError("date must be YYYYMMDD")
        datetime.strptime(value, "%Y%m%d")
        return value

    @model_validator(mode="after")
    def ordered_dates(self):
        if not self.record_date <= self.ex_date <= self.pay_date:
            raise ValueError("expected record_date <= ex_date <= pay_date")
        return self

    def record(self):
        payload = self.model_dump(mode="json")
        for key in ("entitled_quantity", "cash_per_share"):
            payload[key] = format(getattr(self, key).normalize(), "f")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        identity = [
            self.execution_domain,
            self.instance_id,
            self.symbol,
            self.record_date,
            self.ex_date,
        ]
        return payload | {
            "entitlement_id": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
            "amount": str(
                (self.entitled_quantity * self.cash_per_share).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            ),
            "request_sha256": digest,
        }


def _outstanding_right(session, right, date):
    if right.ex_date > str(date):
        return Decimal(0)
    domain, instance_id = right.execution_domain, right.instance_id
    cash = session.scalar(
        select(CashFlowJournal).where(
            CashFlowJournal.execution_domain == domain,
            CashFlowJournal.account_alias == right.account_alias,
            CashFlowJournal.source == right.settlement_source,
            CashFlowJournal.source_event_id == right.settlement_event_id,
            CashFlowJournal.status == "APPLIED",
        )
    )
    if cash is not None:
        paid = Decimal(str(cash.amount))
        if (
            cash.instance_id != instance_id
            or cash.event_type != "DIVIDEND"
            or cash.currency != "CNY"
            or cash.event_date < right.ex_date
            or not paid.is_finite()
            or paid.quantize(Decimal("0.01")) != Decimal(right.amount)
        ):
            raise ValueError(
                "dividend settlement differs from entitlement; reconcile valuation only"
            )
        if cash.event_date <= str(date):
            return Decimal(0)  # already in cash; do not count income twice
    return Decimal(right.amount)


def outstanding_dividends(session, instance_id, domain, date):
    rights = session.scalars(
        select(DividendEntitlement).where(
            DividendEntitlement.instance_id == instance_id,
            DividendEntitlement.execution_domain == domain,
            DividendEntitlement.ex_date <= str(date),
        )
    ).all()
    return sum((_outstanding_right(session, right, date) for right in rights), Decimal(0))


class DividendEntitlementService:
    def __init__(self, session_factory):
        self.sf = session_factory

    def register(self, req: DividendRequest, *, apply=False):
        values = req.record()
        with self.sf() as session:
            if apply:
                begin_ledger_transaction(session, req.execution_domain, req.account_alias)
            state = session.get(InstanceState, req.instance_id)
            if state is None or state.execution_domain != req.execution_domain:
                raise ValueError("entitlement instance/domain mismatch")
            if state.account_alias not in (None, req.account_alias):
                raise ValueError("entitlement account mismatch")
            existing = session.get(DividendEntitlement, values["entitlement_id"])
            if existing is not None:
                if existing.request_sha256 != values["request_sha256"]:
                    raise ValueError("entitlement already registered with different evidence/terms")
                return values | {"already_registered": True, "applied": apply}
            reused = session.scalar(
                select(DividendEntitlement).where(
                    DividendEntitlement.execution_domain == req.execution_domain,
                    DividendEntitlement.account_alias == req.account_alias,
                    DividendEntitlement.settlement_source == req.settlement_source,
                    DividendEntitlement.settlement_event_id == req.settlement_event_id,
                )
            )
            if reused:
                raise ValueError("cash event already assigned to another entitlement")
            candidate = DividendEntitlement(**values)
            _outstanding_right(session, candidate, req.pay_date)
            if apply:
                session.add(candidate)
                session.commit()
        return values | {"already_registered": False, "applied": apply}
