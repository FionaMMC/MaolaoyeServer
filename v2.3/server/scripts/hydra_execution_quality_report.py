"""按 target 汇总滑点、溢价与费用；不把决策跳空混进 broker shortfall。"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import ExecutionQualityObservation


def _weighted(rows, field: str) -> float | None:
    pairs = [
        (getattr(row, field), int(row.filled_quantity))
        for row in rows
        if getattr(row, field) is not None and int(row.filled_quantity) > 0
    ]
    total = sum(weight for _, weight in pairs)
    if not total:
        return None
    return sum(float(value) * weight for value, weight in pairs) / total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--execution-domain", choices=("paper", "live"), required=True)
    args = parser.parse_args()
    sf = sessionmaker(bind=create_engine(args.db_url, future=True), future=True)
    with sf() as session:
        rows = session.execute(
            select(ExecutionQualityObservation).where(
                ExecutionQualityObservation.target_id == args.target_id,
                ExecutionQualityObservation.execution_domain == args.execution_domain,
            )
        ).scalars().all()
    result = {
        "target_id": args.target_id,
        "execution_domain": args.execution_domain,
        "orders": len(rows),
        "filled_quantity": sum(int(row.filled_quantity) for row in rows),
        "weighted_decision_gap_bps": _weighted(rows, "decision_gap_bps"),
        "weighted_execution_shortfall_bps": _weighted(
            rows, "execution_shortfall_bps"
        ),
        "weighted_premium_bps": _weighted(rows, "premium_bps"),
        "estimated_fees": sum(float(row.estimated_fees) for row in rows),
        "missing_arrival_reference": sum(
            row.arrival_reference_price is None for row in rows
        ),
        "missing_iopv": sum(row.iopv is None for row in rows),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
