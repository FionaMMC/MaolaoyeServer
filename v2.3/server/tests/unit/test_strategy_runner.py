"""StrategyRunner 测试"""
from pathlib import Path

from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal, Strategy
from app.strategy.runner import StrategyRunner


class FixedBuyStrategy(Strategy):
    name = "fixed_buy"
    def run(self, ctx, trade_date):
        return [RawSignal(symbol="600519.SH", direction="BUY",
                          quantity=100, reference_price=10.0, price_offset=0.005)]


class EmptyStrategy(Strategy):
    name = "empty"
    def run(self, ctx, trade_date):
        return []


class CrashingStrategy(Strategy):
    name = "crash"
    def run(self, ctx, trade_date):
        raise RuntimeError("boom from strategy")


class BadReturnStrategy(Strategy):
    name = "bad_return"
    def run(self, ctx, trade_date):
        return "not a list"


def _runner(tmp_path: Path) -> StrategyRunner:
    return StrategyRunner(
        registry={
            "fixed_buy": FixedBuyStrategy,
            "empty": EmptyStrategy,
            "crash": CrashingStrategy,
            "bad_return": BadReturnStrategy,
        },
        parquet_store=ParquetStore(root=tmp_path),
    )


def test_runner_runs_single_instance(tmp_path: Path):
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[{
        "instance_id": "real_A_fixed_buy",
        "strategy_id": "fixed_buy",
        "virtual_cash": 1_000_000.0,
        "virtual_positions": {},
    }])
    assert "real_A_fixed_buy" in out
    sigs = out["real_A_fixed_buy"]
    assert len(sigs) == 1
    assert sigs[0].symbol == "600519.SH"


def test_runner_iterates_multiple_instances(tmp_path: Path):
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[
        {"instance_id": "i1", "strategy_id": "fixed_buy",
         "virtual_cash": 100.0, "virtual_positions": {}},
        {"instance_id": "i2", "strategy_id": "empty",
         "virtual_cash": 100.0, "virtual_positions": {}},
    ])
    assert len(out["i1"]) == 1
    assert out["i2"] == []


def test_runner_unknown_strategy_id_logs_and_returns_empty(tmp_path: Path):
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[
        {"instance_id": "i1", "strategy_id": "nonexistent",
         "virtual_cash": 0.0, "virtual_positions": {}},
    ])
    assert out["i1"] == []


def test_runner_crashing_strategy_isolated(tmp_path: Path):
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[
        {"instance_id": "crash_i", "strategy_id": "crash",
         "virtual_cash": 0.0, "virtual_positions": {}},
        {"instance_id": "good_i", "strategy_id": "fixed_buy",
         "virtual_cash": 0.0, "virtual_positions": {}},
    ])
    assert out["crash_i"] == []
    assert len(out["good_i"]) == 1


def test_runner_bad_return_type_isolated(tmp_path: Path):
    runner = _runner(tmp_path)
    out = runner.run_all(20260430, instances=[
        {"instance_id": "i1", "strategy_id": "bad_return",
         "virtual_cash": 0.0, "virtual_positions": {}},
    ])
    assert out["i1"] == []
