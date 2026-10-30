"""Audited fee corrections, atomic ownership transfers and manual action resolution.

All operations serialize with settlement; receipts replay before inspecting the
current ledger. Nothing here submits/cancels orders or infers broker facts.
"""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from app.exceptions import APIError, ErrorCode
from app.models import (AccountingReceipt, CashFlowJournal, ExecutionQualityObservation,
                        InstanceState, Order, OrderSignalMap, RawSignal, Trade)
from app.oms.models import OmsBrokerSnapshot
from app.services.ledger_transaction import begin_ledger_transaction
from app.services.strategy_capital import StrategyCapitalService

TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "NOT_SUBMITTED", "EXPIRED_BY_POLICY"}


def _fail(message):
    raise APIError(ErrorCode.BAD_REQUEST, message, http_status=409)


def _money(value):
    number = Decimal(str(value))
    if not number.is_finite():
        _fail("nonfinite ledger cash")
    return number


def _identity(req, kind):
    payload = req.model_dump(mode="json")
    for field in ("amount", "actual_cumulative_fees", "cash_delta", "expected_cash"):
        if field in payload:
            payload[field] = format(_money(payload[field]).quantize(Decimal(".01")), "f")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key = hashlib.sha256(json.dumps([req.execution_domain, req.account_alias, kind, req.event_id]).encode()).hexdigest()
    return key, digest, payload


