"""Generate next-session paper V7.13 orders after its target is published.

All normal settlement, fetched-order and ownership guards remain active. Other
strategy groups are neither rerun nor cleared. This CLI cannot select live.
"""
import argparse
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from app.models import InstanceState
from plugins.v713_relay import V713RelayAdapter
from scripts.wrap_v713_shadow_base import wrap
from scripts.stage_shadow_target import stage_target
from datetime import datetime
from zoneinfo import ZoneInfo
from scripts.v713_cycle import exchange_dates, plan_cycle
from app.db import make_engine, make_session_factory
from app.dependencies import (get_strategy_pipeline, get_parquet_store, get_perf_service,
    get_orders_queue_service, get_blacklist_service, get_daily_risk_service)
from app.settings import get_settings


def run(trade_date):
    settings = get_settings()
    sf = make_session_factory(make_engine(settings.db_url))
    store = get_parquet_store(settings)
    pipeline = get_strategy_pipeline(settings=settings, sf=sf, store=store,
        perf=get_perf_service(sf,store), orders_queue=get_orders_queue_service(sf),
        blacklist=get_blacklist_service(sf), daily_risk=get_daily_risk_service(sf,store))
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    cycle = plan_cycle(today, exchange_dates(today))
    if cycle.get("decision_date") != str(trade_date):
        raise ValueError("paper publication requires the confirmed next trading session")
    gap = (datetime.strptime(str(trade_date), "%Y%m%d") - datetime.strptime(today, "%Y%m%d")).days
    # A known holiday gap is not stale data; the pipeline still requires today's
    # close before any future batch and retains all execution guards.
    if pipeline.max_staleness_days is not None:
        pipeline.max_staleness_days = max(pipeline.max_staleness_days, gap)
    basket = V713RelayAdapter()._read_latest_basket()
    if basket is None or str(basket.as_of_date.iloc[0]) != cycle["as_of_date"]:
        raise ValueError("installed V7.13 target does not match the due monthly cycle")
    with sf() as session:
        state = session.get(InstanceState, "paper_v79_v713_relay")
        consumed = state is not None and (state.strategy_state or {}).get("last_consumed_basket_sha256") == str(basket.basket_sha256.iloc[0])
    result = {"orders":0, "status":"monthly_target_already_consumed"} if consumed else pipeline.run(
        trade_date, account_group="paper_v79", execution_domain="paper")
    if result.get("skipped"):
        return result
    # Re-running after an interrupted publication also repairs Base delivery,
    # without reselecting stocks or emitting a new batch for a consumed target.
    target_dir = Path(V713RelayAdapter.data_dir)
    source_manifest = json.loads((target_dir / "v713_target_latest.json").read_text())
    version = str(source_manifest.get("source_commit") or "v7.13-base")
    with TemporaryDirectory() as directory:
        output = Path(directory)
        shutil.copy2(target_dir / "v713_target_latest.parquet", output / "v713_target_latest.parquet")
        wrap(output, version)
        result["shadow"] = stage_target(output / "Shadow_Base_latest.parquet", settings.strategies_file,
            "Shadow_Base", trade_date, sidecar=output / "Shadow_Base_latest.json", install=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=int)
    args = parser.parse_args()
    result = run(args.trade_date)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("skipped"):
        raise SystemExit("V7.13 orders not generated: " + result["skipped"])
