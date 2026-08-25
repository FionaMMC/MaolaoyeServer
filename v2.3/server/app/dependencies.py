"""共享 Depends 工厂：Settings → Engine → SessionFactory → Services。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import Depends
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.db import init_db, make_engine, make_session_factory
from app.scheduler.pipeline import StrategyPipeline
from app.services.account_initialization import AccountInitializationService
from app.services.aggregate import AggregateService
from app.services.alerts import AlertEngine
from app.services.blacklist import BlacklistService
from app.services.cash_flow import CashFlowService
from app.services.data_upload import DataUploadService
from app.services.hydra_data import HydraDataStore
from app.services.hydra_relay import HydraRelayService, HydraRiskLimits
from app.services.ingest import IngestService
from app.services.metrics import MetricsService
from app.services.ops_monitor import OpsMonitorService
from app.services.orders_queue import OrdersQueueService
from app.services.perf import PerfService
from app.services.precheck import PrecheckService
from app.services.reconcile import ReconcileService
from app.services.settlement import SettlementService
from app.services.shadow_ledger import ShadowLedgerService
from app.settings import Settings, get_settings
from app.storage.parquet import ParquetStore
from app.strategy.loader import load_plugins


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


def get_cash_flow_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> CashFlowService:
    return CashFlowService(session_factory=sf)


def get_account_initialization_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> AccountInitializationService:
    return AccountInitializationService(session_factory=sf)


def get_hydra_data_store(
    settings: Settings = Depends(get_settings),
) -> HydraDataStore:
    return HydraDataStore(root=settings.parquet_root)


def get_blacklist_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> BlacklistService:
    return BlacklistService(session_factory=sf)


def get_hydra_relay_service(
    sf: sessionmaker = Depends(get_session_factory),
    store: HydraDataStore = Depends(get_hydra_data_store),
    settings: Settings = Depends(get_settings),
    blacklist: BlacklistService = Depends(get_blacklist_service),
) -> HydraRelayService:
    symbols = {
        value.strip()
        for value in settings.hydra_allowed_symbols_csv.split(",")
        if value.strip()
    }
    publishers = {
        value.strip()
        for value in settings.hydra_allowed_publisher_commits_csv.split(",")
        if value.strip()
    }
    return HydraRelayService(
        sf,
        store,
        allowed_symbols=symbols,
        allowed_publisher_commits=publishers,
        live_enabled=settings.live_order_generation_enabled,
        live_limits=HydraRiskLimits(
            max_daily_orders=settings.live_max_daily_orders,
            max_single_order_notional=settings.live_max_single_order_notional,
            max_daily_buy_notional=settings.live_max_daily_buy_notional,
            max_daily_sell_notional=settings.live_max_daily_sell_notional,
            max_daily_turnover_notional=settings.live_max_daily_turnover_notional,
            max_price_offset_bps=settings.live_max_price_offset_bps,
            mode=settings.hydra_live_risk_mode,
            auto_max_daily_orders=settings.live_auto_max_daily_orders,
            auto_buffer_bps=settings.live_auto_buffer_bps,
        ),
        blacklist_service=blacklist,
    )


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


def get_perf_service(
    sf: sessionmaker = Depends(get_session_factory),
    store: ParquetStore = Depends(get_parquet_store),
) -> PerfService:
    return PerfService(session_factory=sf, parquet_store=store)


def get_metrics_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> MetricsService:
    return MetricsService(session_factory=sf)


def get_reconcile_service(
    sf: sessionmaker = Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
) -> ReconcileService:
    return ReconcileService(
        session_factory=sf,
        strategies_file=Path(settings.strategies_file),
    )


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
        max_staleness_days=settings.max_data_staleness_days,
        freshness_probe_category=settings.data_freshness_probe_category,
        freshness_probe_symbol=settings.data_freshness_probe_symbol,
        live_order_generation_enabled=settings.live_order_generation_enabled,
    )


def get_data_upload_service(
    settings: Settings = Depends(get_settings),
) -> DataUploadService:
    registry = _strategy_registry(str(settings.plugins_dir))
    return DataUploadService(registry=registry)


def get_shadow_ledger_service(
    settings: Settings = Depends(get_settings),
    sf: sessionmaker = Depends(get_session_factory),
    store: ParquetStore = Depends(get_parquet_store),
) -> ShadowLedgerService:
    return ShadowLedgerService(
        session_factory=sf,
        parquet_store=store,
        config_path=Path(settings.strategies_file),
    )


def get_ops_monitor(
    sf=Depends(get_session_factory),
    store: ParquetStore = Depends(get_parquet_store),
    settings: Settings = Depends(get_settings),
) -> OpsMonitorService:
    return OpsMonitorService(sf, parquet_store=store, settings=settings)


def get_alert_engine(
    ops: OpsMonitorService = Depends(get_ops_monitor),
    sf=Depends(get_session_factory),
) -> AlertEngine:
    from sqlalchemy import select
    from app.models import InstanceState, PerfSnapshot
    with sf() as s:
        a = {r[0] for r in s.execute(select(InstanceState.instance_id)).all()}
        b = {r[0] for r in s.execute(select(PerfSnapshot.instance_id).distinct()).all()}
    return AlertEngine(ops, instances=sorted(a | b))
