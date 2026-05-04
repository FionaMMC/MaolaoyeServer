"""共享 Depends 工厂（避免在路由文件里重复构造服务）。"""
from __future__ import annotations

from fastapi import Depends

from app.services.ingest import IngestService
from app.settings import Settings, get_settings
from app.storage.parquet import ParquetStore


def get_parquet_store(settings: Settings = Depends(get_settings)) -> ParquetStore:
    return ParquetStore(root=settings.parquet_root)


def get_ingest_service(
    store: ParquetStore = Depends(get_parquet_store),
) -> IngestService:
    return IngestService(parquet_store=store)
