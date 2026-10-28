"""Operator commands for the execution core. Dry-run by default; nothing touches a broker.

    PYTHONPATH=. python -m scripts.oms_ops publish --instance live_hydra_v481_rb --account hydra-live \
        --signal-date 20260930 --weights w.json --closes c.json --calendar cal.json --source-sha256 <sha> [--apply]
    PYTHONPATH=. python -m scripts.oms_ops publish-v713-stock-leg --instance live_v713_stockleg --account <alias> \
        --basket v713_target_latest.parquet --policy-template t.json --closes c.json --calendar cal.json [--apply]
    PYTHONPATH=. python -m scripts.oms_ops approve --cycle-id C00001 --approver <name> [--yes]
    PYTHONPATH=. python -m scripts.oms_ops resolve --cycle-id C00001 --operator <name> --reason "<why>" [--yes]
    PYTHONPATH=. python -m scripts.oms_ops status --account hydra-live

``publish`` prints the per-symbol share list that must be approved before ``approve --yes``.
``resolve`` records a human decision on the latest failed reconciliation and reactivates a
HELD cycle; the next snapshot that reconciles continues the normal flow.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from sqlalchemy import select


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _table(rows: list[dict]) -> str:
    head = f"{'symbol':<11}{'weight':>8}{'close':>10}{'held':>9}{'target':>9}{'delta':>9}{'delta_value':>13}"
    lines = [head, "-" * len(head)]
    for r in rows:
        close = r["close"] or 0.
        lines.append(f"{r['symbol']:<11}{r['weight']:>8.4f}{close:>10.3f}{r['held']:>9}{r['target']:>9}"
                     f"{r['delta']:>9}{r['delta'] * close:>13.2f}")
    return "\n".join(lines)


def _preview(sf, policy, *, instance, account, signal_date, weights, closes, calendar, now,
             earliest_execution_date=None, reference_closes=None) -> dict:
    """What publish would freeze, computed read-only."""
    import math
    from app.models import InstanceState
    from app.oms.cycles import earliest_sell_day
    from app.oms.planner import lot_target, plan_sell_session, session_schedule
    closes = {s: float(p) for s, p in closes.items() if p is not None and math.isfinite(float(p)) and float(p) > 0}
    with sf() as session:
        inst = session.get(InstanceState, instance)
        if inst is None or inst.account_alias != account or inst.execution_domain != "live":
            raise SystemExit(f"unknown instance {instance} for {account}")
        positions = {s: int(q) for s, q in (inst.virtual_positions or {}).items() if int(q)}
        cash = float(inst.virtual_cash)
    nav = cash + sum(q * closes[s] for s, q in positions.items())
    complete_weights = {s: 0. for s in positions} | weights
    frozen = lot_target(nav, complete_weights, closes, policy)
    earliest = max(earliest_sell_day(now), earliest_execution_date or "")
    schedule = session_schedule(calendar, signal_date, policy.window, earliest_sell=earliest)
    sell_anchor = closes
    if policy.anchor == "previous_close":
        before = max(d for d in sorted(set(calendar)) if d < schedule[0].trade_date)
        if before != signal_date:
            if reference_closes is None:
                raise ValueError(f"previous-close anchors: the first session needs the {before} closes")
            missing = sorted(s for s in frozen if not (float(reference_closes.get(s) or 0) > 0))
            if missing:
                raise ValueError(f"reference closes missing for {missing}")
            sell_anchor = {s: float(reference_closes[s]) for s in frozen}
    sells, deferrals = plan_sell_session(target=frozen, positions=positions, sellable=positions, sell_anchor=sell_anchor,
                                         attempt=0, policy=policy)
    rows = [{"symbol": s, "weight": float(weights.get(s, 0.)), "close": closes.get(s), "target": frozen.get(s, 0),
             "held": positions.get(s, 0), "delta": frozen.get(s, 0) - positions.get(s, 0)}
            for s in sorted(set(frozen) | set(positions))]
    return {"nav": nav, "cash": cash, "schedule": [x.__dict__ for x in schedule], "shares": rows,
            "first_session_sells": [o.__dict__ for o in sells], "deferrals": [d.__dict__ for d in deferrals]}


def main(argv=None, *, session_factory=None, settings=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.oms_ops")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("publish")
    for name in ("--instance", "--account", "--signal-date", "--weights", "--closes", "--calendar", "--source-sha256"):
        p.add_argument(name, required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--policy", help="explicit versioned policy JSON; V713 must use publish-v713")
    p.add_argument("--now", default=None, help="rehearsal/replay only: publish as if at this zoned ISO time; "
                                               "a past schedule is never executed (the agent refuses other dates)")
    v = sub.add_parser("publish-v713", help="validate a monthly V713 basket and preview its attributed-account plan")
    for name in ("--instance", "--account", "--basket", "--policy", "--closes", "--calendar"):
        v.add_argument(name, required=True)
    v.add_argument("--apply", action="store_true")
    v.add_argument("--now", default=None, help="zoned ISO publication time for an isolated rehearsal")
    k = sub.add_parser("publish-v713-stock-leg",
                       help="derive the V713 stock-leg target from a monthly basket and record its state")
    for name in ("--instance", "--account", "--basket", "--policy-template", "--closes", "--calendar"):
        k.add_argument(name, required=True)
    k.add_argument("--apply", action="store_true")
    k.add_argument("--now", default=None, help="zoned ISO publication time for an isolated rehearsal")
    k.add_argument("--reference-closes", default=None,
                   help="late publication with previous-close anchors: closes of the day before the first sell session")
    a = sub.add_parser("approve")
    a.add_argument("--cycle-id", required=True)
    a.add_argument("--approver", required=True)
    a.add_argument("--yes", action="store_true")
    r = sub.add_parser("resolve")
    r.add_argument("--cycle-id", required=True)
    r.add_argument("--operator", required=True)
    r.add_argument("--reason", required=True)
    r.add_argument("--yes", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--account", required=True)
    args = parser.parse_args(argv)

    from app.db import make_engine, make_session_factory
    from app.dependencies import get_oms_cycle_service
    from app.settings import get_settings
    settings = settings or get_settings()
    if session_factory is None:
        engine = make_engine(settings.db_url)
        # Publishing is not a schema migration. In particular dry-run must never
        # create tables or an empty SQLite database at a mistyped location.
        if engine.url.get_backend_name() == "sqlite" and engine.url.database not in (None, "", ":memory:"):
            if not Path(engine.url.database).is_file():
                raise SystemExit("OMS database does not exist; initialize the isolated environment separately")
        session_factory = make_session_factory(engine)
    service = get_oms_cycle_service(session_factory, settings)

    if args.command in ("publish", "publish-v713", "publish-v713-stock-leg"):
        from app.oms.planner import policy_from_dict, policy_to_dict

        closes = json.loads(Path(args.closes).read_text(encoding="utf-8"))
        calendar = json.loads(Path(args.calendar).read_text(encoding="utf-8"))
        now = args.now or _now()
        if args.command in ("publish-v713", "publish-v713-stock-leg"):
            from app.models import InstanceState
            from app.oms.v713 import build_publication
            with session_factory() as session:
                instance = session.get(InstanceState, args.instance)
                if instance is None or instance.execution_domain != "live" or instance.account_alias != args.account:
                    raise SystemExit("V713 publication requires the initialized live instance for this account")
                held = [s for s, q in (instance.virtual_positions or {}).items() if q]
            if args.command == "publish-v713":
                policy_payload = json.loads(Path(args.policy).read_text(encoding="utf-8"))
            else:
                import pandas as pd
                from app.oms.v713_stock_leg import expand_policy
                codes = set(pd.read_parquet(args.basket, columns=["code"])["code"].astype(str))
                policy_payload = expand_policy(json.loads(Path(args.policy_template).read_text(encoding="utf-8")),
                                               codes | set(held))
            payload = build_publication(args.basket, closes=closes, calendar=calendar,
                                        policy_payload=policy_payload, now=now, held_symbols=held)
            if args.command == "publish-v713-stock-leg":
                from app.oms.cycles import CycleService
                from app.oms.v713_stock_leg import derive_stock_leg
                payload = derive_stock_leg(payload)
                # A later monthly target ends the previous cycle's catch-up instead of being refused.
                payload["supersede"] = True
                if args.reference_closes:
                    payload["reference_closes"] = json.loads(Path(args.reference_closes).read_text(encoding="utf-8"))
                with session_factory() as session:
                    open_cycle = CycleService._open_cycle(session, args.account, args.instance)
                if open_cycle is not None:
                    print(f"WILL SUPERSEDE {open_cycle.cycle_id}: a later monthly target ends its catch-up")
                if payload["liquidate"] and not held:
                    print(json.dumps({"sleeve": payload["provenance"]["sleeve"], "action": "STATE_ONLY",
                                      "reason": "the stock leg already holds no stock"}, ensure_ascii=False))
                    if not args.apply:
                        print("DRY RUN: nothing written.")
                        return 0
                    service.close_superseded(instance_id=args.instance, account_alias=args.account,
                                             signal_date=payload["signal_date"], closes=closes, now=now,
                                             v713_state=payload)
                    print(json.dumps({"instance_id": args.instance, "signal_date": payload["signal_date"],
                                      "sleeve": payload["provenance"]["sleeve"], "target_version_id": None},
                                     ensure_ascii=False))
                    return 0
        else:
            policy = policy_from_dict(json.loads(Path(args.policy).read_text(encoding="utf-8"))) if args.policy else service.policy
            if policy.strategy_id == "v713":
                raise SystemExit("use publish-v713 for V713 basket validation and monthly provenance")
            payload = {"weights": json.loads(Path(args.weights).read_text(encoding="utf-8")),
                       "signal_closes": closes, "calendar": calendar, "signal_date": args.signal_date,
                       "source_sha256": args.source_sha256, "policy": policy, "now": now}
        preview = _preview(session_factory, payload["policy"], instance=args.instance, account=args.account,
                           signal_date=payload["signal_date"], weights=payload["weights"], closes=closes,
                           calendar=calendar, now=now, earliest_execution_date=payload.get("earliest_execution_date"),
                           reference_closes=payload.get("reference_closes"))
        print(_table(preview["shares"]))
        print(json.dumps({k: preview[k] for k in ("nav", "cash", "schedule", "first_session_sells", "deferrals")},
                         ensure_ascii=False, indent=2))
        print(json.dumps({"policy": policy_to_dict(payload["policy"]), "provenance": payload.get("provenance")},
                         ensure_ascii=False, indent=2))
        if not args.apply:
            print("DRY RUN: nothing written. Re-run with --apply to create the cycle (status PENDING_APPROVAL).")
            return 0
        out = service.publish_target(instance_id=args.instance, account_alias=args.account, **payload,
                                     v713_state=payload if args.command == "publish-v713-stock-leg" else None)
        print(json.dumps({k: out[k] for k in ("cycle_id", "status", "target_version_id", "lot_gap")}, indent=2))
        if args.command == "publish-v713-stock-leg":
            print(json.dumps(out["v713_state"], ensure_ascii=False))
        return 0

    if args.command == "approve":
        from app.oms.models import OmsCycle
        with session_factory() as session:
            cycle = session.get(OmsCycle, args.cycle_id)
            if cycle is None:
                raise SystemExit(f"unknown cycle {args.cycle_id}")
            print(json.dumps({"cycle_id": cycle.cycle_id, "status": cycle.status, "frozen_target": cycle.frozen_target,
                              "schedule": cycle.schedule}, ensure_ascii=False, indent=2))
        if not args.yes:
            print("Not approved. Re-run with --yes after checking the share list.")
            return 0
        service.approve(args.cycle_id, args.approver, _now())
        print(f"{args.cycle_id} ACTIVE (approved by {args.approver})")
        return 0

    if args.command == "resolve":
        from app.oms.models import OmsCycle, OmsReconciliation
        from app.services.ledger_transaction import begin_ledger_transaction
        with session_factory() as session:
            begin_ledger_transaction(session, "live", None)
            cycle = session.get(OmsCycle, args.cycle_id)
            if cycle is None or cycle.status != "HELD":
                raise SystemExit(f"cycle {args.cycle_id} is not HELD")
            recon = session.execute(select(OmsReconciliation).where(
                OmsReconciliation.cycle_id == cycle.cycle_id, OmsReconciliation.passed.is_(False),
                OmsReconciliation.resolution.is_(None)).order_by(OmsReconciliation.id.desc())).scalars().first()
            print(json.dumps({"cycle_id": cycle.cycle_id,
                              "discrepancies": recon.discrepancies if recon else None}, ensure_ascii=False, indent=2))
            if not args.yes:
                print("Not resolved. Re-run with --yes once the discrepancy is understood.")
                return 0
            if recon is not None:
                recon.resolution, recon.resolved_by, recon.resolved_at = args.reason, args.operator, _now()
            cycle.status = "ACTIVE"
            session.commit()
        print(f"{args.cycle_id} ACTIVE; the next reconciling snapshot resumes planning")
        return 0

    print(json.dumps(service.status(args.account), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
