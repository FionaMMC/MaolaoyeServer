#!/usr/bin/env python3
"""Idempotently backfill materialized daily portfolio risk snapshots."""
from __future__ import annotations

import argparse
import json

from app.db import init_db, make_engine, make_session_factory
from app.services.daily_risk import DailyRiskSnapshotService
from app.settings import Settings
from app.storage.parquet import ParquetStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--execution-domain", choices=("paper", "live"))
    args = parser.parse_args()

    settings = Settings()
    engine = make_engine(settings.db_url)
    init_db(engine)
    service = DailyRiskSnapshotService(
        session_factory=make_session_factory(engine),
        parquet_store=ParquetStore(settings.parquet_root),
    )
    result = service.rebuild(
        instance_id=args.instance_id,
        start_date=args.start_date,
        end_date=args.end_date,
        execution_domain=args.execution_domain,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
