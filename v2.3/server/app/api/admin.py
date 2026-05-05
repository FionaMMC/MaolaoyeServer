"""Admin 端点：人工触发 + 数据管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.auth import verify_api_key
from app.dependencies import get_data_upload_service, get_strategy_pipeline
from app.scheduler.pipeline import StrategyPipeline
from app.schemas.common import APIResponse
from app.services.data_upload import DataUploadService

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


@router.post(
    "/upload-data",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def upload_strategy_data(
    strategy: str = Query(min_length=1, max_length=100),
    filename: str = Query(min_length=1, max_length=200),
    file: UploadFile = File(...),
    service: DataUploadService = Depends(get_data_upload_service),
):
    """上传策略私有数据。需要 strategy 已声明 data_dir + data_files。"""
    body = await file.read()
    info = service.upload(strategy_name=strategy, filename=filename, body=body)
    return APIResponse[dict](code=0, message="ok", data=info)


@router.get(
    "/data-status",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def data_status(
    strategy: str = Query(min_length=1, max_length=100),
    service: DataUploadService = Depends(get_data_upload_service),
):
    info = service.status(strategy_name=strategy)
    return APIResponse[dict](code=0, message="ok", data=info)
