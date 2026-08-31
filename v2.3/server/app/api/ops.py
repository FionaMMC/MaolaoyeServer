"""运营/对账/告警端点。轻薄：调用 OpsMonitorService / AlertEngine。"""
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc

from app.auth import verify_api_key
from app.schemas.common import APIResponse
from app.dependencies import get_ops_monitor, get_alert_engine, get_session_factory
from app.models import PerfSnapshot
from app.services.ops_monitor import OpsMonitorService
from app.services.alerts import AlertEngine

router = APIRouter(prefix="/admin")


@router.get("/ops/pipeline-runs", response_model=APIResponse[dict], dependencies=[Depends(verify_api_key)])
async def ops_pipeline_runs(days: int = Query(14, ge=1, le=60),
                            ops: OpsMonitorService = Depends(get_ops_monitor)):
    return APIResponse[dict](code=0, message="ok", data={"runs": ops.pipeline_runs(days)})


@router.get("/ops/snapshot-integrity", response_model=APIResponse[dict], dependencies=[Depends(verify_api_key)])
async def ops_snapshot_integrity(instance_id: str, lookback: int = Query(30, ge=2, le=400),
                                 ops: OpsMonitorService = Depends(get_ops_monitor)):
    return APIResponse[dict](code=0, message="ok", data=ops.snapshot_integrity(instance_id, lookback))


@router.get("/ops/reconcile-anomalies", response_model=APIResponse[dict], dependencies=[Depends(verify_api_key)])
async def ops_reconcile_anomalies(instance_id: str, threshold: float = Query(0.5, gt=0),
                                  ops: OpsMonitorService = Depends(get_ops_monitor)):
    return APIResponse[dict](code=0, message="ok",
        data={"overnight_position_anomalies": ops.overnight_position_anomalies(instance_id, threshold)})


@router.get(
    "/ops/live-snapshot",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def ops_live_snapshot(
    instance_id: str,
    days: int = Query(30, ge=1, le=365),
    ops: OpsMonitorService = Depends(get_ops_monitor),
):
    """24h command-center snapshot; observed values and telemetry gaps in one call."""
    return APIResponse[dict](
        code=0,
        message="ok",
        data=ops.live_snapshot(instance_id=instance_id, lookback_days=days),
    )


@router.get("/alerts", response_model=APIResponse[dict], dependencies=[Depends(verify_api_key)])
async def alerts(eng: AlertEngine = Depends(get_alert_engine)):
    al = eng.run_checks()
    return APIResponse[dict](code=0, message="ok",
        data={"alerts": [a.to_dict() for a in al],
              "counts": {"critical": sum(a.severity == "critical" for a in al),
                         "warn": sum(a.severity == "warn" for a in al),
                         "info": sum(a.severity == "info" for a in al)}})


@router.get("/dashboard-meta", response_model=APIResponse[dict], dependencies=[Depends(verify_api_key)])
async def dashboard_meta(ops: OpsMonitorService = Depends(get_ops_monitor),
                         eng: AlertEngine = Depends(get_alert_engine),
                         sf=Depends(get_session_factory)):
    al = eng.run_checks()
    fr = ops.data_freshness()
    with sf() as s:
        rows = s.execute(select(PerfSnapshot.instance_id, PerfSnapshot.date, PerfSnapshot.nav)
                         .order_by(desc(PerfSnapshot.date))).all()
    max_perf = rows[0][1] if rows else None
    navs = {}
    for inst, d, nav in rows:
        navs.setdefault(inst, (d, nav))
    runs = ops.pipeline_runs(5)
    # no_signal（有快照无信号）也算管线跑过——周更策略非调仓日不能算"最近运行=几天前"
    last_run = next((r for r in reversed(runs) if r["status"] in ("ok", "no_signal")), None)
    return APIResponse[dict](code=0, message="ok", data={
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "freshness": fr,
        "last_pipeline_run": last_run,
        "account_nav": sum(v[1] for v in navs.values()) if navs else None,
        "instance_navs": {k: {"date": v[0], "nav": v[1]} for k, v in navs.items()},
        "alerts": {"critical": sum(a.severity == "critical" for a in al),
                   "warn": sum(a.severity == "warn" for a in al)},
        "version": {"max_perf_date": max_perf,
                    "last_run_signal_time": last_run["signal_time"] if last_run else None,
                    "alert_rev": len(al)},
    })
