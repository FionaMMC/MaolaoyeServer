"""POST /market-data — 真实业务：通过 IngestService 写 Parquet。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.dependencies import get_ingest_service
from app.schemas.common import APIResponse
from app.schemas.market_data import MarketDataRequest, MarketDataResponseData
from app.services.ingest import IngestService

router = APIRouter()


@router.post(
    "/market-data",
    response_model=APIResponse[MarketDataResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def push_market_data(
    req: MarketDataRequest,
    ingest: IngestService = Depends(get_ingest_service),
):
    """接收 client 推送的当日行情，写入 Parquet 仓库。"""
    data = ingest.ingest(req)
    return APIResponse[MarketDataResponseData](
        code=0,
        message="ok",
        data=data,
    )
