"""Single writer of execution-core order state, and its projection into the strategy ledger.

Order state changes only through ``transition``; illegal observations are recorded,
never raised and never applied. Fills reach the attributed strategy ledger through
the existing SettlementService via legacy mirror rows (status OMS_ROUTED, which the
legacy GET /orders never serves), so dashboards and NAV keep their single source.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re
import uuid

from sqlalchemy import func, select

from app.models import Order, OrderSignalMap, RawSignal
from app.oms.models import OmsCycle, OmsFill, OmsOrder, OmsOrderEvent, OmsSession
from app.oms.planner import PlannedOrder
from app.oms.schemas import BrokerOrder, BrokerTrade, EventIn
from app.oms.states import TERMINAL, IllegalTransition, OrderState as S, classify_qmt, legacy_status, transition
from app.schemas.trade_result import TradeResult
from app.services.ledger_transaction import begin_ledger_transaction

logger = logging.getLogger(__name__)

CHINA = timezone(timedelta(hours=8))
MIRROR_STATUS = "OMS_ROUTED"
OPEN_QMT_STATUSES = {48, 49, 50, 51, 52, 55, 255}
# H = cycle orders, E = dashboard manual orders.
_CLIENT_ID = re.compile(r"^[HE]\d{9}$")
_EVENT_STATE = {"SUBMIT_STARTED": S.SUBMITTING, "ACKED": S.ACKED,
                "SUBMIT_REJECTED": S.REJECTED, "SUBMIT_UNKNOWN": S.UNKNOWN}


def client_order_id(cycle_no: int, seq: int, leg: int) -> str:
    """Ten characters, so a 24-character QMT remark can never truncate it."""
    return f"H{cycle_no:05d}{seq:02d}{leg:02d}"


class OrderLedger:
    def __init__(self, session_factory, settlement):
        self.session_factory = session_factory
        self.settlement = settlement

    # ── planning ──────────────────────────────────────────────────────────
    def create_orders(self, session, *, cycle: OmsCycle, oms_session: OmsSession,
                      planned: list[PlannedOrder], now: str) -> list[OmsOrder]:
        """Add cycle intents plus legacy mirror rows inside the caller's transaction."""
        existing = session.execute(select(func.count()).select_from(OmsOrder)
                                   .where(OmsOrder.session_id == oms_session.session_id)).scalar_one()
        return [self.add_order(session, client_order_id=client_order_id(cycle.cycle_no, oms_session.seq, leg),
                               session_id=oms_session.session_id, cycle_id=cycle.cycle_id,
                               account_alias=cycle.account_alias, instance_id=cycle.instance_id,
                               trade_date=oms_session.trade_date, target_id=cycle.target_version_id,
                               plan_sha256=oms_session.plan_sha256, attempt_number=oms_session.seq,
                               item=item, now=now)
                for leg, item in enumerate(planned, start=existing + 1)]

    @staticmethod
    def add_order(session, *, client_order_id: str, session_id: str, cycle_id: str, account_alias: str,
                  instance_id: str, trade_date: str, target_id: str, plan_sha256: str | None,
                  attempt_number: int | None, item: PlannedOrder, now: str, precheck_reason: str = "oms_planner"
                  ) -> OmsOrder:
        """One intent plus its legacy mirror (status OMS_ROUTED is never served by GET /orders)."""
        limit = round(item.limit_price, 3)
        row = OmsOrder(client_order_id=client_order_id, session_id=session_id, cycle_id=cycle_id,
                       account_alias=account_alias, trade_date=trade_date, symbol=item.symbol, side=item.side,
                       quantity=int(item.quantity), limit_price=limit, reference_price=float(item.reference_price),
                       state=S.PLANNED.value, filled_qty=0, avg_price=0., projected_qty=0, created_at=now,
                       updated_at=now)
        session.add(row)
        signal_id = uuid.uuid5(uuid.NAMESPACE_URL, client_order_id).hex
        session.add(RawSignal(
            signal_id=signal_id, execution_domain="live", instance_id=instance_id, symbol=item.symbol,
            direction=item.side, quantity=int(item.quantity), reference_price=float(item.reference_price),
            price_offset=limit / float(item.reference_price) - 1, limit_price=limit, valid_date=trade_date,
            signal_time=now, precheck_status="PASS", precheck_reason=precheck_reason))
        session.add(Order(
            order_id=client_order_id, execution_domain="live", qmt_account_alias=account_alias,
            target_id=target_id, rebalance_id=cycle_id, attempt_id=None, attempt_number=attempt_number,
            batch_id=session_id, batch_sha256=plan_sha256, execution_reference_price=float(item.reference_price),
            account_group=account_alias, symbol=item.symbol, direction=item.side, quantity=int(item.quantity),
            limit_price=limit, valid_date=trade_date, status=MIRROR_STATUS, created_at=now))
        session.add(OrderSignalMap(order_id=client_order_id, signal_id=signal_id, signal_quantity=int(item.quantity)))
        return row

    # ── agent journal events ──────────────────────────────────────────────
    def apply_events(self, account_alias: str, events: list[EventIn], now: str) -> dict:
        applied, duplicate, rejected = 0, 0, {}
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", account_alias)
            for event in events:
                if session.get(OmsOrderEvent, event.event_id) is not None:
                    duplicate += 1
                    continue
                ok, error = False, None
                order = session.get(OmsOrder, event.client_order_id)
                if order is None or order.account_alias != account_alias:
                    error = "UNKNOWN_ORDER_FOR_ACCOUNT"
                else:
                    if event.broker_order_id:
                        if order.broker_order_id is None:
                            order.broker_order_id = event.broker_order_id
                        elif order.broker_order_id != event.broker_order_id:
                            error = "BROKER_ORDER_ID_CONFLICT"
                    if error is None and event.kind == "CANCEL_REQUESTED":
                        ok = True
                    elif error is None:
                        try:
                            order.state = transition(S(order.state), _EVENT_STATE[event.kind]).value
                            order.updated_at = now
                            ok = True
                        except IllegalTransition as exc:
                            error = f"ILLEGAL_TRANSITION {exc}"
                session.add(OmsOrderEvent(event_id=event.event_id, client_order_id=event.client_order_id,
                                          kind=event.kind, payload=event.model_dump(mode="json"),
                                          observed_at=event.observed_at.isoformat(), received_at=now,
                                          applied=ok, error=error))
                if ok:
                    applied += 1
                else:
                    rejected[event.event_id] = error
            session.commit()
        return {"applied": applied, "duplicate": duplicate, "rejected": rejected}

    # ── broker facts ──────────────────────────────────────────────────────
    def observe_orders(self, account_alias: str, orders: list[BrokerOrder], observed_at: datetime) -> dict:
        matched, external, conflicts = 0, [], []
        stamp = observed_at.isoformat()
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", account_alias)
            for broker in orders:
                remark = broker.remark.strip()
                order = session.get(OmsOrder, remark) if _CLIENT_ID.match(remark) else None
                if order is None or order.account_alias != account_alias:
                    external.append({"broker_order_id": broker.broker_order_id, "symbol": broker.symbol,
                                     "side": broker.side, "status": broker.status, "remark": remark,
                                     "open": broker.status in OPEN_QMT_STATUSES})
                    continue
                if order.broker_order_id is None:
                    order.broker_order_id = broker.broker_order_id
                elif order.broker_order_id != broker.broker_order_id:
                    conflicts.append({"client_order_id": order.client_order_id, "reason": "DUPLICATE_BROKER_ORDER",
                                      "broker_order_id": broker.broker_order_id})
                    continue
                self._merge_observation(order, broker, stamp, conflicts)
                matched += 1
            session.commit()
        if conflicts:
            logger.error("oms observe conflicts account=%s %s", account_alias, conflicts)
        return {"matched": matched, "external": external, "conflicts": conflicts}

    @staticmethod
    def _merge_observation(order: OmsOrder, broker: BrokerOrder, stamp: str, conflicts: list,
                           forced: S | None = None) -> None:
        if broker.traded_volume > order.filled_qty:
            order.filled_qty = int(broker.traded_volume)
            order.avg_price = float(broker.traded_price)
        observed = forced or classify_qmt(broker.status, order.filled_qty, order.quantity)
        try:
            order.state = transition(S(order.state), observed).value
        except IllegalTransition as exc:
            conflicts.append({"client_order_id": order.client_order_id, "reason": f"ILLEGAL_TRANSITION {exc}",
                              "broker_order_id": broker.broker_order_id})
        order.last_observed_at = stamp
        order.updated_at = stamp

    def record_trades(self, account_alias: str, trades: list[BrokerTrade], now: str) -> int:
        inserted = 0
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", account_alias)
            for trade in trades:
                known = session.execute(select(OmsFill.id).where(
                    OmsFill.account_alias == account_alias, OmsFill.broker_trade_id == trade.broker_trade_id)).first()
                if known is not None:
                    continue
                remark = trade.remark.strip()
                order = session.get(OmsOrder, remark) if _CLIENT_ID.match(remark) else None
                if order is None:
                    order = session.execute(select(OmsOrder).where(
                        OmsOrder.account_alias == account_alias,
                        OmsOrder.broker_order_id == trade.broker_order_id)).scalar_one_or_none()
                session.add(OmsFill(account_alias=account_alias, broker_trade_id=trade.broker_trade_id,
                                    client_order_id=order.client_order_id if order is not None else None,
                                    symbol=trade.symbol, side=trade.side, quantity=trade.quantity,
                                    price=trade.price, traded_at=trade.traded_at, received_at=now))
                inserted += 1
            session.commit()
        return inserted

    def finalize_day(self, account_alias: str, trade_date: str, orders: list[BrokerOrder],
                     observed_at: datetime) -> dict:
        """After 15:00 every order of the day reaches a terminal state or is reported as an anomaly."""
        local = observed_at.astimezone(CHINA)
        if local.strftime("%Y%m%d") != trade_date or local.hour < 15:
            raise ValueError("finalize_day requires a same-day observation at or after 15:00 China time")
        by_remark = {}
        for broker in orders:
            by_remark.setdefault(broker.remark.strip(), broker)
        finalized, anomalies, stamp = [], [], observed_at.isoformat()
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", account_alias)
            open_orders = session.execute(select(OmsOrder).where(
                OmsOrder.account_alias == account_alias, OmsOrder.trade_date == trade_date,
                OmsOrder.state.not_in([state.value for state in TERMINAL]))).scalars().all()
            for order in open_orders:
                broker = by_remark.get(order.client_order_id)
                state = S(order.state)
                if broker is None:
                    if state in (S.PLANNED, S.SUBMITTING, S.UNKNOWN):
                        order.state = transition(state, S.NOT_SUBMITTED).value
                        order.updated_at = stamp
                        finalized.append(order.client_order_id)
                    else:
                        anomalies.append({"client_order_id": order.client_order_id,
                                          "reason": "MISSING_FROM_BROKER_DAY_LIST"})
                    continue
                observed = classify_qmt(broker.status, max(order.filled_qty, broker.traded_volume), order.quantity)
                if observed is S.UNKNOWN:
                    anomalies.append({"client_order_id": order.client_order_id, "reason": "BROKER_STATUS_UNKNOWN"})
                    continue
                # Day orders end at 15:00; QMT keeps showing 50/55 (observed 2026-09-04).
                forced = S.EXPIRED_DAY if observed in (S.ACKED, S.PARTIAL, S.PENDING_CANCEL) else None
                if order.broker_order_id is None:
                    order.broker_order_id = broker.broker_order_id
                self._merge_observation(order, broker, stamp, anomalies, forced=forced)
                if S(order.state) in TERMINAL:
                    finalized.append(order.client_order_id)
            session.commit()
        return {"finalized": finalized, "anomalies": anomalies}

    # ── projection into the strategy ledger ───────────────────────────────
    def project(self, account_alias: str, trade_date: str) -> dict:
        with self.session_factory() as session:
            rows = session.execute(select(OmsOrder).where(
                OmsOrder.account_alias == account_alias, OmsOrder.trade_date == trade_date)).scalars().all()
            results = []
            for order in rows:
                status = legacy_status(S(order.state), order.filled_qty)
                if status is None or (order.filled_qty, status) == (order.projected_qty, order.projected_status):
                    continue
                not_submitted = status == "NOT_SUBMITTED"
                results.append(TradeResult(
                    order_id=order.client_order_id, filled_quantity=order.filled_qty,
                    filled_price=order.avg_price if order.filled_qty else 0.0,
                    filled_time=order.last_observed_at, status=status, symbol=order.symbol, direction=order.side,
                    qmt_order_id=None if not_submitted else order.broker_order_id,
                    not_submitted_reason="EXECUTION_WINDOW_EXPIRED" if not_submitted else None))
        if not results:
            return {"matched_count": 0, "unmatched_order_ids": [], "rejected_observations": {}}
        response = self.settlement.settle(trade_date, results, "live", (account_alias,))
        refused = set(response.unmatched_order_ids) | set(response.rejected_observations)
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", account_alias)
            for result in results:
                if result.order_id in refused:
                    continue
                order = session.get(OmsOrder, result.order_id)
                order.projected_qty = result.filled_quantity
                order.projected_status = result.status
            session.commit()
        if refused:
            logger.error("oms projection refused account=%s %s", account_alias, sorted(refused))
        return response.model_dump()
