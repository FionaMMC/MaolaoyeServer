"""POST /trade-result — 真实业务：拆单更新虚拟账本 + 标记订单状态。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_settlement_service
from app.exceptions import APIError, ErrorCode
from app.schemas.common import APIResponse
from app.schemas.trade_result import TradeResultRequest, TradeResultResponseData
from app.services.settlement import SettlementService

router = APIRouter()


@router.post(
    "/trade-result",
    response_model=APIResponse[TradeResultResponseData],
)
async def push_trade_result(
    req: TradeResultRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: SettlementService = Depends(get_settlement_service),
):
    if req.execution_domain != auth.execution_domain:
        raise APIError(
            ErrorCode.AUTH_FAILED,
            "成交回报 execution_domain 与 token 身份不一致",
            http_status=403,
        )
    data = service.settle(
        trade_date=req.trade_date,
        results=req.results,
        execution_domain=auth.execution_domain,
        allowed_account_aliases=auth.allowed_account_aliases,
    )
    return APIResponse[TradeResultResponseData](
        code=0,
        message="ok",
        data=data,
    )
