"""Value Hydra's attributed live ledger; never generates or delivers orders."""
import argparse
import json
from app.db import make_engine, make_session_factory
from app.settings import get_settings
from app.storage.parquet import ParquetStore
from app.services.live_performance import materialize_live_performance

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill", action="store_true", help="Only since the last authoritative ledger update")
    args = parser.parse_args()
    settings = get_settings()
    result = materialize_live_performance(make_session_factory(make_engine(settings.db_url)),
        ParquetStore(settings.parquet_root), settings.hydra_monthly_instance_id, backfill=args.backfill)
    print(json.dumps(result))
