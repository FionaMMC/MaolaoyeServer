"""POST /trade-result — stub。Plan 10 实现真实业务。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.schemas.common import APIResponse
from app.schemas.trade_result import TradeResultRequest, TradeResultResponseData

router = APIRouter()


@router.post(
    "/trade-result",
    response_model=APIResponse[TradeResultResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def push_trade_result(req: TradeResultRequest):
    """[STUB] 接收成交回报。"""
    return APIResponse[TradeResultResponseData](
        code=0,
        message="ok (stub)",
        data=TradeResultResponseData(
            trade_date=req.trade_date,
            matched_count=0,
            unmatched_order_ids=[r.order_id for r in req.results],
        ),
    )
