"""共享 Depends 工厂：Settings → Engine → SessionFactory → Services。"""
from __future__ import annotations

from functools import lru_cache

from fastapi import Depends
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.db import init_db, make_engine, make_session_factory
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


from app.services.settlement import SettlementService


def get_settlement_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> SettlementService:
    return SettlementService(session_factory=sf)


from app.services.perf import PerfService


def get_perf_service(
    sf: sessionmaker = Depends(get_session_factory),
    store: ParquetStore = Depends(get_parquet_store),
) -> PerfService:
    return PerfService(session_factory=sf, parquet_store=store)
