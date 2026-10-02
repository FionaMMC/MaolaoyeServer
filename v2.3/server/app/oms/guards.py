"""Single writer: once the execution core owns live, legacy live write paths refuse."""
from __future__ import annotations

from fastapi import Depends

from app.auth import AuthContext, verify_api_key
from app.exceptions import APIError, ErrorCode
from app.settings import Settings, get_settings


def legacy_live_write_guard(auth: AuthContext = Depends(verify_api_key),
                            settings: Settings = Depends(get_settings)) -> AuthContext:
    if auth.execution_domain == "live" and settings.oms_live_enabled:
        raise APIError(ErrorCode.BAD_REQUEST, "实盘已由执行核心接管，旧的实盘写接口已停用", http_status=409)
    return auth
