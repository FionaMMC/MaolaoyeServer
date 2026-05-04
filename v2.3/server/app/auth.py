"""Bearer token 鉴权 dependency。"""
from __future__ import annotations

from fastapi import Depends, Header

from app.exceptions import APIError, ErrorCode
from app.settings import Settings, get_settings


async def verify_api_key(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """检查 Authorization: Bearer {API_KEY}。失败抛 APIError(1001)。"""
    if not settings.api_key:
        raise APIError(ErrorCode.AUTH_FAILED, "server api_key 未配置", http_status=401)

    if not authorization or not authorization.startswith("Bearer "):
        raise APIError(ErrorCode.AUTH_FAILED, "缺少 Bearer token", http_status=401)

    provided = authorization[len("Bearer "):]
    if provided != settings.api_key:
        raise APIError(ErrorCode.AUTH_FAILED, "API key 不匹配", http_status=401)
