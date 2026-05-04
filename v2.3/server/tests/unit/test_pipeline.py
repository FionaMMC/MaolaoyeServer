"""StrategyPipeline 集成测试（用真实 SQLite + 真实 Parquet + 内联策略）"""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, Order, RawSignal as RawSignalRow
from app.scheduler.pipeline import StrategyPipeline
from app.services.aggregate import AggregateService
from app.services.orders_queue import OrdersQueueService
from app.services.perf import PerfService
from app.services.precheck import PrecheckService
from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal, Strategy


# ── 测试用策略 ───────────────────────────────────────────────────────
class AlwaysBuyStrategy(Strategy):
    """每天买茅台 100 股。"""
    name = "always_buy"
    def run(self, ctx, trade_date):
        return [RawSignal(
            symbol="600519.SH", direction="BUY", quantity=100,
            reference_price=10.0, price_offset=0.005,
        )]


class NoopStrategy(Strategy):
    name = "noop"
    def run(self, ctx, trade_date):
        return []


# ── fixtures ──────────────────────────────────────────────────────
def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _bar(d: int, close: float = 10.0) -> dict:
    return {"trade_date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": 1000, "amount": close * 1000,
            "suspendFlag": 0}


@pytest.fixture
def setup(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    sf = make_session_factory(engine)
    store = ParquetStore(root=tmp_path / "data")
    yaml_path = tmp_path / "strategies.yaml"

    pipeline = StrategyPipeline(
        registry={"always_buy": AlwaysBuyStrategy, "noop": NoopStrategy},
        parquet_store=store,
        session_factory=sf,
        precheck=PrecheckService(fee_rate=0.001),
        aggregate=AggregateService(),
        orders_queue=OrdersQueueService(session_factory=sf),
        perf=PerfService(session_factory=sf, parquet_store=store),
        strategies_yaml_path=yaml_path,
    )
    return pipeline, sf, store, yaml_path


def _write_yaml(path: Path, content: dict):
    path.write_text(yaml.safe_dump(content), encoding="utf-8")


# ── 测试 ──────────────────────────────────────────────────────────
def test_pipeline_no_yaml_returns_zero(setup):
    pipeline, sf, store, yaml_path = setup
    summary = pipeline.run(20260430)
    assert summary["instances"] == 0
    assert summary["orders"] == 0


def test_pipeline_creates_default_instance_state(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "noop", "virtual_initial_cash": 500_000}],
        }],
    })
    pipeline.run(20260430)

    with sf() as s:
        row = s.get(InstanceState, "real_A_noop")
        assert row is not None
        assert row.virtual_cash == 500_000


def test_pipeline_runs_strategy_and_creates_orders(setup):
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 1_000_000}],
        }],
    })
    summary = pipeline.run(20260430)

    assert summary["signals"] == 1
    assert summary["passed"] == 1
    assert summary["orders"] == 1

    with sf() as s:
        # raw_signals 表里应该有一条 PASS 记录
        signals = s.query(RawSignalRow).all()
        assert len(signals) == 1
        assert signals[0].precheck_status == "PASS"
        # orders 表里应该有一条 PENDING 订单
        orders = s.query(Order).all()
        assert len(orders) == 1
        assert orders[0].status == "PENDING"
        assert orders[0].symbol == "600519.SH"


def test_pipeline_aggregates_across_instances(setup):
    """两个实例同账户组同标的同方向 → 归集为一条订单。"""
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [
                {"strategy_id": "always_buy", "virtual_initial_cash": 500_000},
                # 第二个 strategy 也是 always_buy 用不同 strategy_id
            ],
        }],
    })
    # 先跑一次确认基线
    pipeline.run(20260430)
    with sf() as s:
        assert s.query(Order).count() == 1


def test_pipeline_precheck_fails_blocks_signal(setup):
    """资金不够：信号被预检拒绝。"""
    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "always_buy", "virtual_initial_cash": 100}],  # 不够买 100*10=1000
        }],
    })
    summary = pipeline.run(20260430)

    assert summary["signals"] == 1
    assert summary["passed"] == 0   # 预检拒绝
    assert summary["orders"] == 0   # 无归集

    with sf() as s:
        sigs = s.query(RawSignalRow).all()
        assert sigs[0].precheck_status == "FAIL"


def test_pipeline_runs_perf_snapshot(setup):
    """pipeline 跑完应产出 NAV 快照。"""
    from app.models import PerfSnapshot

    pipeline, sf, store, yaml_path = setup
    _write_yaml(yaml_path, {
        "account_groups": [{
            "group_id": "real_A",
            "qmt_account_id": "X",
            "strategies": [{"strategy_id": "noop", "virtual_initial_cash": 1000}],
        }],
    })
    pipeline.run(20260430)

    with sf() as s:
        snap = s.get(PerfSnapshot, ("real_A_noop", "20260430"))
        assert snap is not None
        assert snap.nav == 1000.0
