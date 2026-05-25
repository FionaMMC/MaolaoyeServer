"""FastAPI 入口：create_app + lifespan + router 注册。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import admin, admin_query, dashboard, health, market_data, orders, trade_result
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
        from app.dependencies import get_session_factory, get_engine
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
    scheduler = None
    try:
        from app.scheduler.runtime import make_scheduler  # noqa: F401
        # 注意：这里直接构造 pipeline（绕开 Depends，因为 Depends 只在请求里有）
        # ...实际部署时 scheduler 应该用同样的 Settings 实例
        log.info("scheduler 已预备（实际启动需要 Plan 13 部署文档配置）")
    except Exception as e:
        log.warning("scheduler 启动失败: %s", e)

    yield

    if scheduler:
        scheduler.shutdown(wait=False)
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
    app.include_router(orders.router, tags=["orders"])
    app.include_router(trade_result.router, tags=["trade-result"])
    app.include_router(admin.router, tags=["admin"])
    app.include_router(admin_query.router, tags=["admin-query"])
    app.include_router(dashboard.router, tags=["dashboard"])

    if settings_override is not None:
        app.dependency_overrides[get_settings] = lambda: settings_override

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
