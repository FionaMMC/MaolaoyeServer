"""FastAPI 入口：create_app + lifespan + router 注册。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import (
    admin,
    canary,
    admin_query,
    account_initialization,
    cash_flow,
    dashboard,
    health,
    hydra_relay,
    market_data,
    ops,
    orders,
    trade_result,
)
from app.exceptions import APIError, ErrorCode
from app.logging_setup import get_logger, setup_logging
from app.settings import Settings, get_settings
from app.services.reconcile import OwnershipOverlap, ReconcileService


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    log = get_logger("app")
    log.info("server_starting", version="2.3.0")

    # 启动校验：owned_symbols 两两不重叠（多实例共用 QMT 账户时防止 symbol 归属冲突）
    try:
        from app.db import init_db, make_engine, make_session_factory
        settings = get_settings()
        _engine = make_engine(settings.db_url)
        init_db(_engine)
        _sf = make_session_factory(_engine)
        ReconcileService(_sf).validate_no_overlap()
        log.info("validate_no_overlap: OK (owned_symbols 无重叠)")
    except OwnershipOverlap as e:
        log.error("validate_no_overlap FAILED — owned_symbols 有重叠，请检查 strategies.yaml: %s", e)
        raise SystemExit(1) from e
    except Exception as e:
        log.warning("validate_no_overlap 跳过（DB 尚未初始化或无 instance_state 表）: %s", e)

    # 启动 APScheduler
    # ⚠ 多 worker 部署注意：每个 worker 各起一个 scheduler，cron 会重复触发。
    #    管线幂等（同 trade_date 先清后写）可容忍，但建议调度跑在单进程
    #    （uvicorn --workers 1，或独立 scheduler 进程）以免并发写 SQLite 抢锁。
    app.state.scheduler = None
    try:
        settings = getattr(app.state, "settings", None) or get_settings()
        if settings.scheduler_enabled:
            from app.db import make_engine, make_session_factory
            from app.dependencies import (
                get_blacklist_service, get_orders_queue_service,
                get_parquet_store, get_perf_service, get_shadow_ledger_service,
                get_strategy_pipeline,
            )
            from app.scheduler.runtime import make_scheduler

            _eng = make_engine(settings.db_url)
            _sf2 = make_session_factory(_eng)
            store = get_parquet_store(settings)
            pipeline = get_strategy_pipeline(
                settings=settings, sf=_sf2, store=store,
                orders_queue=get_orders_queue_service(_sf2),
                perf=get_perf_service(_sf2, store),
                blacklist=get_blacklist_service(_sf2),
            )
            shadow = get_shadow_ledger_service(
                settings=settings, sf=_sf2, store=store,
            )

            def _pipeline_run(trade_date: int) -> dict:
                return pipeline.run(trade_date)

            def _shadow_run(trade_date: int) -> dict:
                return shadow.run_all(trade_date)

            scheduler = make_scheduler(
                _pipeline_run,
                shadow_run=_shadow_run,
                cron_hour=settings.scheduler_cron_hour,
                cron_minute=settings.scheduler_cron_minute,
            )
            scheduler.start()
            app.state.scheduler = scheduler
            log.info(
                "scheduler_started hour=%s minute=%s staleness_guard=%sd",
                settings.scheduler_cron_hour, settings.scheduler_cron_minute,
                settings.max_data_staleness_days,
            )
        else:
            log.info("scheduler_disabled (set QMT_SCHEDULER_ENABLED=true to enable)")
    except Exception as e:
        log.warning("scheduler 启动失败: %s", e)

    yield

    sched = getattr(app.state, "scheduler", None)
    if sched is not None:
        sched.shutdown(wait=False)
    log.info("server_stopping")


def create_app(settings_override: Settings | None = None) -> FastAPI:
    if settings_override is not None:
        get_settings.cache_clear()

    settings = settings_override or get_settings()
    setup_logging(log_level=settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title="QMT Pipeline Server",
        version="2.3.0",
        lifespan=_lifespan,
    )
    # lifespan 读取这里的 settings（含 create_app 的 override），而非全局 get_settings()。
    app.state.settings = settings

    @app.exception_handler(APIError)
    async def _api_error_handler(request: Request, exc: APIError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": int(exc.code), "message": exc.message, "data": None},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=200,
            content={
                "code": int(ErrorCode.BAD_REQUEST),
                "message": f"请求参数不合法: {exc.errors()}",
                "data": None,
            },
        )

    app.include_router(health.router, tags=["health"])
    app.include_router(market_data.router, tags=["market-data"])
    from app.api import live_qmt_backup
    app.include_router(live_qmt_backup.router, tags=["live-qmt-backup"])
    app.include_router(orders.router, tags=["orders"])
    app.include_router(trade_result.router, tags=["trade-result"])
    app.include_router(cash_flow.router, tags=["cash-flow"])
    app.include_router(account_initialization.router, tags=["account-initialization"])
    app.include_router(hydra_relay.router, tags=["hydra-relay"])
    app.include_router(canary.router, tags=["hydra-canary"])
    app.include_router(admin.router, tags=["admin"])
    app.include_router(admin_query.router, tags=["admin-query"])
    app.include_router(ops.router, tags=["ops"])
    app.include_router(dashboard.router, tags=["dashboard"])

    if settings_override is not None:
        app.dependency_overrides[get_settings] = lambda: settings_override

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
