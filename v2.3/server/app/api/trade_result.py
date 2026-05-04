"""POST /trade-result — 真实业务：拆单更新虚拟账本 + 标记订单状态。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.dependencies import get_settlement_service
from app.schemas.common import APIResponse
from app.schemas.trade_result import TradeResultRequest, TradeResultResponseData
from app.services.settlement import SettlementService

router = APIRouter()


@router.post(
    "/trade-result",
    response_model=APIResponse[TradeResultResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def push_trade_result(
    req: TradeResultRequest,
    service: SettlementService = Depends(get_settlement_service),
):
    data = service.settle(trade_date=req.trade_date, results=req.results)
    return APIResponse[TradeResultResponseData](
        code=0,
        message="ok",
        data=data,
    )
