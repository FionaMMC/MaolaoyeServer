"""GET /orders — 真实业务：从 SQLite 读取 PENDING 订单。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth import verify_api_key
from app.dependencies import get_orders_queue_service
from app.schemas.common import APIResponse
from app.schemas.orders import OrdersResponseData
from app.services.orders_queue import OrdersQueueService

router = APIRouter()


@router.get(
    "/orders",
    response_model=APIResponse[OrdersResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def get_orders(
    date: str = Query(min_length=8, max_length=8, pattern=r"^\d{8}$"),
    service: OrdersQueueService = Depends(get_orders_queue_service),
):
    items = service.list_pending(valid_date=date)
    return APIResponse[OrdersResponseData](
        code=0,
        message="ok",
        data=OrdersResponseData(date=date, orders=items),
    )
