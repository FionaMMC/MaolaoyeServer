"""FastAPI 入口：create_app + lifespan + router 注册。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.api import health
from app.logging_setup import get_logger, setup_logging
from app.settings import Settings, get_settings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用启停时的资源管理。"""
    log = get_logger("app")
    log.info("server_starting", version="2.3.0")
    yield
    log.info("server_stopping")


def create_app(settings_override: Settings | None = None) -> FastAPI:
    """工厂函数。测试时传 settings_override 注入隔离配置。"""
    if settings_override is not None:
        get_settings.cache_clear()

    settings = settings_override or get_settings()
    setup_logging(log_level=settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title="QMT Pipeline Server",
        version="2.3.0",
        lifespan=_lifespan,
    )
    app.include_router(health.router, tags=["health"])

    if settings_override is not None:
        app.dependency_overrides[get_settings] = lambda: settings_override

    return app


# uvicorn 入口：uvicorn app.main:app --host 0.0.0.0 --port 8000
app = create_app()


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
