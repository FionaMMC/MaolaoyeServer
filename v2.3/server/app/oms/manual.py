"""Dashboard manual instructions and dividend registration.

Manual orders bypass the planner and the frozen-target caps but not the order ledger:
they become E-prefixed intents with the same state machine, mirror rows and projection
into the strategy ledger. Every action needs an operator and a reason and is audited.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select

from app.models import DividendEntitlement, InstanceState
from app.oms.cycles import CycleService
from app.oms.models import OmsBrokerSnapshot, OmsManualInstruction, OmsOrder, OmsOverride
from app.oms.planner import PlannedOrder
from app.services.dividend_entitlement import DividendEntitlementService, DividendRequest
from app.services.ledger_transaction import begin_ledger_transaction
from app.settings import HYDRA_LIVE_EXECUTABLE_SYMBOLS

CHINA = timezone(timedelta(hours=8))
MANUAL = "MANUAL"


class ManualOrderIn(BaseModel):
    account_alias: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    reason: str = Field(min_length=4)
    operator: str = Field(min_length=1)
    confirm: Literal[True]

    @field_validator("symbol")
    @classmethod
    def whitelisted(cls, value: str) -> str:
        if value not in HYDRA_LIVE_EXECUTABLE_SYMBOLS:
            raise ValueError("symbol is not one of the nine executable Hydra ETFs")
        return value

    @model_validator(mode="after")
    def lots_and_tick(self):
        if self.side == "BUY" and self.quantity % 100:
            raise ValueError("buy quantity must be a multiple of 100")
        if abs(round(self.limit_price * 1000) - self.limit_price * 1000) > 1e-6:
            raise ValueError("limit price must sit on the 0.001 tick")
        return self


class ManualCancelIn(BaseModel):
    account_alias: str
    client_order_id: str | None = None
    broker_order_id: str | None = None
    reason: str = Field(min_length=4)
    operator: str = Field(min_length=1)
    confirm: Literal[True]

    @model_validator(mode="after")
    def one_target(self):
        if not (self.client_order_id or self.broker_order_id):
            raise ValueError("give client_order_id or broker_order_id")
        return self


class AckItem(BaseModel):
    instruction_id: str
    status: Literal["SUBMITTED", "REJECTED", "UNKNOWN", "CANCEL_REQUESTED", "FAILED", "SKIPPED"]
    detail: str | None = None


class AckIn(BaseModel):
    account_alias: str
    results: list[AckItem]


class DividendIn(BaseModel):
    account_alias: str
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    record_date: str
    ex_date: str
    pay_date: str
    entitled_quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    cash_per_share: Decimal = Field(gt=0, allow_inf_nan=False)
    evidence: str = Field(min_length=8)
    reason: str = Field(min_length=4)
    operator: str = Field(min_length=1)


def _today(now: datetime) -> str:
    return now.astimezone(CHINA).strftime("%Y%m%d")


class ManualService:
    def __init__(self, session_factory, cycles: CycleService):
        self.session_factory = session_factory
        self.cycles = cycles
        self.dividends = DividendEntitlementService(session_factory)

    @staticmethod
    def _instance(session, account_alias: str) -> InstanceState:
        rows = session.execute(select(InstanceState).where(InstanceState.execution_domain == "live",
                                                           InstanceState.account_alias == account_alias)).scalars().all()
        if len(rows) != 1:
            raise ValueError(f"expected exactly one live instance for {account_alias}, found {len(rows)}")
        return rows[0]

    @staticmethod
    def _audit(session, *, account_alias, action, payload, reason, operator, now, effect=None):
        session.add(OmsOverride(account_alias=account_alias, action=action, payload=payload, reason=reason,
                                operator=operator, created_at=now.isoformat(), effect=effect))

    def create_order(self, req: ManualOrderIn, now: datetime) -> dict:
        day = _today(now)
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", req.account_alias)
            instance = self._instance(session, req.account_alias)
            seq = session.execute(select(func.count()).select_from(OmsOrder).where(
                OmsOrder.cycle_id == MANUAL, OmsOrder.trade_date == day)).scalar_one() + 1
            if seq > 999:
                raise ValueError("more than 999 manual orders today")
            coid = f"E{day[2:]}{seq:03d}"
            item = PlannedOrder(req.symbol, req.side, req.quantity, req.limit_price, req.limit_price)
            self.cycles.ledger.add_order(session, client_order_id=coid, session_id=f"{MANUAL}:{day}",
                                         cycle_id=MANUAL, account_alias=req.account_alias,
                                         instance_id=instance.instance_id, trade_date=day, target_id=MANUAL,
                                         plan_sha256=None, attempt_number=None, item=item, now=now.isoformat(),
                                         precheck_reason=f"manual:{req.operator}")
            payload = req.model_dump(mode="json")
            session.add(OmsManualInstruction(instruction_id=coid, account_alias=req.account_alias, trade_date=day,
                                             kind="ORDER", client_order_id=coid, broker_order_id=None,
                                             payload=payload, status="PENDING", reason=req.reason,
                                             operator=req.operator, created_at=now.isoformat()))
            self._audit(session, account_alias=req.account_alias, action="MANUAL_ORDER", payload=payload,
                        reason=req.reason, operator=req.operator, now=now, effect={"client_order_id": coid})
            session.commit()
        return {"client_order_id": coid, "instruction_id": coid, "trade_date": day, "status": "PENDING"}

    def create_cancel(self, req: ManualCancelIn, now: datetime) -> dict:
        day = _today(now)
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", req.account_alias)
            seq = session.execute(select(func.count()).select_from(OmsManualInstruction).where(
                OmsManualInstruction.kind == "CANCEL", OmsManualInstruction.trade_date == day)).scalar_one() + 1
            instruction_id = f"X{day[2:]}{seq:03d}"
            payload = req.model_dump(mode="json")
            session.add(OmsManualInstruction(instruction_id=instruction_id, account_alias=req.account_alias,
                                             trade_date=day, kind="CANCEL", client_order_id=req.client_order_id,
                                             broker_order_id=req.broker_order_id, payload=payload, status="PENDING",
                                             reason=req.reason, operator=req.operator, created_at=now.isoformat()))
            self._audit(session, account_alias=req.account_alias, action="MANUAL_CANCEL", payload=payload,
                        reason=req.reason, operator=req.operator, now=now, effect={"instruction_id": instruction_id})
            session.commit()
        return {"instruction_id": instruction_id, "trade_date": day, "status": "PENDING"}

    def pending(self, account_alias: str, trade_date: str) -> dict:
        with self.session_factory() as session:
            rows = session.execute(select(OmsManualInstruction).where(
                OmsManualInstruction.account_alias == account_alias, OmsManualInstruction.trade_date == trade_date,
                OmsManualInstruction.status == "PENDING").order_by(OmsManualInstruction.created_at,
                                                                    OmsManualInstruction.instruction_id)).scalars()
            out = []
            for row in rows:
                item = {"instruction_id": row.instruction_id, "kind": row.kind, "client_order_id": row.client_order_id,
                        "broker_order_id": row.broker_order_id}
                if row.kind == "ORDER":
                    item.update(symbol=row.payload["symbol"], side=row.payload["side"],
                                quantity=row.payload["quantity"], limit_price=row.payload["limit_price"])
                out.append(item)
        return {"trade_date": trade_date, "instructions": out}

    def ack(self, req: AckIn, now: datetime) -> dict:
        updated = 0
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", req.account_alias)
            for item in req.results:
                row = session.get(OmsManualInstruction, item.instruction_id)
                if row is None or row.account_alias != req.account_alias or row.status != "PENDING":
                    continue
                row.status = "DONE" if item.status in ("SUBMITTED", "CANCEL_REQUESTED") else item.status
                row.acked_at, row.result = now.isoformat(), item.model_dump(mode="json")
                updated += 1
            session.commit()
        return {"updated": updated}

    def dividend(self, req: DividendIn, now: datetime, *, apply: bool) -> dict:
        with self.session_factory() as session:
            instance = self._instance(session, req.account_alias)
        request = DividendRequest(
            execution_domain="live", account_alias=req.account_alias, instance_id=instance.instance_id,
            symbol=req.symbol, record_date=req.record_date, ex_date=req.ex_date, pay_date=req.pay_date,
            entitled_quantity=req.entitled_quantity, cash_per_share=req.cash_per_share,
            evidence_sha256=hashlib.sha256(req.evidence.encode("utf-8")).hexdigest(),
            settlement_source="dashboard", settlement_event_id=f"{req.symbol}-{req.record_date}-{req.pay_date}")
        result = self.dividends.register(request, apply=apply)
        if apply and not result.get("already_registered"):
            with self.session_factory() as session:
                self._audit(session, account_alias=req.account_alias, action="DIVIDEND_REGISTERED",
                            payload=req.model_dump(mode="json"), reason=req.reason, operator=req.operator, now=now,
                            effect={"entitlement_id": result["entitlement_id"], "amount": result["amount"]})
                session.commit()
        return result

    def overview(self, account_alias: str) -> dict:
        status = self.cycles.status(account_alias)
        with self.session_factory() as session:
            instance = self._instance(session, account_alias)
            instructions = session.execute(select(OmsManualInstruction).where(
                OmsManualInstruction.account_alias == account_alias)
                .order_by(OmsManualInstruction.created_at.desc()).limit(50)).scalars().all()
            overrides = session.execute(select(OmsOverride).where(OmsOverride.account_alias == account_alias)
                                        .order_by(OmsOverride.id.desc()).limit(50)).scalars().all()
            dividends = session.execute(select(DividendEntitlement).where(
                DividendEntitlement.instance_id == instance.instance_id)
                .order_by(DividendEntitlement.pay_date.desc()).limit(20)).scalars().all()
            snap = session.execute(select(OmsBrokerSnapshot).where(OmsBrokerSnapshot.account_alias == account_alias)
                                   .order_by(OmsBrokerSnapshot.taken_at.desc())).scalars().first()
            manual_orders = session.execute(select(OmsOrder).where(
                OmsOrder.account_alias == account_alias, OmsOrder.cycle_id == MANUAL)
                .order_by(OmsOrder.created_at.desc()).limit(50)).scalars().all()
            return {
                "status": status,
                "ledger": {"instance_id": instance.instance_id, "cash": float(instance.virtual_cash),
                           "positions": {k: int(v) for k, v in (instance.virtual_positions or {}).items() if int(v)},
                           "last_update": instance.last_update},
                "manual_orders": [{"client_order_id": o.client_order_id, "trade_date": o.trade_date,
                                   "symbol": o.symbol, "side": o.side, "quantity": o.quantity,
                                   "limit_price": o.limit_price, "state": o.state, "filled_qty": o.filled_qty,
                                   "avg_price": o.avg_price} for o in manual_orders],
                "instructions": [{"instruction_id": r.instruction_id, "trade_date": r.trade_date, "kind": r.kind,
                                  "client_order_id": r.client_order_id, "broker_order_id": r.broker_order_id,
                                  "status": r.status, "reason": r.reason, "operator": r.operator,
                                  "created_at": r.created_at, "result": r.result} for r in instructions],
                "overrides": [{"id": r.id, "action": r.action, "operator": r.operator, "reason": r.reason,
                               "created_at": r.created_at, "effect": r.effect} for r in overrides],
                "dividends": [{"symbol": d.symbol, "record_date": d.record_date, "ex_date": d.ex_date,
                               "pay_date": d.pay_date, "entitled_quantity": d.entitled_quantity,
                               "cash_per_share": d.cash_per_share, "amount": d.amount,
                               "settlement_event_id": d.settlement_event_id} for d in dividends],
                "latest_snapshot": None if snap is None else {
                    "kind": snap.kind, "taken_at": snap.taken_at, "available_cash": snap.payload["available_cash"],
                    "positions": snap.payload["positions"],
                    "open_orders": [o for o in snap.payload["orders"] if o["status"] in (48, 49, 50, 51, 52, 55)]},
                "symbols": sorted(HYDRA_LIVE_EXECUTABLE_SYMBOLS),
            }
