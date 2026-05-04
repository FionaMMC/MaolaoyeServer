"""GET /orders — stub。Plan 09 实现真实业务。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth import verify_api_key
from app.schemas.common import APIResponse
from app.schemas.orders import OrdersResponseData

router = APIRouter()


@router.get(
    "/orders",
    response_model=APIResponse[OrdersResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def get_orders(date: str = Query(min_length=8, max_length=8, pattern=r"^\d{8}$")):
    """[STUB] 返回指定日期的归集订单。"""
    return APIResponse[OrdersResponseData](
        code=0,
        message="ok (stub)",
        data=OrdersResponseData(date=date, orders=[]),
    )
