"""Independent monthly worker. Default is diagnostic; --apply only queues plans.

Run from server with PYTHONPATH=. No broker calls, order submissions, or changes
to cash/positions. systemd serializes invocations; DB identity handles retries.
"""

import argparse
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

from sqlalchemy import select
from app.models import HydraMonthlyCycle
from app.services.hydra_monthly import RESEARCH_COMMIT, publish_monthly_plan, validate_inputs


def compute(research, store, day, hashes):
    """Child process only: no research module/global env leaks into HTTP."""
    import pandas as pd

    actual = subprocess.check_output(
        ["git", "-C", str(research), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != RESEARCH_COMMIT or subprocess.check_output(
        ["git", "-C", str(research), "diff", "HEAD", "--"]
    ):
        raise ValueError("研究源码必须为未修改的获批 aa6b60d")
    model, manifest = store.load("hydra_model_hfq", hashes["model_hfq"])
    if manifest.as_of_date != day:
        raise ValueError("HFQ 日期不一致")
    for key, digest in hashes.items():
        _, item = store.load("hydra_" + key, digest)
        if item.as_of_date != day:
            raise ValueError("研究输入日期不一致")

    def no_network(*args, **kwargs):
        raise RuntimeError("研究计算只能使用冻结输入，禁止联网回退")

    socket.create_connection = no_network
    socket.socket.connect = no_network
    with tempfile.TemporaryDirectory(prefix="hydra-research-") as temporary:
        root = Path(temporary)
        flat = root / "flat" / "etf_back"
        flat.mkdir(parents=True)
        empty = root / "empty"
        empty.mkdir()
        for symbol, rows in model.groupby("symbol"):
            rows.drop(columns="symbol").to_parquet(flat / f"{symbol}.parquet", index=False)
        os.environ["V48_FLAT_DATA_DIR"] = str(flat.parent)
        os.environ["V48_DATA_DIR"] = str(empty)
        os.environ["V48_BACKTEST_END"] = day
        sys.path[:0] = [str(research / "v48"), str(research)]
        builder = importlib.import_module("build_v481_rb_weights")
        weights, audit = builder.build_weights(pd.Timestamp(day))
        if weights.index.max().normalize() != pd.Timestamp(day):
            raise ValueError("研究权重未到达指定收盘日")
        row = weights.loc[pd.Timestamp(day)]
        return dict(
            research_commit=actual,
            as_of_date=day,
            input_hashes=hashes,
            weights={builder.ETF_CODES[key]: float(value) for key, value in row.items()},
            schedule_rows=len(weights),
            budget_audit=audit.iloc[-1].to_dict(),
        )


def run_worker(relay, settings, *, apply=False):
    if not settings.hydra_monthly_enabled:
        return {"status": "DISABLED", "order_count": 0}
    with relay.session_factory() as session:
        cycles = list(
            session.scalars(
                select(HydraMonthlyCycle)
                .where(
                    HydraMonthlyCycle.instance_id == settings.hydra_monthly_instance_id,
                    HydraMonthlyCycle.account_alias == settings.hydra_monthly_account_alias,
                    HydraMonthlyCycle.execution_domain == "live",
                    HydraMonthlyCycle.as_of_date >= settings.hydra_monthly_start_date,
                    HydraMonthlyCycle.status != "PLANNED",
                )
                .order_by(HydraMonthlyCycle.as_of_date)
            )
        )
    if not cycles:
        return {"status": "IDLE_NO_NEW_MONTHLY_INPUT", "order_count": 0}
    results = []
    for cycle in cycles:
        try:
            validate_inputs(relay, cycle.input_hashes, cycle.as_of_date)
            with tempfile.TemporaryDirectory(prefix="hydra-monthly-worker-") as temporary:
                request = Path(temporary) / "request.json"
                output = Path(temporary) / "weights.json"
                request.write_text(
                    json.dumps({"day": cycle.as_of_date, "hashes": cycle.input_hashes})
                )
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--compute",
                        str(request),
                        "--research",
                        str(settings.hydra_monthly_research_dir),
                        "--data-root",
                        str(relay.data_store.root.parent.parent),
                        "--output",
                        str(output),
                    ],
                    check=True,
                    timeout=600,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                computed = json.loads(output.read_text())
            if apply:
                result = publish_monthly_plan(relay, cycle.cycle_id, computed)
            else:
                result = {
                    "status": "DRY_RUN_COMPUTED",
                    "cycle_id": cycle.cycle_id,
                    "weights": computed["weights"],
                    "order_count": 0,
                }
            results.append(result)
        except Exception as exc:
            # Missing/corrupt input or research failure never takes down HTTP.
            results.append(
                {
                    "status": "WAITING_RESEARCH_REVIEW",
                    "cycle_id": cycle.cycle_id,
                    "reason": str(exc),
                    "order_count": 0,
                }
            )
    return {"status": "MONTHLY_WORKER_FINISHED", "results": results, "order_count": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--compute", type=Path)
    parser.add_argument("--research", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.compute:
        from app.services.hydra_data import HydraDataStore

        request = json.loads(args.compute.read_text())
        result = compute(
            args.research, HydraDataStore(args.data_root), request["day"], request["hashes"]
        )
        args.output.write_text(json.dumps(result, default=str, allow_nan=False))
        return
    from app.settings import get_settings
    from app.db import make_engine, make_session_factory
    from app.dependencies import get_hydra_relay_service
    from app.services.blacklist import BlacklistService
    from app.services.hydra_data import HydraDataStore

    settings = get_settings()
    engine = make_engine(settings.db_url)
    try:
        sf = make_session_factory(engine)
        relay = get_hydra_relay_service(
            sf, HydraDataStore(settings.parquet_root), settings, BlacklistService(sf)
        )
        result = run_worker(relay, settings, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False))
        if any(item["status"] == "WAITING_RESEARCH_REVIEW" for item in result.get("results", [])):
            raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
