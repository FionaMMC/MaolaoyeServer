"""Value selected attributed live ledgers; never generates or delivers orders."""
import argparse
import json
from app.db import make_engine, make_session_factory
from app.settings import get_settings
from app.storage.parquet import ParquetStore
from app.services.live_performance import materialize_live_performance
from app.models import InstanceState
from sqlalchemy import select
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill", action="store_true", help="Only since the last authoritative ledger update")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--instance", action="append", help="Explicit live instance; repeat for several strategies")
    scope.add_argument("--all-live", action="store_true", help="All attributed live strategies")
    parser.add_argument("--raw-price-evidence", type=Path,
        help="JSON mapping instance ID -> symbol -> {category,adjustment,source,sha256}")
    args = parser.parse_args()
    settings = get_settings()
    sf = make_session_factory(make_engine(settings.db_url))
    instances = args.instance or [settings.hydra_monthly_instance_id]
    if args.all_live:
        with sf() as session:
            instances = list(session.scalars(select(InstanceState.instance_id).where(
                InstanceState.execution_domain == "live", InstanceState.ledger_mode == "attributed")))
    proofs = json.loads(args.raw_price_evidence.read_text()) if args.raw_price_evidence else {}
    results = {instance: materialize_live_performance(sf, ParquetStore(settings.parquet_root), instance,
        backfill=args.backfill, raw_price_evidence=proofs.get(instance)) for instance in instances}
    # Existing Hydra timers/collectors retain the original one-result shape.
    print(json.dumps(results if args.instance or args.all_live else results[settings.hydra_monthly_instance_id]))
