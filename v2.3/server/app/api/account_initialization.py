"""POST /accounts/initialize-from-qmt — 单次只读快照初始化。"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_account_initialization_service
from app.exceptions import APIError, ErrorCode
from app.schemas.account_initialization import (
    AccountInitializationRequest,
    AccountInitializationResponseData,
)
from app.schemas.common import APIResponse
from app.services.account_initialization import AccountInitializationService
from app.settings import Settings, get_settings

router = APIRouter(prefix="/accounts")


@router.post(
    "/initialize-from-qmt",
    response_model=APIResponse[AccountInitializationResponseData],
)
async def initialize_from_qmt(
    req: AccountInitializationRequest,
    auth: AuthContext = Depends(verify_api_key),
    settings: Settings = Depends(get_settings),
    service: AccountInitializationService = Depends(get_account_initialization_service),
):
    if req.execution_domain != auth.execution_domain or not auth.allows_account(
        req.account_alias
    ):
        raise APIError(ErrorCode.AUTH_FAILED, "QMT 初始化请求跨域/账户", http_status=403)
    if req.execution_domain == "live":
        if not settings.live_account_initialization_enabled:
            raise APIError(
                ErrorCode.STRATEGY_PENDING,
                "live 账户初始化闸门关闭",
                http_status=423,
            )
        fingerprint = hashlib.sha256(req.qmt_account_id.encode()).hexdigest()
        if (
            not settings.live_qmt_account_sha256
            or fingerprint != settings.live_qmt_account_sha256
        ):
            raise APIError(
                ErrorCode.AUTH_FAILED,
                "QMT account fingerprint 不匹配",
                http_status=403,
            )
    data = service.initialize(req)
    return APIResponse[AccountInitializationResponseData](code=0, message="ok", data=data)
