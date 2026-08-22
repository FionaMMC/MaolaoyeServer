from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from app.db import init_db, make_engine, make_session_factory
from app.models import InstanceState, Order
from app.scheduler.pipeline import StrategyPipeline
from app.services.aggregate import AggregateService
from app.services.orders_queue import OrdersQueueService
from app.services.perf import PerfService
from app.services.precheck import PrecheckService
from app.storage.parquet import ParquetStore
from plugins.v713_relay import V713RelayAdapter, basket_hash


def build(tmp_path: Path, monkeypatch, *, live: bool):
    engine = make_engine(f"sqlite:///{tmp_path}/v713.db")
    init_db(engine)
    sf = make_session_factory(engine)
    store = ParquetStore(tmp_path / "market")
    store.append("etfs", "511260.SH", pd.DataFrame({
        "trade_date": [20260701], "close": [100.0], "volume": [1_000_000],
    }))
    data_dir = tmp_path / "target"
    data_dir.mkdir()
    frame = pd.DataFrame([{
        "code": "511260.SH", "weight": 1.0, "strategy_version": "v7.13-base",
        "sleeve": "AUX_HYDRA", "decision_date": "20260701",
        "as_of_date": "20260630",
    }])
    frame["basket_sha256"] = basket_hash(frame)
    frame.to_parquet(data_dir / "v713_target_latest.parquet", index=False)
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {
        "dry_run": not live, "cash_buffer": 0.01,
        "max_target_age_days": 7, "risk_filters": {},
    }
    config = tmp_path / "strategies.yaml"
    account_groups = []
    if live:
        # 证明 V7.13 在与另一实例共享同一 QMT 账户时仍可通过严格护栏；
        # 归属来自订单血缘，不靠 owned_symbols 抢占真实持仓。
        account_groups.append({
            "group_id": "paper_v53", "qmt_account_id": "SHARED",
            "strategies": [{
                "strategy_id": "noop", "virtual_initial_cash": 1_000_000,
                "owned_symbols": ["510300.SH"],
            }],
        })
    account_groups.append({
        "group_id": "paper_v79", "qmt_account_id": "SHARED" if live else None,
        "strategies": [{
            "strategy_id": "v713_relay", "virtual_initial_cash": 1_000_000,
            "orders_enabled": live,
            "account_isolation": "shared_ledger" if live else "none",
            "requires_reconciled_rebalance": True,
            "owned_symbols": [],
        }],
    })
    config.write_text(
        yaml.safe_dump({"account_groups": account_groups}), encoding="utf-8",
    )
    pipeline = StrategyPipeline(
        registry={"v713_relay": V713RelayAdapter}, parquet_store=store,
        session_factory=sf, precheck=PrecheckService(), aggregate=AggregateService(),
        orders_queue=OrdersQueueService(sf), perf=PerfService(sf, store),
        strategies_yaml_path=config,
    )
    return pipeline, sf


def test_offline_replay_records_hash_and_second_run_skips(tmp_path, monkeypatch):
    pipeline, sf = build(tmp_path, monkeypatch, live=False)
    assert pipeline.run(20260701)["orders"] == 0
    with sf() as session:
        state = session.get(InstanceState, "paper_v79_v713_relay").strategy_state
        assert state["last_replayed_basket_sha256"]
        assert state["last_target_quantities"] == {"511260.SH": 9900}
    assert pipeline.run(20260701)["orders"] == 0
    with sf() as session:
        assert session.query(Order).count() == 0
    V713RelayAdapter._cfg = None


def test_offline_replay_ignores_new_artifact_for_same_month(tmp_path, monkeypatch):
    pipeline, sf = build(tmp_path, monkeypatch, live=False)
    assert pipeline.run(20260701)["orders"] == 0

    target_path = V713RelayAdapter.data_dir / "v713_target_latest.parquet"
    target = pd.read_parquet(target_path)
    target["decision_date"] = "20260702"
    target["basket_sha256"] = basket_hash(target)
    target.to_parquet(target_path, index=False)

    second = pipeline.run(20260702)

    assert second["signals"] == 0
    assert second["orders"] == 0
    with sf() as session:
        state = session.get(InstanceState, "paper_v79_v713_relay").strategy_state
        assert state["last_replayed_as_of_date"] == "20260630"
        assert state["last_ignored_reason"] == "monthly_cycle_already_consumed"
        assert session.query(Order).count() == 0
    V713RelayAdapter._cfg = None


def test_live_server_order_is_preserved_by_strict_pending_guard(tmp_path, monkeypatch):
    pipeline, sf = build(tmp_path, monkeypatch, live=True)
    first = pipeline.run(20260701)
    assert first["orders"] == 1
    with sf() as session:
        order_id = session.query(Order).one().order_id
    second = pipeline.run(20260701)
    assert second["skipped"] == "strict_rebalance_blocked"
    assert second["trade_date"] == 20260701
    assert second["valid_date"] == "20260701"
    assert set(second["blockers"]["paper_v79_v713_relay"]) == {"unresolved_order"}
    with sf() as session:
        assert session.get(Order, order_id) is not None
    V713RelayAdapter._cfg = None
