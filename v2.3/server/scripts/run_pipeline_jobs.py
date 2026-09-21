"""One bounded paper recovery job per invocation. No broker calls.

Diagnostic default; systemd must explicitly opt in with --apply.
"""
import argparse
import json
from app.db import init_db, make_engine, make_session_factory
from app.dependencies import (get_blacklist_service, get_orders_queue_service,
                              get_parquet_store, get_perf_service, get_strategy_pipeline)
from app.services.pipeline_jobs import PipelineJobService
from app.settings import get_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"status": "DRY_RUN", "message": "use --apply to process one queued job"}))
        return
    settings = get_settings()
    engine = make_engine(settings.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    store = get_parquet_store(settings)
    pipeline = get_strategy_pipeline(settings=settings, sf=sf, store=store,
        orders_queue=get_orders_queue_service(sf), perf=get_perf_service(sf, store),
        daily_risk=None, blacklist=get_blacklist_service(sf))
    try:
        print(json.dumps(PipelineJobService(sf, settings.strategies_file).run_once(pipeline),
                         ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
