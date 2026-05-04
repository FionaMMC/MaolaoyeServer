"""Health 端点：liveness + readiness。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.settings import Settings, get_settings

router = APIRouter()


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    """Liveness probe — 进程在跑就 OK。永远不查依赖。"""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Readiness probe — 检查依赖是否就绪，未就绪返回 503。"""
    checks = {}

    # Parquet 仓库根目录
    checks["parquet_root"] = "ok" if settings.parquet_root.exists() else (
        f"missing: {settings.parquet_root}"
    )

    all_ok = all(v == "ok" for v in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "checks": checks}

    return {"status": "ready", "checks": checks}
