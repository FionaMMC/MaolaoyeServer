"""Hydra 实盘 16:00 专用触发入口（已停用，保留路由以返回 410）。

这不是 admin 接口：只有专用 trigger token 能调用，且始终固定为 live
执行域。行情备份、订单领取和管理接口均不在此 token 的权限范围内。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth import AuthContext, verify_api_key
from app.exceptions import APIError, ErrorCode
from app.schemas.common import APIResponse


router = APIRouter(prefix="/hydra/live")


@router.post("/trigger", response_model=APIResponse[dict])
async def trigger_live_pipeline(
    trade_date: int = Query(ge=20000101, le=99991231),
    force: bool = Query(
        False,
        description="仅用于人工事故处置；正式定时任务不得传 true。",
    ),
    auth: AuthContext = Depends(verify_api_key),
):
    """已停用：旧实现会在无券商证据时把实盘单标为过期并删单。"""
    # token 已在 verify_api_key 处被严格限定到这个 path；这里仍保留断言，
    # 防止日后路由/鉴权重构时出现静默跨域。
    if auth.execution_domain != "live" or auth.client_id != "live-trigger":
        raise APIError(ErrorCode.AUTH_FAILED, "仅 live trigger token 可触发实盘管线", http_status=403)
    raise APIError(ErrorCode.BAD_REQUEST, "实盘管线入口已停用；实盘执行由 /oms/live 负责", http_status=410)
