"""GET /orders — 真实业务：从 SQLite 读取 PENDING 订单。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_orders_queue_service
from app.exceptions import APIError, ErrorCode
from app.schemas.common import APIResponse
from app.schemas.orders import OrdersResponseData
from app.services.orders_queue import OrdersQueueService
from app.settings import Settings, get_settings

router = APIRouter()


@router.get(
    "/orders",
    response_model=APIResponse[OrdersResponseData],
)
async def get_orders(
    date: str = Query(min_length=8, max_length=8, pattern=r"^\d{8}$"),
    auth: AuthContext = Depends(verify_api_key),
    settings: Settings = Depends(get_settings),
    service: OrdersQueueService = Depends(get_orders_queue_service),
):
    if auth.execution_domain == "live" and not settings.live_order_delivery_enabled:
        raise APIError(
            ErrorCode.STRATEGY_PENDING,
            "live 订单领取闸门关闭",
            http_status=423,
        )
    items = service.list_pending(
        valid_date=date,
        execution_domain=auth.execution_domain,
        allowed_account_aliases=auth.allowed_account_aliases,
    )
    return APIResponse[OrdersResponseData](
        code=0,
        message="ok",
        data=OrdersResponseData(date=date, orders=items),
    )
