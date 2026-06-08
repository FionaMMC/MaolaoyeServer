"""数据新鲜度护栏 + ParquetStore.latest_date

防止「触发器恢复后管线拿陈旧行情下单」——若最新行情比 trade_date 还旧超过容差，
管线直接跳过（不写 raw_signals / orders），返回 skipped 摘要。
"""
from pathlib import Path

import pandas as pd
import yaml

from app.db import init_db, make_engine, make_session_factory
from app.models import Order, RawSignal as RawSignalRow
from app.scheduler.pipeline import StrategyPipeline
from app.services.aggregate import AggregateService
from app.services.orders_queue import OrdersQueueService
from app.services.perf import PerfService
from app.services.precheck import PrecheckService
from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal, Strategy


class AlwaysBuyStrategy(Strategy):
    name = "always_buy"

    def run(self, ctx, trade_date):
        return [RawSignal(symbol="600519.SH", direction="BUY", quantity=100,
                          reference_price=10.0, price_offset=0.005)]


def _bar(d: int, close: float = 10.0) -> dict:
    return {"trade_date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": 1000, "amount": close * 1000, "suspendFlag": 0}


def _mk_pipeline(tmp_path: Path, max_staleness_days):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    sf = make_session_factory(engine)
    store = ParquetStore(root=tmp_path / "data")
    yaml_path = tmp_path / "strategies.yaml"
    yaml_path.write_text(yaml.safe_dump({
        "account_groups": [{
            "group_id": "real_A", "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 500_000}],
        }],
    }), encoding="utf-8")
    pipeline = StrategyPipeline(
        registry={"always_buy": AlwaysBuyStrategy},
        parquet_store=store,
        session_factory=sf,
        precheck=PrecheckService(fee_rate=0.001),
        aggregate=AggregateService(),
        orders_queue=OrdersQueueService(session_factory=sf),
        perf=PerfService(session_factory=sf, parquet_store=store),
        strategies_yaml_path=yaml_path,
        max_staleness_days=max_staleness_days,
    )
    return pipeline, sf, store


def test_latest_date(tmp_path):
    store = ParquetStore(root=tmp_path / "data")
    assert store.latest_date("indexes", "000852.SH") is None
    store.append("indexes", "000852.SH", pd.DataFrame([_bar(20260428), _bar(20260430)]))
    assert store.latest_date("indexes", "000852.SH") == 20260430


def test_pipeline_skips_on_stale_market_data(tmp_path):
    pipeline, sf, store = _mk_pipeline(tmp_path, max_staleness_days=5)
    # 行情冻结在 20260430，却为 20260608 跑（>5 天陈旧）→ 必须跳过
    store.append("indexes", "000852.SH", pd.DataFrame([_bar(20260430)]))
    store.append("stocks", "600519.SH", pd.DataFrame([_bar(20260430)]))
    summary = pipeline.run(20260608)
    assert summary.get("skipped") == "stale_market_data"
    assert summary["orders"] == 0
    with sf() as s:
        assert s.query(Order).count() == 0
        assert s.query(RawSignalRow).count() == 0


def test_pipeline_runs_when_fresh(tmp_path):
    pipeline, sf, store = _mk_pipeline(tmp_path, max_staleness_days=5)
    store.append("indexes", "000852.SH", pd.DataFrame([_bar(20260608)]))
    store.append("stocks", "600519.SH", pd.DataFrame([_bar(20260608)]))
    summary = pipeline.run(20260608)
    assert "skipped" not in summary
    assert summary["orders"] >= 1


def test_guard_disabled_when_none(tmp_path):
    # max_staleness_days=None → 不启用护栏（向后兼容旧构造）
    pipeline, sf, store = _mk_pipeline(tmp_path, max_staleness_days=None)
    store.append("stocks", "600519.SH", pd.DataFrame([_bar(20260430)]))
    summary = pipeline.run(20260608)  # 无 index 数据但护栏关
    assert "skipped" not in summary


def test_backfill_historical_date_not_stale(tmp_path):
    # 灾备/回填：为历史 trade_date 跑，行情正好在该日 → 不算陈旧
    pipeline, sf, store = _mk_pipeline(tmp_path, max_staleness_days=5)
    store.append("indexes", "000852.SH", pd.DataFrame([_bar(20260430)]))
    store.append("stocks", "600519.SH", pd.DataFrame([_bar(20260430)]))
    summary = pipeline.run(20260430)
    assert "skipped" not in summary
