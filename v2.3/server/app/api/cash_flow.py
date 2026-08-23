"""POST /cash-flows — domain-scoped 外部现金流入账。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_cash_flow_service
from app.exceptions import APIError, ErrorCode
from app.schemas.cash_flow import CashFlowRequest, CashFlowResponseData
from app.schemas.common import APIResponse
from app.services.cash_flow import CashFlowService
from app.settings import Settings, get_settings

router = APIRouter()


@router.post("/cash-flows", response_model=APIResponse[CashFlowResponseData])
async def post_cash_flow(
    req: CashFlowRequest,
    auth: AuthContext = Depends(verify_api_key),
    settings: Settings = Depends(get_settings),
    service: CashFlowService = Depends(get_cash_flow_service),
):
    if req.execution_domain != auth.execution_domain:
        raise APIError(
            ErrorCode.AUTH_FAILED,
            "现金流 execution_domain 与 token 身份不一致",
            http_status=403,
        )
    if not auth.allows_account(req.account_alias):
        raise APIError(
            ErrorCode.AUTH_FAILED,
            "token 无权访问该 account_alias",
            http_status=403,
        )
    if auth.execution_domain == "live" and not settings.live_cash_flow_ingest_enabled:
        raise APIError(
            ErrorCode.STRATEGY_PENDING,
            "live 现金流入账闸门关闭",
            http_status=423,
        )
    data = service.apply(req)
    return APIResponse[CashFlowResponseData](code=0, message="ok", data=data)
