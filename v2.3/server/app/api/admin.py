"""Admin 端点：人工触发 + 数据管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.auth import verify_api_key
from app.dependencies import (
    get_blacklist_service,
    get_data_upload_service,
    get_strategy_pipeline,
)
from app.scheduler.pipeline import StrategyPipeline
from app.schemas.common import APIResponse
from app.services.blacklist import BlacklistService
from app.services.data_upload import DataUploadService

router = APIRouter(prefix="/admin")


@router.post(
    "/run-pipeline",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def run_pipeline_now(
    trade_date: int = Query(ge=20000101, le=99991231),
    force: bool = Query(
        False,
        description="放行『已拉取』护栏强制重算。重算后必须让客户端重新拉取信号，"
                    "否则次日成交回报无法匹配（2026-07-02 事故）。",
    ),
    pipeline: StrategyPipeline = Depends(get_strategy_pipeline),
):
    """同步触发整条策略管线，返回摘要。供联测/灾备使用。"""
    summary = pipeline.run(trade_date, force=force)
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


@router.post(
    "/clear-state",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def clear_state(
    date: str = Query(min_length=8, max_length=8, pattern=r"^\d{8}$"),
    include_instance_state: bool = Query(False),
    pipeline: StrategyPipeline = Depends(get_strategy_pipeline),
    blacklist: BlacklistService = Depends(get_blacklist_service),
):
    """清除指定 trade_date 的 raw_signals + orders + order_signal_map。

    在删除前会自动把 REJECTED 的 symbol 晋升到持久 risk_blacklist 表
    （这样 clear-state 不会让黑名单丢）。

    include_instance_state=true 时还会把所有 InstanceState 的 virtual_cash 还原成
    strategies.yaml 初始值、virtual_positions 清空。**慎用**——会丢失累计仓位状态。
    """
    promoted = blacklist.auto_promote()
    cleared = pipeline._clear_for_date(date)
    info = {"date": date, "blacklist_promoted": promoted, "cleared": cleared}
    if include_instance_state:
        reset = pipeline.reset_instance_states()
        info["reset_instances"] = reset
    return APIResponse[dict](code=0, message="ok", data=info)


@router.get(
    "/blacklist",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def blacklist_status(
    lookback_days: int = Query(30, ge=1, le=3650),
    service: BlacklistService = Depends(get_blacklist_service),
):
    """诊断：自动派生（过去 N 天 REJECTED orders）+ 手工维护 entries。"""
    return APIResponse[dict](code=0, message="ok", data=service.stats(lookback_days))


@router.post(
    "/blacklist-add",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def blacklist_add(
    symbols: str = Query(min_length=1, description="逗号分隔的 QMT 代码，如 600063.SH,002001.SZ"),
    reason: str = Query("", max_length=200, description="备注，如 'delisting' / 'ST'"),
    service: BlacklistService = Depends(get_blacklist_service),
):
    """批量加入持久黑名单（不被 clear-state 影响）。已存在则更新 reason。"""
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    info = service.add_manual(sym_list, reason)
    return APIResponse[dict](code=0, message="ok", data=info)


@router.delete(
    "/blacklist-remove",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def blacklist_remove(
    symbol: str = Query(min_length=1, max_length=20),
    service: BlacklistService = Depends(get_blacklist_service),
):
    """从持久黑名单删一个 symbol。"""
    removed = service.remove_manual(symbol)
    return APIResponse[dict](
        code=0 if removed else 404,
        message="ok" if removed else "symbol not found",
        data={"symbol": symbol, "removed": removed},
    )
