"""Missing inputs isolate one strategy and remain recoverable, never fake PnL."""

import pandas as pd

from app.models import InstanceState, PerfSnapshot
from app.services.perf import PerfService
from tests.unit.test_perf import _setup, _now, _bar
from tests.unit.test_v53_adapter import _make_bundle_in_tmp_dir, _reset_adapter_cache, _divid_df


def test_missing_price_skips_only_affected_nav_and_recovers(tmp_path):
    sf, store = _setup(tmp_path)
    with sf() as session:
        session.add_all(
            [
                InstanceState(
                    instance_id="missing",
                    virtual_cash=100,
                    virtual_positions={"510300.SH": 100},
                    last_update=_now(),
                ),
                InstanceState(
                    instance_id="healthy",
                    virtual_cash=200,
                    virtual_positions={},
                    last_update=_now(),
                ),
            ]
        )
        session.commit()
    svc = PerfService(sf, store)
    assert svc.snapshot_all(20260903) == 1
    with sf() as session:
        assert session.get(PerfSnapshot, ("missing", "20260903")) is None
        assert session.get(PerfSnapshot, ("healthy", "20260903")).nav == 200
        assert (
            session.get(InstanceState, "missing").strategy_state["valuation_status"]["status"]
            == "WAITING_PRICE"
        )
    store.append("etfs", "510300.SH", pd.DataFrame([_bar(20260903, 4.0)]))
    assert svc.snapshot_all(20260903) == 2
    with sf() as session:
        assert session.get(PerfSnapshot, ("missing", "20260903")).nav == 500
        assert session.get(InstanceState, "missing").virtual_positions == {"510300.SH": 100}


def test_missing_dividend_is_not_raw_fallback_and_late_file_recovers(tmp_path, monkeypatch):
    import pytest
    import plugins.v53_adapter as mod

    _reset_adapter_cache()
    root = _make_bundle_in_tmp_dir(tmp_path)
    (root / "data" / "etf_divid.parquet").unlink()
    monkeypatch.setattr(mod, "_V53_DIR", root)
    adapter = mod.V53Adapter()
    with pytest.raises(Exception, match="etf_divid"):
        adapter._load_resources()
    path = root / "data" / "etf_divid.parquet"
    _divid_df([("511260.SH", "2024-01-05", 1.0)]).to_parquet(path, index=False)
    adapter._load_resources()
    assert mod.V53Adapter._etf_divid.iloc[0]["cash"] == 1
    _divid_df([("511260.SH", "2024-01-05", 2.0)]).to_parquet(path, index=False)
    adapter._load_resources()
    assert mod.V53Adapter._etf_divid.iloc[0]["cash"] == 2
    _reset_adapter_cache()


def test_missing_input_is_explicit_and_does_not_stop_other_strategy(tmp_path):
    from app.strategy.base import Strategy, StrategyInputNotReady
    from app.strategy.runner import StrategyRunner
    from app.storage.parquet import ParquetStore

    class Waiting(Strategy):
        def run(self, ctx, trade_date):
            raise StrategyInputNotReady("etf_divid missing")

    class Healthy(Strategy):
        def run(self, ctx, trade_date):
            return []

    runner = StrategyRunner({"wait": Waiting, "good": Healthy}, ParquetStore(tmp_path))
    rows = [
        dict(instance_id=i, strategy_id=s, virtual_cash=100, virtual_positions={})
        for i, s in [("a", "wait"), ("b", "good")]
    ]
    signals, states = runner.run_all(20260904, rows)
    assert signals == {"a": [], "b": []}
    assert states["a"]["input_readiness"]["status"] == "WAITING_INPUT"
    assert runner.input_statuses["a"]["requested_trade_date"] == "20260904"
    assert states["b"] is None

    # A non-rebalance date succeeding does not erase yesterday's missing input.
    rows[0]["strategy_state"] = states["a"]
    rows[0]["strategy_id"] = "good"
    _, states = runner.run_all(20260907, rows)
    assert states["a"]["input_readiness"]["requested_trade_date"] == "20260904"
    assert states["a"]["input_readiness"]["status"] == "WAITING_INPUT"
    rows[0]["strategy_state"] = states["a"]
    _, states = runner.run_all(20260904, rows)
    assert states["a"]["input_readiness"]["status"] == "READY"
