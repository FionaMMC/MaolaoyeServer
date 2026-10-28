"""Daily read-only V713 synthetic ledger; never creates targets or orders.

    PYTHONPATH=. python -m scripts.run_v713_synthetic --policy plugins/v713/synthetic_policy_v1.json \
        --output /path/to/v713_synthetic.json

Run after the live performance job has valued both legs for the day.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def main(argv=None, *, session_factory=None, now=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.run_v713_synthetic")
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="defaults to QMT_V713_SYNTHETIC_OUTPUT")
    parser.add_argument("--as-of", help="Expected completed exchange trading date (YYYYMMDD); never inferred from wall time")
    args = parser.parse_args(argv)
    if args.as_of:
        try:
            parsed_date = datetime.strptime(args.as_of, "%Y%m%d")
            if parsed_date.strftime("%Y%m%d") != args.as_of:
                raise ValueError("date must have eight digits")
        except ValueError as exc:
            parser.error(f"invalid --as-of: {exc}")

    from app.services.v713_synthetic import (SyntheticEvidenceUnavailable, load_inputs,
                                             load_synthetic_policy, run_synthetic)
    policy = load_synthetic_policy(args.policy)
    output = args.output
    if session_factory is None or output is None:
        from app.settings import get_settings
        settings = get_settings()
        output = output or settings.v713_synthetic_output
        if session_factory is None:
            from app.db import make_engine, make_session_factory
            session_factory = make_session_factory(make_engine(settings.db_url))
    if output is None:
        raise SystemExit("--output or QMT_V713_SYNTHETIC_OUTPUT is required")
    missing_order_evidence = []
    expected_date = args.as_of
    try:
        inputs = load_inputs(session_factory, policy)
        expected_date = args.as_of or inputs["expected_date"]
        inputs["expected_date"] = expected_date
        lines = {f"{bps:g}": run_synthetic(policy, bps=bps, **inputs) for bps in policy.cost_bps}
    except SyntheticEvidenceUnavailable as exc:
        missing_order_evidence = exc.order_ids
        lines = {f"{bps:g}": {"status": "WAITING_ORDER_EVIDENCE", "rows": [], "events": []}
                 for bps in policy.cost_bps}
    primary = lines[f"{policy.primary_bps:g}"]
    document = {"policy_id": policy.policy_id, "label": policy.label, "stock_instance": policy.stock_instance,
                "hydra_instance": policy.hydra_instance, "base_value": policy.base_value,
                "primary_bps": policy.primary_bps, "status": primary["status"],
                "generated_at": now or _now(), "lines": lines,
                "as_of_date": primary["rows"][-1]["date"] if primary["rows"] else None,
                "expected_date": expected_date, "missing_order_evidence": missing_order_evidence}
    output = Path(output)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, output)
    print(json.dumps({"status": primary["status"], "rows": len(primary["rows"]), "output": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
