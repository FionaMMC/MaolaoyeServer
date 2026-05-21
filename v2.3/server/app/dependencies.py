"""共享 Depends 工厂：Settings → Engine → SessionFactory → Services。"""
from __future__ import annotations

from functools import lru_cache

from fastapi import Depends
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.db import init_db, make_engine, make_session_factory
from app.services.blacklist import BlacklistService
from app.services.ingest import IngestService
from app.services.orders_queue import OrdersQueueService
from app.settings import Settings, get_settings
from app.storage.parquet import ParquetStore


@lru_cache(maxsize=8)
def _engine_for_url(db_url: str) -> Engine:
    eng = make_engine(db_url)
    init_db(eng)
    return eng


def get_engine(settings: Settings = Depends(get_settings)) -> Engine:
    return _engine_for_url(settings.db_url)


def get_session_factory(engine: Engine = Depends(get_engine)) -> sessionmaker:
    return make_session_factory(engine)


def get_parquet_store(settings: Settings = Depends(get_settings)) -> ParquetStore:
    return ParquetStore(root=settings.parquet_root)


def get_ingest_service(
    store: ParquetStore = Depends(get_parquet_store),
) -> IngestService:
    return IngestService(parquet_store=store)


def get_orders_queue_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> OrdersQueueService:
    return OrdersQueueService(session_factory=sf)


def get_blacklist_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> BlacklistService:
    return BlacklistService(session_factory=sf)


from app.services.settlement import SettlementService


def get_settlement_service(
    sf: sessionmaker = Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
) -> SettlementService:
    return SettlementService(
        session_factory=sf,
        commission_rate=settings.stock_commission_rate,
        min_commission=settings.stock_min_commission,
        stamp_duty_sell=settings.stock_stamp_duty_sell,
    )


from app.services.perf import PerfService


def get_perf_service(
    sf: sessionmaker = Depends(get_session_factory),
    store: ParquetStore = Depends(get_parquet_store),
) -> PerfService:
    return PerfService(session_factory=sf, parquet_store=store)


from app.services.metrics import MetricsService


def get_metrics_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> MetricsService:
    return MetricsService(session_factory=sf)


from app.services.reconcile import ReconcileService


def get_reconcile_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> ReconcileService:
    return ReconcileService(session_factory=sf)


from pathlib import Path

from app.scheduler.pipeline import StrategyPipeline
from app.services.aggregate import AggregateService
from app.services.precheck import PrecheckService
from app.strategy.loader import load_plugins


# 全局策略注册表（启动时一次性加载）
@lru_cache(maxsize=1)
def _strategy_registry(plugins_dir: str) -> dict:
    return load_plugins(Path(plugins_dir))


def get_strategy_pipeline(
    settings: Settings = Depends(get_settings),
    sf: sessionmaker = Depends(get_session_factory),
    store: ParquetStore = Depends(get_parquet_store),
    orders_queue: OrdersQueueService = Depends(get_orders_queue_service),
    perf: PerfService = Depends(get_perf_service),
    blacklist: BlacklistService = Depends(get_blacklist_service),
) -> StrategyPipeline:
    return StrategyPipeline(
        registry=_strategy_registry(str(settings.plugins_dir)),
        parquet_store=store,
        session_factory=sf,
        precheck=PrecheckService(fee_rate=0.001),
        aggregate=AggregateService(),
        orders_queue=orders_queue,
        perf=perf,
        strategies_yaml_path=Path(settings.strategies_file),
        blacklist=blacklist,
    )


from app.services.data_upload import DataUploadService


def get_data_upload_service(
    settings: Settings = Depends(get_settings),
) -> DataUploadService:
    registry = _strategy_registry(str(settings.plugins_dir))
    return DataUploadService(registry=registry)
