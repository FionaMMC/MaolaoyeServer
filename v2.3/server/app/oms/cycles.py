"""Snapshot-driven rebalance cycles: publish → approve → (reconcile → finalize → plan)* → close.

Planning happens only when a broker snapshot passes reconciliation; there is no
chain of timers. Every plan is computed from the attributed strategy ledger after
projecting broker facts, capped by the frozen target of the cycle.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

from sqlalchemy import func, select

from app.models import InstanceState
from app.oms.ledger import OPEN_QMT_STATUSES as OPEN_BROKER_STATUSES, OrderLedger
from app.oms.models import (OmsBrokerSnapshot, OmsCycle, OmsOrder, OmsReconciliation, OmsSession,
                            OmsTargetVersion)
from app.oms.planner import Policy, Session, lot_target, plan_buy_session, plan_sell_session, session_schedule
from app.oms.schemas import PlanOrder, PlanOut, SnapshotIn
from app.services.ledger_transaction import begin_ledger_transaction

logger = logging.getLogger(__name__)

CHINA = timezone(timedelta(hours=8))
OPEN_STATUSES = ("PENDING_APPROVAL", "ACTIVE", "HELD")


class CycleConflict(ValueError):
    """A cycle is still open for this account; a new target must wait for it to close."""


def _sha(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _date(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(CHINA).strftime("%Y%m%d")


class CycleService:
    def __init__(self, session_factory, ledger: OrderLedger, reconcile, policy: Policy = Policy()):
        self.session_factory = session_factory
        self.ledger = ledger
        self.reconcile = reconcile
        self.policy = policy

    # ── publishing ────────────────────────────────────────────────────────
    def publish_target(self, *, instance_id: str, account_alias: str, signal_date: str, weights: dict,
                       signal_closes: dict, calendar: list, source_sha256: str, now: str) -> dict:
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", account_alias)
            if self._open_cycle(session, account_alias) is not None:
                raise CycleConflict(f"open cycle exists for {account_alias}")
            inst = session.get(InstanceState, instance_id)
            if inst is None or inst.execution_domain != "live" or inst.account_alias != account_alias:
                raise ValueError(f"unknown live instance {instance_id} for {account_alias}")
            positions = {s: int(q) for s, q in (inst.virtual_positions or {}).items() if int(q)}
            missing = sorted({s for s, w in weights.items() if w > 0} | set(positions))
            missing = [s for s in missing if s not in signal_closes]
            if missing:
                raise ValueError(f"signal closes missing for {missing}")
            cash = float(inst.virtual_cash)
            nav = cash + sum(q * float(signal_closes[s]) for s, q in positions.items())
            frozen = lot_target(nav, weights, signal_closes, self.policy)
            schedule = session_schedule(calendar, signal_date, self.policy.window)
            if _date(now) > schedule[0].trade_date:
                raise ValueError("execution schedule already started")
            lot_gap = sum(max(0., (1 - self.policy.reserve) * w - frozen[s] * float(signal_closes[s]) / nav)
                          for s, w in weights.items())
            version_id = "tv_" + _sha({"instance": instance_id, "signal": signal_date, "weights": weights,
                                       "source": source_sha256})[:24]
            for older in session.execute(select(OmsTargetVersion).where(
                    OmsTargetVersion.instance_id == instance_id, OmsTargetVersion.status == "ACTIVE")).scalars():
                older.status = "SUPERSEDED"
            session.add(OmsTargetVersion(target_version_id=version_id, instance_id=instance_id,
                                         account_alias=account_alias, signal_date=signal_date, weights=dict(weights),
                                         signal_closes=dict(signal_closes), calendar=sorted(set(calendar)),
                                         source_sha256=source_sha256, status="ACTIVE", created_at=now))
            cycle_no = (session.execute(select(func.max(OmsCycle.cycle_no))).scalar() or 0) + 1
            cycle = OmsCycle(cycle_id=f"C{cycle_no:05d}", cycle_no=cycle_no, target_version_id=version_id,
                             account_alias=account_alias, instance_id=instance_id, policy=asdict(self.policy),
                             nav_at_signal=nav, frozen_target=frozen,
                             sell_anchor={s: float(signal_closes[s]) for s in frozen}, buy_anchor=None,
                             lot_gap=lot_gap, schedule=[asdict(x) for x in schedule], status="PENDING_APPROVAL",
                             created_at=now)
            session.add(cycle)
            session.flush()
            first = self._plan_session(session, cycle, schedule[0], positions=positions, sellable=positions,
                                       cash=cash, tradable=set(), basis=None, now=now)
            session.commit()
            shares = [{"symbol": s, "weight": float(weights.get(s, 0.)), "close": float(signal_closes[s]),
                       "target": frozen.get(s, 0), "held": positions.get(s, 0),
                       "delta": frozen.get(s, 0) - positions.get(s, 0)} for s in sorted(set(frozen) | set(positions))]
            return {"cycle_id": cycle.cycle_id, "target_version_id": version_id, "status": cycle.status,
                    "nav": nav, "lot_gap": lot_gap, "schedule": cycle.schedule, "shares": shares,
                    "first_session": first}

    def approve(self, cycle_id: str, approver: str, now: str) -> None:
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", None)
            cycle = session.get(OmsCycle, cycle_id)
            if cycle is None or cycle.status != "PENDING_APPROVAL":
                raise ValueError(f"cycle {cycle_id} is not awaiting approval")
            cycle.status, cycle.approved_by, cycle.approved_at = "ACTIVE", approver, now
            session.commit()

    # ── snapshot driven progression ───────────────────────────────────────
    def ingest_snapshot(self, snap: SnapshotIn, now: str) -> dict:
        alias = snap.account_alias
        payload = snap.model_dump(mode="json")
        snapshot_id = _sha(payload)
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", alias)
            if session.get(OmsBrokerSnapshot, snapshot_id) is None:
                session.add(OmsBrokerSnapshot(snapshot_id=snapshot_id, account_alias=alias, kind=snap.kind,
                                              trade_date=snap.trade_date, taken_at=snap.taken_at.isoformat(),
                                              payload=payload, received_at=now))
            session.commit()
        observed = self.ledger.observe_orders(alias, snap.orders, snap.taken_at)
        if snap.trades is not None:
            self.ledger.record_trades(alias, snap.trades, now)
        local = snap.taken_at.astimezone(CHINA)
        end_of_day = snap.kind == "EOD" and local.strftime("%Y%m%d") == snap.trade_date and local.hour >= 15
        finalized = self.ledger.finalize_day(alias, snap.trade_date, snap.orders, snap.taken_at) if end_of_day else None
        projection = self.ledger.project(alias, snap.trade_date)
        discrepancies = self._discrepancies(snap, observed, finalized, projection)
        passed = not discrepancies
        result = {"snapshot_id": snapshot_id,
                  "reconciliation": {"passed": passed, "discrepancies": discrepancies},
                  "planned_sessions": [], "cycle_status": None}
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", alias)
            cycle = self._open_cycle(session, alias)
            session.add(OmsReconciliation(snapshot_id=snapshot_id, cycle_id=cycle.cycle_id if cycle else None,
                                          passed=passed, discrepancies=discrepancies, created_at=now))
            if cycle is None:
                session.commit()
                return result
            if not passed and snap.kind in ("PRE", "EOD") and cycle.status == "ACTIVE":
                cycle.status = "HELD"
                logger.error("oms cycle %s HELD: %s", cycle.cycle_id, discrepancies)
            if passed and end_of_day and cycle.status == "ACTIVE":
                result["planned_sessions"] = self._advance(session, cycle, snap, snapshot_id, now)
            result["cycle_status"] = cycle.status
            session.commit()
        return result

    def _discrepancies(self, snap: SnapshotIn, observed: dict, finalized: dict | None, projection: dict) -> list:
        found = []
        with self.session_factory() as session:
            owned = set()
            for inst in session.execute(select(InstanceState).where(
                    InstanceState.execution_domain == "live", InstanceState.account_alias == snap.account_alias)).scalars():
                owned |= set(inst.owned_symbols or ())
        for order in observed["external"]:
            if order["open"] and order["symbol"] in owned:
                found.append({"type": "UNKNOWN_OPEN_BROKER_ORDER", **order})
        found += [{"type": "ORDER_CONFLICT", **c} for c in observed["conflicts"]]
        if finalized is not None:
            found += [{"type": "FINALIZE_ANOMALY", **a} for a in finalized["anomalies"]]
        for order_id, reason in (projection.get("rejected_observations") or {}).items():
            found.append({"type": "PROJECTION_REFUSED", "client_order_id": order_id, "reason": reason})
        # Available cash excludes what open buy orders freeze; add it back before comparing to the ledger.
        frozen = sum((o.quantity - o.traded_volume) * o.price for o in snap.orders
                     if o.side == "BUY" and o.status in OPEN_BROKER_STATUSES)
        comparable_cash = float(snap.available_cash) + frozen * 1.001 + (5.0 if frozen else 0.)
        recon = self.reconcile.reconcile_total(snap.positions, comparable_cash, snap.taken_at.isoformat(),
                                               cash_tolerance=0.0, execution_domain="live",
                                               account_alias=snap.account_alias)
        found += [{"type": "POSITION_MISMATCH", **m} for m in recon.mismatches]
        if not recon.cash_ok and snap.kind in ("PRE", "EOD"):
            found.append({"type": "CASH_SHORTFALL", "ledger_cash": recon.ledger_cash_total,
                          "broker_cash": recon.qmt_cash})
        return found

    def _advance(self, session, cycle: OmsCycle, snap: SnapshotIn, snapshot_id: str, now: str) -> list:
        for today in session.execute(select(OmsSession).where(
                OmsSession.cycle_id == cycle.cycle_id, OmsSession.trade_date == snap.trade_date,
                OmsSession.status == "PLANNED")).scalars():
            today.status, today.closed_at = "CLOSED", now
        schedule = [Session(**x) for x in cycle.schedule]
        first_sell = schedule[0].trade_date
        if snap.trade_date < first_sell:
            # Before the first sell day nothing is scheduled; its plan was made at publish time.
            return []
        if cycle.buy_anchor is None:
            if snap.trade_date != first_sell:
                cycle.status = "HELD"
                logger.error("oms cycle %s HELD: buy anchor needs the %s close", cycle.cycle_id, first_sell)
                return []
            missing = [s for s in cycle.frozen_target if s not in snap.quotes]
            if missing:
                cycle.status = "HELD"
                logger.error("oms cycle %s HELD: closing quotes missing for %s", cycle.cycle_id, missing)
                return []
            cycle.buy_anchor = {s: float(snap.quotes[s].last_price) for s in cycle.frozen_target}
        remaining = [x for x in schedule if x.trade_date > snap.trade_date]
        if not remaining:
            self._close(session, cycle, "WINDOW_COMPLETE", snap, now)
            return []
        calendar = session.get(OmsTargetVersion, cycle.target_version_id).calendar
        next_day = min(d for d in calendar if d > snap.trade_date)
        inst = session.get(InstanceState, cycle.instance_id)
        positions = {s: int(q) for s, q in (inst.virtual_positions or {}).items() if int(q)}
        cash = min(float(inst.virtual_cash), float(snap.available_cash))
        tradable = {s for s, q in snap.quotes.items() if q.is_trading}
        planned = []
        for spec in remaining:
            if spec.trade_date != next_day:
                continue
            exists = session.execute(select(OmsSession.session_id).where(
                OmsSession.cycle_id == cycle.cycle_id, OmsSession.seq == spec.seq)).first()
            if exists is None:
                # Everything held tonight is sellable tomorrow (T+1); the agent caps at can_use_volume.
                planned.append(self._plan_session(session, cycle, spec, positions=positions, sellable=positions,
                                                  cash=cash, tradable=tradable, basis=snapshot_id, now=now))
        return planned

    def _plan_session(self, session, cycle: OmsCycle, spec: Session, *, positions, sellable, cash, tradable,
                      basis, now) -> dict:
        if spec.phase == "SELL":
            orders, deferrals = plan_sell_session(target=cycle.frozen_target, positions=positions, sellable=sellable,
                                                  sell_anchor=cycle.sell_anchor, attempt=spec.sell_attempt,
                                                  policy=self.policy)
        else:
            orders, deferrals = plan_buy_session(target=cycle.frozen_target, positions=positions, cash=cash,
                                                 buy_anchor=cycle.buy_anchor, tradable=tradable, policy=self.policy)
        plan_sha = _sha([[o.symbol, o.side, o.quantity, round(o.limit_price, 3)] for o in orders]
                        + [cycle.cycle_id, spec.seq])
        row = OmsSession(session_id=f"{cycle.cycle_id}:{spec.seq}", cycle_id=cycle.cycle_id, seq=spec.seq,
                         trade_date=spec.trade_date, phase=spec.phase, sell_attempt=spec.sell_attempt,
                         status="PLANNED", plan_sha256=plan_sha, basis_snapshot_id=basis,
                         deferrals=[asdict(d) for d in deferrals], created_at=now)
        session.add(row)
        session.flush()
        self.ledger.create_orders(session, cycle=cycle, oms_session=row, planned=orders, now=now)
        return {"session_id": row.session_id, "trade_date": row.trade_date, "phase": row.phase,
                "orders": [asdict(o) for o in orders], "deferrals": row.deferrals}

    def _close(self, session, cycle: OmsCycle, reason: str, snap: SnapshotIn, now: str) -> None:
        cycle.status, cycle.close_reason, cycle.closed_at = "CLOSED", reason, now
        session.flush()
        cycle.report = self._build_report(session, cycle, {s: float(q.last_price) for s, q in snap.quotes.items()})

    # ── read side ─────────────────────────────────────────────────────────
    def plan_for(self, account_alias: str, trade_date: str, phase: str, *, executable_flag: bool) -> PlanOut:
        with self.session_factory() as session:
            row = session.execute(select(OmsSession, OmsCycle).join(OmsCycle, OmsCycle.cycle_id == OmsSession.cycle_id)
                                  .where(OmsCycle.account_alias == account_alias,
                                         OmsCycle.status.in_(OPEN_STATUSES),
                                         OmsSession.trade_date == trade_date, OmsSession.phase == phase)
                                  .order_by(OmsSession.seq.desc())).first()
            if row is None:
                raise LookupError(f"no {phase} session on {trade_date} for {account_alias}")
            oms_session, cycle = row
            orders = session.execute(select(OmsOrder).where(OmsOrder.session_id == oms_session.session_id)
                                     .order_by(OmsOrder.client_order_id)).scalars().all()
            return PlanOut(account_alias=account_alias, cycle_id=cycle.cycle_id, session_id=oms_session.session_id,
                           trade_date=trade_date, phase=phase,
                           executable=bool(executable_flag and cycle.status == "ACTIVE"
                                           and oms_session.status == "PLANNED"),
                           plan_sha256=oms_session.plan_sha256, frozen_target=cycle.frozen_target,
                           orders=[PlanOrder(client_order_id=o.client_order_id, symbol=o.symbol, side=o.side,
                                             quantity=o.quantity, limit_price=o.limit_price) for o in orders])

    def report(self, cycle_id: str) -> dict:
        with self.session_factory() as session:
            cycle = session.get(OmsCycle, cycle_id)
            if cycle is None:
                raise LookupError(cycle_id)
            if cycle.report is not None:
                return cycle.report
            snap = session.execute(select(OmsBrokerSnapshot).where(OmsBrokerSnapshot.account_alias == cycle.account_alias)
                                   .order_by(OmsBrokerSnapshot.taken_at.desc())).scalars().first()
            marks = {s: q["last_price"] for s, q in (snap.payload.get("quotes") or {}).items()} if snap else {}
            return self._build_report(session, cycle, marks)

    def _build_report(self, session, cycle: OmsCycle, marks: dict) -> dict:
        inst = session.get(InstanceState, cycle.instance_id)
        positions = {s: int(q) for s, q in (inst.virtual_positions or {}).items()}
        prices = {s: float(marks.get(s) or (cycle.buy_anchor or {}).get(s) or cycle.sell_anchor[s])
                  for s in cycle.frozen_target}
        nav = float(inst.virtual_cash) + sum(q * prices.get(s, 0.) for s, q in positions.items())
        orders = session.execute(select(OmsOrder).where(OmsOrder.cycle_id == cycle.cycle_id)
                                 .order_by(OmsOrder.client_order_id)).scalars().all()
        last_by_symbol = {}
        for order in orders:
            last_by_symbol[(order.symbol, order.side)] = order
        unfinished = {}
        for symbol, target in cycle.frozen_target.items():
            gap = int(target) - positions.get(symbol, 0)
            if gap == 0:
                continue
            side = "BUY" if gap > 0 else "SELL"
            last = last_by_symbol.get((symbol, side))
            reason = last.state if last is not None else "NO_ORDER"
            unfinished[symbol] = {"side": side, "shares": abs(gap), "value": abs(gap) * prices[symbol],
                                  "reason": reason}
        drift = [{"client_order_id": o.client_order_id, "symbol": o.symbol, "side": o.side,
                  "bps": (1 if o.side == "BUY" else -1) * (o.avg_price / o.reference_price - 1) * 1e4}
                 for o in orders if o.filled_qty and o.reference_price]
        return {"cycle_id": cycle.cycle_id, "close_reason": cycle.close_reason, "lot_gap": cycle.lot_gap,
                "exec_underweight": sum(v["value"] for v in unfinished.values() if v["side"] == "BUY") / nav if nav else 0.,
                "exec_overweight": sum(v["value"] for v in unfinished.values() if v["side"] == "SELL") / nav if nav else 0.,
                "unfinished": unfinished, "price_drift_bps": drift, "nav": nav}

    def status(self, account_alias: str) -> dict:
        with self.session_factory() as session:
            cycle = self._open_cycle(session, account_alias) or session.execute(
                select(OmsCycle).where(OmsCycle.account_alias == account_alias)
                .order_by(OmsCycle.cycle_no.desc())).scalars().first()
            if cycle is None:
                return {"cycle": None}
            sessions = session.execute(select(OmsSession).where(OmsSession.cycle_id == cycle.cycle_id)
                                       .order_by(OmsSession.seq)).scalars().all()
            orders = session.execute(select(OmsOrder).where(OmsOrder.cycle_id == cycle.cycle_id)
                                     .order_by(OmsOrder.client_order_id)).scalars().all()
            recon = session.execute(select(OmsReconciliation).where(OmsReconciliation.cycle_id == cycle.cycle_id)
                                    .order_by(OmsReconciliation.id.desc())).scalars().first()
            return {"cycle": {"cycle_id": cycle.cycle_id, "status": cycle.status, "frozen_target": cycle.frozen_target,
                              "lot_gap": cycle.lot_gap, "schedule": cycle.schedule},
                    "sessions": [{"session_id": s.session_id, "trade_date": s.trade_date, "phase": s.phase,
                                  "status": s.status, "deferrals": s.deferrals} for s in sessions],
                    "orders": [{"client_order_id": o.client_order_id, "symbol": o.symbol, "side": o.side,
                                "quantity": o.quantity, "limit_price": o.limit_price, "state": o.state,
                                "filled_qty": o.filled_qty, "avg_price": o.avg_price} for o in orders],
                    "last_reconciliation": None if recon is None else
                    {"passed": recon.passed, "discrepancies": recon.discrepancies, "created_at": recon.created_at}}

    @staticmethod
    def _open_cycle(session, account_alias: str) -> OmsCycle | None:
        return session.execute(select(OmsCycle).where(OmsCycle.account_alias == account_alias,
                                                      OmsCycle.status.in_(OPEN_STATUSES))).scalars().first()
