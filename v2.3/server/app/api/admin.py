"""Internal /admin endpoints — 联测时人工触发管线。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth import verify_api_key
from app.dependencies import get_strategy_pipeline
from app.scheduler.pipeline import StrategyPipeline
from app.schemas.common import APIResponse

router = APIRouter(prefix="/admin")


@router.post(
    "/run-pipeline",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def run_pipeline_now(
    trade_date: int = Query(ge=20000101, le=99991231),
    pipeline: StrategyPipeline = Depends(get_strategy_pipeline),
):
    """同步触发整条策略管线，返回摘要。供联测/灾备使用。"""
    summary = pipeline.run(trade_date)
    return APIResponse[dict](code=0, message="ok", data=summary)
