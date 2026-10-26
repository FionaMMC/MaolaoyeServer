"""Snapshot-driven rebalance cycles: publish → approve → (reconcile → finalize → plan)* → close.

Planning happens only when a broker snapshot passes reconciliation; there is no
chain of timers. Every plan is computed from the attributed strategy ledger after
projecting broker facts, capped by the frozen target of the cycle.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import logging
import math

from sqlalchemy import func, select

from app.models import InstanceState
from app.oms.ledger import OPEN_QMT_STATUSES as OPEN_BROKER_STATUSES, OrderLedger
from app.oms.models import (OmsBrokerSnapshot, OmsCycle, OmsOrder, OmsReconciliation, OmsSession,
                            OmsTargetVersion)
from app.oms.planner import (Policy, Session, lot_target, plan_buy_session, plan_sell_session,
                             session_schedule, policy_from_dict, policy_to_dict, get_instrument as instrument)
from app.oms.schemas import PlanOrder, PlanOut, SnapshotIn, plan_content_sha256
from app.oms.states import TERMINAL, OrderState, transition
from app.oms.account import instance_is_enabled, strategy_resources
from app.oms.instruments import instrument_to_dict
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


SELL_PLAN_CUTOFF = time(14, 45)     # the PRE snapshot that caches the day's sell plan


def earliest_sell_day(now: str) -> str:
    """First day a cycle published at ``now`` can still sell: today before the 14:45 PRE, else tomorrow."""
    local = datetime.fromisoformat(now).astimezone(CHINA)
    return (local if local.time() < SELL_PLAN_CUTOFF else local + timedelta(days=1)).strftime("%Y%m%d")


class CycleService:
    def __init__(self, session_factory, ledger: OrderLedger, reconcile, policy: Policy = Policy(),
                 account_fingerprint: str = ""):
        self.session_factory = session_factory
        self.ledger = ledger
        self.reconcile = reconcile
        self.policy = policy
        self.account_fingerprint = account_fingerprint

    # ── publishing ────────────────────────────────────────────────────────
    def publish_target(self, *, instance_id: str, account_alias: str, signal_date: str, weights: dict,
                       signal_closes: dict, calendar: list, source_sha256: str, now: str,
                       policy: Policy | dict | None = None, metadata: dict | None = None,
                       provenance: dict | None = None, earliest_execution_date: str | None = None,
                       liquidate: bool = False, supersede: bool = False,
                       reference_closes: dict | None = None) -> dict:
        policy = policy_from_dict(policy) if isinstance(policy, dict) else (policy or self.policy)
        if liquidate:
            # An explicit all-cash target (V713 AUX): every held symbol is frozen at zero below.
            if weights:
                raise ValueError("a liquidation target carries no weights")
        else:
            if not weights or any(not math.isfinite(float(w)) or float(w) < 0 for w in weights.values()):
                raise ValueError("target weights must be finite, non-negative and nonempty")
            if abs(sum(float(w) for w in weights.values()) - 1.0) > 1e-6:
                raise ValueError("target weights must sum to one")
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", account_alias)
            inst = session.get(InstanceState, instance_id)
            if inst is None or inst.execution_domain != "live" or inst.account_alias != account_alias:
                raise ValueError(f"unknown live instance {instance_id} for {account_alias}")
            version_id = "tv_" + _sha({"instance": instance_id, "signal": signal_date, "weights": weights,
                                       "source": source_sha256})[:24]
            previous = session.get(OmsTargetVersion, version_id)
            if previous is not None:
                prior_cycle = session.execute(select(OmsCycle).where(
                    OmsCycle.target_version_id == version_id)).scalar_one()
                if (previous.weights != weights or previous.signal_closes != signal_closes
                        or _sha(prior_cycle.policy) != _sha(policy_to_dict(policy))):
                    raise CycleConflict("target id already exists with different prices or policy")
                return self._publication(session, prior_cycle)
            open_cycle = self._open_cycle(session, account_alias, instance_id)
            if open_cycle is not None:
                if not supersede:
                    raise CycleConflict(f"open cycle exists for {instance_id}")
                self._supersede(session, open_cycle, signal_date, signal_closes, now)
            if policy.strategy_id == "v713":
                monthly = session.execute(select(OmsTargetVersion).where(
                    OmsTargetVersion.instance_id == instance_id,
                    OmsTargetVersion.signal_date == signal_date)).scalars().all()
                if monthly:
                    raise CycleConflict("V713 monthly allocation is already frozen; a different allocation needs review")
            positions = {s: int(q) for s, q in (inst.virtual_positions or {}).items() if int(q)}
            if liquidate and not positions:
                raise ValueError("nothing to liquidate: the instance holds no position")
            # Not-yet-listed or suspended symbols carry no close; they may only appear with zero weight.
            signal_closes = {s: float(p) for s, p in signal_closes.items()
                             if p is not None and math.isfinite(float(p)) and float(p) > 0}
            needed = {s for s, w in weights.items() if w > 0} | set(positions)
            missing = sorted(s for s in needed if s not in signal_closes)
            if missing:
                raise ValueError(f"signal closes missing for {missing}")
            cash = float(inst.virtual_cash)
            nav = cash + sum(q * float(signal_closes[s]) for s, q in positions.items())
            if not math.isfinite(nav) or nav <= 0:
                raise ValueError("strategy NAV must be positive")
            frozen = lot_target(nav, weights, signal_closes, policy)
            for symbol in positions:
                frozen.setdefault(symbol, 0)
            # Every security remains managed until its position and outstanding responsibility end.
            specs = {symbol: instrument_to_dict(instrument(symbol, policy)) for symbol, qty in frozen.items()
                     if qty > 0 or positions.get(symbol, 0) > 0}
            # Published late (the runbook fallback), the cycle starts at the next pair still ahead.
            schedule = session_schedule(calendar, signal_date, policy.window,
                                        earliest_sell=max(earliest_sell_day(now), earliest_execution_date or ""))
            lot_gap = sum(max(0., (1 - policy.reserve) * w - frozen[s] * signal_closes.get(s, 0.) / nav)
                          for s, w in weights.items())
            sell_anchor = {s: signal_closes[s] for s in frozen if s in signal_closes}
            if policy.anchor == "previous_close":
                # Option B prices the first session off the close just before it, like every later one.
                before = max(d for d in sorted(set(calendar)) if d < schedule[0].trade_date)
                if before != signal_date:
                    if reference_closes is None:
                        raise ValueError(f"previous-close anchors: the first session needs the {before} closes")
                    missing = sorted(s for s in frozen if not (float(reference_closes.get(s) or 0) > 0))
                    if missing:
                        raise ValueError(f"reference closes missing for {missing}")
                    sell_anchor = {s: float(reference_closes[s]) for s in frozen}
            state = dict(inst.strategy_state or {})
            state["oms_instruments"] = {**state.get("oms_instruments", {}), **specs}
            state["oms_policy"] = policy_to_dict(policy)
            if provenance is not None or metadata is not None:
                state["oms_target_metadata"] = dict(provenance or metadata)
            inst.strategy_state = state
            inst.owned_symbols = sorted(set(inst.owned_symbols or ()) | set(frozen))
            for older in session.execute(select(OmsTargetVersion).where(
                    OmsTargetVersion.instance_id == instance_id, OmsTargetVersion.status == "ACTIVE")).scalars():
                older.status = "SUPERSEDED"
            session.add(OmsTargetVersion(target_version_id=version_id, instance_id=instance_id,
                                         account_alias=account_alias, signal_date=signal_date, weights=dict(weights),
                                         signal_closes=dict(signal_closes), calendar=sorted(set(calendar)),
                                         source_sha256=source_sha256, status="ACTIVE", created_at=now))
            cycle_no = (session.execute(select(func.max(OmsCycle.cycle_no))).scalar() or 0) + 1
            cycle = OmsCycle(cycle_id=f"C{cycle_no:05d}", cycle_no=cycle_no, target_version_id=version_id,
                             account_alias=account_alias, instance_id=instance_id, policy=policy_to_dict(policy),
                             nav_at_signal=nav, frozen_target=frozen,
                             sell_anchor=sell_anchor,
                             buy_anchor=None,
                             lot_gap=lot_gap, schedule=[asdict(x) for x in schedule], status="PENDING_APPROVAL",
                             created_at=now)
            session.add(cycle)
            session.flush()
            first = self._plan_session(session, cycle, schedule[0], positions=positions, sellable=positions,
                                       cash=cash, tradable=set(), basis=None, now=now)
            session.commit()
            shares = [{"symbol": s, "weight": float(weights.get(s, 0.)), "close": signal_closes.get(s),
                       "target": frozen.get(s, 0), "held": positions.get(s, 0),
                       "delta": frozen.get(s, 0) - positions.get(s, 0)} for s in sorted(set(frozen) | set(positions))]
            return {"cycle_id": cycle.cycle_id, "target_version_id": version_id, "status": cycle.status,
                    "nav": nav, "lot_gap": lot_gap, "schedule": cycle.schedule, "shares": shares,
                    "first_session": first}

    @staticmethod
    def _publication(session, cycle: OmsCycle) -> dict:
        target = session.get(OmsTargetVersion, cycle.target_version_id)
        inst = session.get(InstanceState, cycle.instance_id)
        positions = inst.virtual_positions or {}
        return {"cycle_id": cycle.cycle_id, "target_version_id": cycle.target_version_id,
                "status": cycle.status, "nav": cycle.nav_at_signal, "lot_gap": cycle.lot_gap,
                "schedule": cycle.schedule, "already_published": True,
                "shares": [{"symbol": symbol, "weight": target.weights.get(symbol, 0.0),
                            "close": target.signal_closes.get(symbol), "target": quantity,
                            "held": positions.get(symbol, 0), "delta": quantity - positions.get(symbol, 0)}
                           for symbol, quantity in sorted(cycle.frozen_target.items())]}

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
            cycles = self._open_cycles(session, alias)
            result["cycle_statuses"] = {}
            for cycle in cycles or [None]:
                session.add(OmsReconciliation(snapshot_id=snapshot_id, cycle_id=cycle.cycle_id if cycle else None,
                                              passed=passed, discrepancies=discrepancies, created_at=now))
                if cycle is None:
                    continue
                if not passed and snap.kind in ("PRE", "EOD") and cycle.status == "ACTIVE":
                    cycle.status = "HELD"
                    logger.error("oms cycle %s HELD: %s", cycle.cycle_id, discrepancies)
                if passed and end_of_day and cycle.status == "ACTIVE":
                    result["planned_sessions"] += self._advance(session, cycle, snap, snapshot_id, now)
                result["cycle_statuses"][cycle.instance_id] = cycle.status
            if len(cycles) == 1:
                result["cycle_status"] = cycles[0].status
            session.commit()
        return result

    def _discrepancies(self, snap: SnapshotIn, observed: dict, finalized: dict | None, projection: dict) -> list:
        found = []
        with self.session_factory() as session:
            owned = set()
            instances = session.execute(select(InstanceState).where(
                    InstanceState.execution_domain == "live", InstanceState.account_alias == snap.account_alias)).scalars().all()
            for inst in instances:
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
        # The broker freezes notional plus a commission reserve per order, so add back per order.
        frozen = sum(rest * 1.001 + 5.0 for rest in ((o.quantity - o.traded_volume) * o.price for o in snap.orders
                                                      if o.side == "BUY" and o.status in OPEN_BROKER_STATUSES)
                     if rest > 0)
        if snap.frozen_cash is not None:
            frozen = float(snap.frozen_cash)
        elif len(instances) > 1 and any(o.side == "BUY" and o.status in OPEN_BROKER_STATUSES for o in snap.orders):
            found.append({"type": "FROZEN_CASH_UNOBSERVED"})
        comparable_cash = float(snap.available_cash) + frozen
        recon = self.reconcile.reconcile_total(snap.positions, comparable_cash, snap.taken_at.isoformat(),
                                               cash_tolerance=0.0, execution_domain="live",
                                               account_alias=snap.account_alias)
        found += [{"type": "POSITION_MISMATCH", **m} for m in recon.mismatches]
        if not recon.cash_ok:
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
        wanted = [s for s, q in cycle.frozen_target.items() if int(q) > 0]
        if cycle.buy_anchor is None:
            if snap.trade_date != first_sell:
                cycle.status = "HELD"
                logger.error("oms cycle %s HELD: buy anchor needs the %s close", cycle.cycle_id, first_sell)
                return []
            anchors = self._closing_anchors(cycle, snap, wanted)
            if wanted and not anchors:
                # No target has a usable close at all: a data failure, not a suspension.
                cycle.status = "HELD"
                logger.error("oms cycle %s HELD: closing quotes missing for %s", cycle.cycle_id, sorted(wanted))
                return []
            missing = sorted(set(wanted) - set(anchors))
            if missing:
                logger.warning("oms cycle %s: no usable %s close for %s; their buys wait for a later close",
                               cycle.cycle_id, snap.trade_date, missing)
            cycle.buy_anchor = anchors
        else:
            # A target suspended on the sell day takes its anchor from the first usable close after it.
            late = self._closing_anchors(cycle, snap, [s for s in wanted if s not in cycle.buy_anchor])
            if late:
                logger.info("oms cycle %s: late buy anchors from the %s close for %s",
                            cycle.cycle_id, snap.trade_date, sorted(late))
                cycle.buy_anchor = {**cycle.buy_anchor, **late}
        if policy_from_dict(cycle.policy).anchor == "previous_close":
            # Option B: the next session prices off tonight's close; a symbol without one keeps its last anchor.
            closes = self._closing_anchors(cycle, snap, sorted(cycle.frozen_target))
            cycle.sell_anchor = {**cycle.sell_anchor, **closes}
            cycle.buy_anchor = {**(cycle.buy_anchor or {}), **{s: p for s, p in closes.items() if s in wanted}}
        remaining = [x for x in schedule if x.trade_date > snap.trade_date]
        if not remaining:
            self._close(session, cycle, "WINDOW_COMPLETE", snap, now)
            return []
        calendar = session.get(OmsTargetVersion, cycle.target_version_id).calendar
        next_day = min(d for d in calendar if d > snap.trade_date)
        inst = session.get(InstanceState, cycle.instance_id)
        positions = {s: int(q) for s, q in (inst.virtual_positions or {}).items() if int(q)}
        cash = min(float(inst.virtual_cash), float(snap.available_cash))
        # A closing-session status says nothing about next day's suspension.
        tradable = set(cycle.frozen_target)
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

    @staticmethod
    def _closing_anchors(cycle: OmsCycle, snap: SnapshotIn, symbols) -> dict[str, float]:
        """Same-day official closes usable as buy anchors. A symbol without one (suspended that
        day, or no quote) gets no anchor; with instrument specs the close must be an unadjusted
        price stamped at or after 15:00 of the snapshot's trade date."""
        strict = policy_from_dict(cycle.policy).require_instrument_specs
        taken = snap.taken_at.astimezone(CHINA)
        anchors = {}
        for symbol in symbols:
            quote = snap.quotes.get(symbol)
            try:
                price = float(quote.last_price)
                if not math.isfinite(price) or price <= 0:
                    continue
                if strict:
                    stamp = datetime.fromisoformat(quote.source_time)
                    source = stamp.astimezone(CHINA)
                    if (stamp.utcoffset() is None or source.strftime("%Y%m%d") != snap.trade_date
                            or source.time() < time(15, 0) or source > taken
                            or quote.adjustment != "none"
                            or quote.price_source not in {"raw_tick", "raw_daily_close"}):
                        continue
            except (AttributeError, TypeError, ValueError):
                continue
            anchors[symbol] = price
        return anchors

    def _plan_session(self, session, cycle: OmsCycle, spec: Session, *, positions, sellable, cash, tradable,
                      basis, now) -> dict:
        policy = policy_from_dict(cycle.policy)
        if spec.phase == "SELL":
            orders, deferrals = plan_sell_session(target=cycle.frozen_target, positions=positions, sellable=sellable,
                                                  sell_anchor=cycle.sell_anchor, attempt=spec.sell_attempt,
                                                  policy=policy)
        else:
            orders, deferrals = plan_buy_session(target=cycle.frozen_target, positions=positions, cash=cash,
                                                 buy_anchor=cycle.buy_anchor, tradable=tradable, policy=policy)
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

    def close_superseded(self, *, instance_id: str, account_alias: str, signal_date: str, closes: dict,
                         now: str) -> str | None:
        """End the open cycle for a later target that needs no cycle of its own (an AUX month already in cash)."""
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", account_alias)
            cycle = self._open_cycle(session, account_alias, instance_id)
            if cycle is None:
                return None
            self._supersede(session, cycle, signal_date, closes, now)
            session.commit()
            return cycle.cycle_id

    def _supersede(self, session, cycle: OmsCycle, signal_date: str, closes: dict, now: str) -> None:
        """A newer monthly target ends the old cycle's catch-up instead of trading an expired list."""
        target = session.get(OmsTargetVersion, cycle.target_version_id)
        if signal_date <= target.signal_date:
            raise CycleConflict("only a later signal can supersede an open cycle")
        orders = session.execute(select(OmsOrder).where(
            OmsOrder.cycle_id == cycle.cycle_id,
            OmsOrder.state.not_in([state.value for state in TERMINAL]))).scalars().all()
        if any(order.state != OrderState.PLANNED.value for order in orders):
            raise CycleConflict(f"cycle {cycle.cycle_id} still has broker orders in flight")
        for order in orders:
            order.state = transition(OrderState.PLANNED, OrderState.NOT_SUBMITTED).value
            order.updated_at = now
        for planned in session.execute(select(OmsSession).where(
                OmsSession.cycle_id == cycle.cycle_id, OmsSession.status == "PLANNED")).scalars():
            planned.status, planned.closed_at = "CLOSED", now
        self._close(session, cycle, "SUPERSEDED", None, now,
                    marks={s: float(p) for s, p in closes.items() if p is not None})

    def _close(self, session, cycle: OmsCycle, reason: str, snap: SnapshotIn | None, now: str,
               marks: dict | None = None) -> None:
        cycle.status, cycle.close_reason, cycle.closed_at = "CLOSED", reason, now
        session.flush()
        try:
            if marks is None:
                marks = {s: float(q.last_price) for s, q in snap.quotes.items()}
            cycle.report = self._build_report(session, cycle, marks)
        except Exception:  # a report bug must never keep a finished cycle open
            logger.exception("oms cycle %s report failed", cycle.cycle_id)
            cycle.report = {"cycle_id": cycle.cycle_id, "close_reason": reason, "error": "REPORT_FAILED"}

    # ── read side ─────────────────────────────────────────────────────────
    def plan_for(self, account_alias: str, trade_date: str, phase: str, *, executable_flag: bool,
                 instance_id: str | None = None, wire_version: int = 1, now: str | None = None) -> PlanOut:
        with self.session_factory() as session:
            begin_ledger_transaction(session, "live", account_alias)
            query = select(OmsSession, OmsCycle).join(OmsCycle, OmsCycle.cycle_id == OmsSession.cycle_id).where(
                OmsCycle.account_alias == account_alias, OmsCycle.status.in_(OPEN_STATUSES),
                OmsSession.trade_date == trade_date, OmsSession.phase == phase)
            if instance_id is not None:
                query = query.where(OmsCycle.instance_id == instance_id)
            rows = session.execute(query.order_by(OmsCycle.cycle_no, OmsSession.seq)).all()
            if not rows:
                raise LookupError(f"no {phase} session on {trade_date} for {account_alias}")
            if len(rows) != 1:
                raise ValueError("multiple strategy plans: use /plans or specify instance_id")
            oms_session, cycle = rows[0]
            orders = session.execute(select(OmsOrder).where(OmsOrder.session_id == oms_session.session_id)
                                     .order_by(OmsOrder.client_order_id)).scalars().all()
            inst = session.get(InstanceState, cycle.instance_id)
            plan = PlanOut(account_alias=account_alias, instance_id=cycle.instance_id,
                           cycle_id=cycle.cycle_id, session_id=oms_session.session_id,
                           trade_date=trade_date, phase=phase,
                           executable=bool(executable_flag and cycle.status == "ACTIVE"
                                           and oms_session.status == "PLANNED" and instance_is_enabled(inst)),
                           plan_sha256=oms_session.plan_sha256, frozen_target=cycle.frozen_target,
                           orders=[PlanOrder(client_order_id=o.client_order_id, symbol=o.symbol, side=o.side,
                                             quantity=o.quantity, limit_price=o.limit_price) for o in orders])
            if wire_version >= 2:
                return self._grant(session, plan, inst, cycle.policy, orders, now=now)
            # A legacy executor has no strategy resource contract and must never
            # trade a shared account, even if this particular phase has one plan.
            count = session.execute(select(func.count()).select_from(InstanceState).where(
                InstanceState.execution_domain == "live", InstanceState.account_alias == account_alias)).scalar_one()
            if count > 1:
                plan.executable = False
            return plan

    def _grant(self, session, plan: PlanOut, inst: InstanceState, policy: dict, orders: list[OmsOrder],
               *, now: str | None = None) -> PlanOut:
        local = datetime.fromisoformat(now).astimezone(CHINA) if now else datetime.now(CHINA)
        frozen_policy = policy_to_dict(policy_from_dict(policy))
        from app.settings import HYDRA_LIVE_EXECUTABLE_SYMBOLS
        needed = {symbol for symbol, qty in plan.frozen_target.items()
                  if qty > 0 or symbol in policy.get("instruments", {}) or symbol in HYDRA_LIVE_EXECUTABLE_SYMBOLS}
        needed |= {order.symbol for order in orders}
        specs = {symbol: instrument_to_dict(instrument(symbol, policy_from_dict(policy))) for symbol in needed}
        windows = frozen_policy.get("execution_windows", {})
        end_time = (windows.get(plan.phase) or ["09:30:00", "14:55:00"])[1]
        valid_until = datetime.strptime(plan.trade_date + " " + end_time, "%Y%m%d %H:%M:%S").replace(tzinfo=CHINA)
        lease = min(local + timedelta(minutes=20), valid_until)
        frozen_policy["reference_prices"] = {order.symbol: float(order.reference_price) for order in orders}
        plan.schema_version = 2
        plan.policy = frozen_policy
        plan.instrument_specs = specs
        plan.allowed_symbols = sorted(specs)
        plan.valid_until = valid_until.isoformat()
        plan.account_fingerprint = self.account_fingerprint
        plan.issued_at = local.isoformat()
        plan.lease_expires_at = lease.isoformat()
        for key, value in strategy_resources(session, inst, plan.trade_date,
                exclude={order.client_order_id for order in orders}).items():
            setattr(plan, key, value)
        latest = session.execute(select(OmsBrokerSnapshot).where(
            OmsBrokerSnapshot.account_alias == plan.account_alias,
            OmsBrokerSnapshot.trade_date == plan.trade_date,
        ).order_by(OmsBrokerSnapshot.taken_at.desc())).scalars().first()
        fresh = False
        if latest is not None:
            taken = datetime.fromisoformat(latest.taken_at).astimezone(CHINA)
            fresh = 0 <= (local - taken).total_seconds() <= 1800
            recon_rows = session.execute(select(OmsReconciliation).where(
                OmsReconciliation.snapshot_id == latest.snapshot_id)).scalars().all()
            fresh = fresh and bool(recon_rows) and all(row.passed for row in recon_rows)
        plan.executable = bool(plan.executable and instance_is_enabled(inst) and self.account_fingerprint
                               and local.strftime("%Y%m%d") == plan.trade_date and fresh and local <= lease)
        plan.plan_sha256 = plan_content_sha256(plan.model_dump(mode="json"))
        return plan

    def plans_for(self, account_alias: str, trade_date: str, phase: str, *, executable_flag: bool,
                  now: str | None = None) -> dict:
        with self.session_factory() as session:
            ids = session.execute(select(OmsCycle.instance_id).join(
                OmsSession, OmsSession.cycle_id == OmsCycle.cycle_id).where(
                OmsCycle.account_alias == account_alias, OmsCycle.status.in_(OPEN_STATUSES),
                OmsSession.trade_date == trade_date, OmsSession.phase == phase,
            ).order_by(OmsCycle.instance_id)).scalars().all()
        plans = [self.plan_for(account_alias, trade_date, phase, executable_flag=executable_flag,
                              instance_id=instance_id, wire_version=2, now=now).model_dump(mode="json")
                 for instance_id in ids]
        return {"schema_version": 2, "account_alias": account_alias, "plans": plans,
                "symbols": self.universe(account_alias)["symbols"]}

    def universe(self, account_alias: str) -> dict:
        symbols: set[str] = set()
        instances = []
        with self.session_factory() as session:
            for inst in session.execute(select(InstanceState).where(
                    InstanceState.execution_domain == "live", InstanceState.account_alias == account_alias)).scalars():
                state = inst.strategy_state or {}
                managed = set(inst.owned_symbols or ()) | set(inst.virtual_positions or {})
                symbols |= managed
                instances.append({"instance_id": inst.instance_id, "enabled": instance_is_enabled(inst),
                                  "symbols": sorted(managed), "accounting_hold": state.get("accounting_hold")})
            for cycle in self._open_cycles(session, account_alias):
                symbols |= set(cycle.frozen_target)
        return {"schema_version": 2, "account_alias": account_alias, "symbols": sorted(symbols),
                "instances": instances}

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
        prices = {s: float(marks.get(s) or (cycle.buy_anchor or {}).get(s) or cycle.sell_anchor.get(s) or 0.)
                  for s in set(cycle.frozen_target) | set(positions)}
        nav = float(inst.virtual_cash) + sum(q * prices.get(s, 0.) for s, q in positions.items())
        orders = session.execute(select(OmsOrder).where(OmsOrder.cycle_id == cycle.cycle_id)
                                 .order_by(OmsOrder.client_order_id)).scalars().all()
        last_by_symbol = {}
        for order in orders:
            last_by_symbol[(order.symbol, order.side)] = order
        unfinished = {}
        deferrals = {}
        for row in session.execute(select(OmsSession).where(OmsSession.cycle_id == cycle.cycle_id)
                                   .order_by(OmsSession.seq)).scalars():
            for deferred in row.deferrals or []:
                deferrals[(deferred["symbol"], deferred["side"])] = deferred["reason"]
        for symbol in set(cycle.frozen_target) | set(positions):
            target = cycle.frozen_target.get(symbol, 0)
            gap = int(target) - positions.get(symbol, 0)
            if gap == 0:
                continue
            side = "BUY" if gap > 0 else "SELL"
            last = last_by_symbol.get((symbol, side))
            reason = last.state if last is not None else deferrals.get((symbol, side), "NO_ORDER")
            unfinished[symbol] = {"side": side, "shares": abs(gap), "value": abs(gap) * prices[symbol],
                                  "reason": reason}
        drift = [{"client_order_id": o.client_order_id, "symbol": o.symbol, "side": o.side,
                  "bps": (1 if o.side == "BUY" else -1) * (o.avg_price / o.reference_price - 1) * 1e4}
                 for o in orders if o.filled_qty and o.reference_price]
        return {"cycle_id": cycle.cycle_id, "close_reason": cycle.close_reason, "lot_gap": cycle.lot_gap,
                "exec_underweight": sum(v["value"] for v in unfinished.values() if v["side"] == "BUY") / nav if nav else 0.,
                "exec_overweight": sum(v["value"] for v in unfinished.values() if v["side"] == "SELL") / nav if nav else 0.,
                "unfinished": unfinished, "price_drift_bps": drift, "nav": nav}

    def status(self, account_alias: str, instance_id: str | None = None) -> dict:
        with self.session_factory() as session:
            if instance_id is None:
                instances = session.execute(select(InstanceState.instance_id).where(
                    InstanceState.execution_domain == "live", InstanceState.account_alias == account_alias)).scalars().all()
                if len(instances) > 1:
                    return {"cycle": None, "strategies": [
                        {"instance_id": identity, **self.status(account_alias, identity)} for identity in instances]}
            query = select(OmsCycle).where(OmsCycle.account_alias == account_alias)
            if instance_id is not None:
                query = query.where(OmsCycle.instance_id == instance_id)
            cycle = query.order_by(OmsCycle.cycle_no.desc())
            cycle = session.execute(cycle).scalars().first()
            if cycle is None:
                return {"cycle": None}
            sessions = session.execute(select(OmsSession).where(OmsSession.cycle_id == cycle.cycle_id)
                                       .order_by(OmsSession.seq)).scalars().all()
            orders = session.execute(select(OmsOrder).where(OmsOrder.cycle_id == cycle.cycle_id)
                                     .order_by(OmsOrder.client_order_id)).scalars().all()
            recon = session.execute(select(OmsReconciliation).where(OmsReconciliation.cycle_id == cycle.cycle_id)
                                    .order_by(OmsReconciliation.id.desc())).scalars().first()
            return {"cycle": {"cycle_id": cycle.cycle_id, "instance_id": cycle.instance_id,
                              "status": cycle.status, "frozen_target": cycle.frozen_target,
                              "lot_gap": cycle.lot_gap, "schedule": cycle.schedule},
                    "sessions": [{"session_id": s.session_id, "trade_date": s.trade_date, "phase": s.phase,
                                  "status": s.status, "deferrals": s.deferrals} for s in sessions],
                    "orders": [{"client_order_id": o.client_order_id, "symbol": o.symbol, "side": o.side,
                                "quantity": o.quantity, "limit_price": o.limit_price, "state": o.state,
                                "filled_qty": o.filled_qty, "avg_price": o.avg_price} for o in orders],
                    "last_reconciliation": None if recon is None else
                    {"passed": recon.passed, "discrepancies": recon.discrepancies, "created_at": recon.created_at}}

    @staticmethod
    def _open_cycles(session, account_alias: str, instance_id: str | None = None) -> list[OmsCycle]:
        query = select(OmsCycle).where(OmsCycle.account_alias == account_alias, OmsCycle.status.in_(OPEN_STATUSES))
        if instance_id is not None:
            query = query.where(OmsCycle.instance_id == instance_id)
        return session.execute(query.order_by(OmsCycle.cycle_no)).scalars().all()

    @staticmethod
    def _open_cycle(session, account_alias: str, instance_id: str | None = None) -> OmsCycle | None:
        rows = CycleService._open_cycles(session, account_alias, instance_id)
        if len(rows) > 1:
            raise CycleConflict("multiple strategy cycles; instance_id is required")
        return rows[0] if rows else None
