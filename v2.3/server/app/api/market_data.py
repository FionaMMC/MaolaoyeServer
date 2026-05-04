"""POST /market-data — stub。Plan 05 实现真实业务。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.schemas.common import APIResponse
from app.schemas.market_data import (
    MarketDataReceived,
    MarketDataRequest,
    MarketDataResponseData,
)

router = APIRouter()


@router.post(
    "/market-data",
    response_model=APIResponse[MarketDataResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def push_market_data(req: MarketDataRequest):
    """[STUB] 接收 client 推送的当日行情。"""
    return APIResponse[MarketDataResponseData](
        code=0,
        message="ok (stub)",
        data=MarketDataResponseData(
            trade_date=req.trade_date,
            received=MarketDataReceived(
                stocks=len(req.stocks),
                indexes=len(req.indexes),
                etfs=len(req.etfs),
            ),
            strategy_triggered=False,
        ),
    )
