"""Operator commands for the execution core. Dry-run by default; nothing touches a broker.

    PYTHONPATH=. python -m scripts.oms_ops publish --instance live_hydra_v481_rb --account hydra-live \
        --signal-date 20260930 --weights w.json --closes c.json --calendar cal.json --source-sha256 <sha> [--apply]
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


def _preview(sf, policy, *, instance, account, signal_date, weights, closes, calendar) -> dict:
    """What publish would freeze, computed read-only."""
    import math
    from app.models import InstanceState
    from app.oms.planner import lot_target, plan_sell_session, session_schedule
    closes = {s: float(p) for s, p in closes.items() if p is not None and math.isfinite(float(p)) and float(p) > 0}
    with sf() as session:
        inst = session.get(InstanceState, instance)
        if inst is None or inst.account_alias != account:
            raise SystemExit(f"unknown instance {instance} for {account}")
        positions = {s: int(q) for s, q in (inst.virtual_positions or {}).items() if int(q)}
        cash = float(inst.virtual_cash)
    nav = cash + sum(q * closes[s] for s, q in positions.items())
    frozen = lot_target(nav, weights, closes, policy)
    schedule = session_schedule(calendar, signal_date, policy.window)
    sells, deferrals = plan_sell_session(target=frozen, positions=positions, sellable=positions, sell_anchor=closes,
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

    from app.db import make_engine, make_session_factory, init_db
    from app.dependencies import get_oms_cycle_service
    from app.settings import get_settings
    settings = settings or get_settings()
    if session_factory is None:
        engine = make_engine(settings.db_url)
        init_db(engine)
        session_factory = make_session_factory(engine)
    service = get_oms_cycle_service(session_factory, settings)

    if args.command == "publish":
        weights = json.loads(Path(args.weights).read_text(encoding="utf-8"))
        closes = json.loads(Path(args.closes).read_text(encoding="utf-8"))
        calendar = json.loads(Path(args.calendar).read_text(encoding="utf-8"))
        preview = _preview(session_factory, service.policy, instance=args.instance, account=args.account,
                           signal_date=args.signal_date, weights=weights, closes=closes, calendar=calendar)
        print(_table(preview["shares"]))
        print(json.dumps({k: preview[k] for k in ("nav", "cash", "schedule", "first_session_sells", "deferrals")},
                         ensure_ascii=False, indent=2))
        if not args.apply:
            print("DRY RUN: nothing written. Re-run with --apply to create the cycle (status PENDING_APPROVAL).")
            return 0
        out = service.publish_target(instance_id=args.instance, account_alias=args.account,
                                     signal_date=args.signal_date, weights=weights, signal_closes=closes,
                                     calendar=calendar, source_sha256=args.source_sha256, now=_now())
        print(json.dumps({k: out[k] for k in ("cycle_id", "status", "target_version_id", "lot_gap")}, indent=2))
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
