"""按 target 汇总滑点、溢价与费用；不把决策跳空混进 broker shortfall。"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import ExecutionQualityObservation
from app.services.execution_quality import summarize_execution_quality


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--execution-domain", choices=("paper", "live"), required=True)
    args = parser.parse_args()
    engine = create_engine(args.db_url, future=True)
    sf = sessionmaker(bind=engine, future=True)
    try:
        with sf() as session:
            rows = session.execute(
                select(ExecutionQualityObservation).where(
                    ExecutionQualityObservation.target_id == args.target_id,
                    ExecutionQualityObservation.execution_domain == args.execution_domain,
                )
            ).scalars().all()
    finally:
        engine.dispose()
    result = {
        "target_id": args.target_id,
        "execution_domain": args.execution_domain,
        **summarize_execution_quality(rows),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