class AccountingOperationsService:
    def __init__(self, session_factory, *, clock=None):
        self.sf = session_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _owner(session, req, instance_id):
        state = session.get(InstanceState, instance_id)
        if (state is None or state.execution_domain != req.execution_domain
                or state.account_alias != req.account_alias or state.ledger_mode != "attributed"):
            _fail("unknown attributed strategy in this account")
        return state

    def _execute(self, req, kind, operation):
        key, digest, payload = _identity(req, kind)
        with self.sf() as session:
            begin_ledger_transaction(session, req.execution_domain, req.account_alias)
            existing = session.get(AccountingReceipt, key)
            if existing:
                if existing.request_sha256 != digest:
                    _fail("event already recorded with different content")
                return existing.response_payload | {"already_applied": True}
            now = self.clock()
            result = operation(session, now)
            result.update(receipt_id=key, already_applied=False)
            session.add(AccountingReceipt(receipt_id=key, execution_domain=req.execution_domain,
                account_alias=req.account_alias, kind=kind, event_id=req.event_id,
                request_sha256=digest, request_payload=payload, response_payload=result,
                created_at=now.isoformat()))
            session.commit()
            return result

    @staticmethod
    def _journal(session, req, state, amount, event_type, source, source_id, now):
        row = CashFlowJournal(execution_domain=req.execution_domain, account_alias=req.account_alias,
            instance_id=state.instance_id, event_date=now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"),
            event_type=event_type, amount=float(amount), currency="CNY", source=source,
            source_event_id=source_id, evidence_sha256=req.evidence_sha256,
            description=f"{req.operator}: {req.reason}", status="APPLIED", created_at=now.isoformat(),
            applied_at=now.isoformat())
        session.add(row)
        state.virtual_cash = float(_money(state.virtual_cash) + amount)
        state.last_update = now.isoformat()
        session.flush()
        return row.id

    def adjust_fees(self, req):
        def operation(session, now):
            order = session.get(Order, req.order_id)
            if (order is None or order.execution_domain != req.execution_domain or
                    order.qmt_account_alias != req.account_alias):
                _fail("fee correction order/account mismatch")
            if order.status not in TERMINAL or order.bookkeeping_divergence:
                _fail("finalize and reconcile order attribution before fee correction")
            owners = set(session.scalars(select(RawSignal.instance_id).join(OrderSignalMap,
                OrderSignalMap.signal_id == RawSignal.signal_id).where(OrderSignalMap.order_id == req.order_id)))
            if len(owners) != 1:
                _fail("fee correction requires one explicit strategy owner")
            state = self._owner(session, req, next(iter(owners)))
            qty = session.scalar(select(func.max(Trade.filled_quantity)).where(
                Trade.order_id == order.order_id, Trade.execution_domain == req.execution_domain)) or 0
            quality = session.get(ExecutionQualityObservation, order.order_id)
            if qty != req.filled_quantity or quality is None or quality.filled_quantity != qty:
                _fail("cumulative fill quantity changed or estimated fee evidence missing")
            source = f"broker-fee-adjustment:{order.order_id}"
            credits = sum((_money(value) for value in session.scalars(select(CashFlowJournal.amount).where(
                CashFlowJournal.execution_domain == req.execution_domain,
                CashFlowJournal.account_alias == req.account_alias, CashFlowJournal.source == source,
                CashFlowJournal.status == "APPLIED"))), Decimal(0))
            delta = _money(quality.estimated_fees) - req.actual_cumulative_fees - credits
            journal_id = self._journal(session, req, state, delta, "FEE_ADJUSTMENT", source, req.event_id, now)
            if state.virtual_cash < 0:
                data = dict(state.strategy_state or {})
                hold = dict(data.get("accounting_hold") or {})
                hold["fee_deficit_order"] = order.order_id
                data["accounting_hold"] = hold
                state.strategy_state = data
            elif (state.strategy_state or {}).get("accounting_hold", {}).get("fee_deficit_order") == order.order_id:
                data = dict(state.strategy_state or {})
                hold = dict(data["accounting_hold"])
                hold.pop("fee_deficit_order", None)
                data["accounting_hold"] = hold
                state.strategy_state = data
            return {"order_id": order.order_id, "instance_id": state.instance_id,
                "estimated_cumulative_fees": quality.estimated_fees,
                "actual_cumulative_fees": str(req.actual_cumulative_fees), "cash_delta": str(delta),
                "cash_after": state.virtual_cash, "journal_id": journal_id}
        return self._execute(req, "FEE_ADJUSTMENT", operation)

    def transfer_capital(self, req):
        def operation(session, now):
            sender = self._owner(session, req, req.from_instance_id)
            recipient = self._owner(session, req, req.to_instance_id)
            StrategyCapitalService.check_account_attribution(session, req)
            if (sender.strategy_state or {}).get("accounting_hold"):
                _fail("source strategy has an unresolved accounting hold")
            scope = SimpleNamespace(execution_domain=req.execution_domain, account_alias=req.account_alias,
                                    instance_id=sender.instance_id)
            protected = StrategyCapitalService(self.sf)._protected_buy_cash(session, scope)
            if _money(sender.virtual_cash) - req.amount < protected:
                _fail("capital transfer would spend source strategy reserved cash")
            debit = self._journal(session, req, sender, -req.amount, "CAPITAL_DEALLOCATION",
                "strategy-capital-transfer", f"{req.event_id}/debit", now)
            credit = self._journal(session, req, recipient, req.amount, "CAPITAL_ALLOCATION",
                "strategy-capital-transfer", f"{req.event_id}/credit", now)
            return {"from_instance_id": sender.instance_id, "to_instance_id": recipient.instance_id,
                "amount": str(req.amount), "debit_journal_id": debit, "credit_journal_id": credit,
                "from_cash_after": sender.virtual_cash, "to_cash_after": recipient.virtual_cash,
                "target_policy": "NEXT_UNFROZEN_TARGET"}
        return self._execute(req, "CAPITAL_TRANSFER", operation)

    def register_issue(self, req):
        def operation(session, now):
            state = self._owner(session, req, req.instance_id)
            if req.symbol not in set(state.owned_symbols or ()) | set(state.virtual_positions or {}):
                _fail("corporate action symbol is not managed by this strategy")
            data = dict(state.strategy_state or {})
            issues = dict(data.get("corporate_action_issues") or {})
            issues[req.event_id] = {"status": "OPEN", "symbol": req.symbol, "action_type": req.action_type,
                "evidence_sha256": req.evidence_sha256, "reason": req.reason, "created_at": now.isoformat()}
            data["corporate_action_issues"] = issues
            hold = dict(data.get("accounting_hold") or {})
            hold["corporate_action_issues"] = sorted(key for key, item in issues.items() if item["status"] == "OPEN")
            data["accounting_hold"] = hold
            state.strategy_state = data
            return {"issue_id": req.event_id, "instance_id": req.instance_id, "status": "OPEN"}
        return self._execute(req, "CORPORATE_ACTION_ISSUE", operation)

    def resolve_issue(self, req):
        def operation(session, now):
            state = self._owner(session, req, req.instance_id)
            data = dict(state.strategy_state or {})
            issues = dict(data.get("corporate_action_issues") or {})
            issue = dict(issues.get(req.issue_id) or {})
            if issue.get("status") != "OPEN":
                _fail("corporate action issue is not open")
            symbol = issue["symbol"]
            if issue["action_type"] == "DIVIDEND" and req.cash_delta:
                _fail("dividend cash must use observed income allocation and entitlement matching before issue resolution")
            positions = dict(state.virtual_positions or {})
            if positions.get(symbol, 0) != req.expected_quantity or _money(state.virtual_cash) != req.expected_cash:
                _fail("strategy changed after manual review; review the current ledger")
            snapshot = session.get(OmsBrokerSnapshot, req.broker_snapshot_id)
            if snapshot is None or snapshot.account_alias != req.account_alias:
                _fail("missing broker snapshot for this account")
            stamp = datetime.fromisoformat(snapshot.taken_at.replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                _fail("manual resolution needs a fresh zoned broker snapshot")
            age = (now - stamp).total_seconds()
            if age < -5 or age > 300:
                _fail("manual resolution needs a fresh zoned broker snapshot")
            watermark = StrategyCapitalService._physical_watermark(session, req)
            if watermark and stamp < watermark:
                _fail("broker snapshot precedes the latest account facts")
            if session.scalar(select(Order.order_id).where(Order.execution_domain == req.execution_domain,
                    Order.qmt_account_alias == req.account_alias, Order.status.not_in(TERMINAL)).limit(1)):
                _fail("finalize account orders before manual corporate action reconciliation")
            after_qty = req.expected_quantity + req.quantity_delta
            after_cash = req.expected_cash + req.cash_delta
            if after_qty < 0 or after_cash < 0:
                _fail("resolution cannot create negative owned assets")
            peers = session.scalars(select(InstanceState).where(InstanceState.execution_domain == req.execution_domain,
                InstanceState.account_alias == req.account_alias)).all()
            owned_qty = sum(int((peer.virtual_positions or {}).get(symbol, 0)) for peer in peers) + req.quantity_delta
            owned_cash = sum((_money(peer.virtual_cash) for peer in peers), Decimal(0)) + req.cash_delta
            if owned_qty > int(snapshot.payload["positions"].get(symbol, 0)) or owned_cash > _money(snapshot.payload["available_cash"]):
                _fail("resolution exceeds physically observed account assets")
            if after_qty:
                positions[symbol] = after_qty
            else:
                positions.pop(symbol, None)
            state.virtual_positions = positions
            journal = None
            if req.cash_delta:
                journal = self._journal(session, req, state, req.cash_delta, "CORPORATE_ACTION_ADJUSTMENT",
                    "corporate-action-resolution", req.event_id, now)
            state.last_update = now.isoformat()
            issue.update(status="RESOLVED", resolution_event_id=req.event_id,
                resolution_evidence_sha256=req.evidence_sha256, resolved_at=now.isoformat())
            issues[req.issue_id] = issue
            data["corporate_action_issues"] = issues
            hold = dict(data.get("accounting_hold") or {})
            open_ids = sorted(key for key, item in issues.items() if item["status"] == "OPEN")
            if open_ids:
                hold["corporate_action_issues"] = open_ids
            else:
                hold.pop("corporate_action_issues", None)
            data["accounting_hold"] = hold
            state.strategy_state = data
            return {"issue_id": req.issue_id, "instance_id": req.instance_id, "status": "RESOLVED",
                "quantity_after": after_qty, "cash_after": float(after_cash), "journal_id": journal}
        return self._execute(req, "CORPORATE_ACTION_RESOLUTION", operation)
