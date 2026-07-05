"""V79RelayAdapter 单元测试 — thin relay: basket-driven diff, 无日历逻辑。

镜像 tests/unit/test_v53_adapter.py 的 Context/FakeCtx 构造模式。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


# ── helpers ───────────────────────────────────────────────────────────────
def _reset_adapter_cache():
    from plugins.v79_relay import V79RelayAdapter
    V79RelayAdapter._cfg = None


def _write_basket(data_dir: Path, rows: list[dict]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(
        data_dir / "v79_target_latest.parquet", index=False)


class _FakeCtx:
    """镜像 v53 测试里的 _FakeBlacklistCtx，补上 strategy_state / market。"""

    def __init__(
        self,
        cash: float = 10_000_000.0,
        positions: dict | None = None,
        market_data: dict | None = None,
        state: dict | None = None,
        blacklist: set | None = None,
        instance_id: str = "paper_v79_v79_relay",
    ):
        self._cash = cash
        self._positions = positions or {}
        self._market = market_data or {}
        self._state = state or {}
        self._next_state = None
        self._bl = blacklist or set()
        self.instance_id = instance_id

    def cash(self) -> float:
        return self._cash

    def positions(self) -> dict:
        return dict(self._positions)

    def market(self, symbol, *, category=None, **kwargs):
        return self._market.get(symbol, pd.DataFrame())

    def strategy_state(self) -> dict:
        return dict(self._state)

    def set_strategy_state(self, state: dict) -> None:
        self._next_state = dict(state) if state is not None else None

    def risk_blacklist(self) -> set:
        return set(self._bl)


def _price_df(price: float, trade_date: int = 20240506) -> pd.DataFrame:
    return pd.DataFrame({
        "trade_date": [trade_date],
        "open": [price], "high": [price], "low": [price], "close": [price],
        "volume": [1_000_000],
    })


# ── tests ─────────────────────────────────────────────────────────────────
def test_new_basket_emits_diff(tmp_path, monkeypatch):
    """空仓 + basket {A:0.5, B:0.5}, nav 来自 cash → BUY A/B, SELL offset0/BUY offset+0.005"""
    _reset_adapter_cache()
    import plugins.v79_relay as adapter_mod
    from plugins.v79_relay import V79RelayAdapter

    data_dir = tmp_path / "v79data"
    _write_basket(data_dir, [
        {"code": "600519.SH", "weight": 0.5, "sleeve": "equity", "decision_date": "20240503"},
        {"code": "511260.SH", "weight": 0.5, "sleeve": "defensive", "decision_date": "20240503"},
    ])
    monkeypatch.setattr(V79RelayAdapter, "data_dir", data_dir)

    ctx = _FakeCtx(
        cash=1_000_000.0,
        positions={},
        market_data={
            "600519.SH": _price_df(100.0),
            "511260.SH": _price_df(100.0),
        },
    )
    adapter = V79RelayAdapter()
    adapter._cfg = {"cash_buffer": 0.01, "dry_run": False, "risk_filters": {}}
    signals = adapter.run(ctx, 20240506)

    assert len(signals) == 2
    assert all(s.direction == "BUY" for s in signals)
    by_code = {s.symbol: s for s in signals}
    # nav = 1,000,000 (no positions); investable = 990,000; 0.5*990000/100/100 lots
    # = 4950/100 = 49.5 -> round to 50 lots? let's just check positive multiples of 100
    for code in ("600519.SH", "511260.SH"):
        assert by_code[code].quantity % 100 == 0
        assert by_code[code].quantity > 0
        assert by_code[code].price_offset == pytest.approx(0.005)
    _reset_adapter_cache()


def test_idempotent_same_decision_date(tmp_path, monkeypatch):
    """state.last_consumed_decision_date == basket decision_date → run() 返回 []"""
    _reset_adapter_cache()
    from plugins.v79_relay import V79RelayAdapter

    data_dir = tmp_path / "v79data"
    _write_basket(data_dir, [
        {"code": "600519.SH", "weight": 1.0, "sleeve": "equity", "decision_date": "20240503"},
    ])
    monkeypatch.setattr(V79RelayAdapter, "data_dir", data_dir)

    ctx = _FakeCtx(
        cash=1_000_000.0,
        positions={},
        market_data={"600519.SH": _price_df(100.0)},
        state={"last_consumed_decision_date": "20240503"},
    )
    adapter = V79RelayAdapter()
    adapter._cfg = {"cash_buffer": 0.01, "dry_run": False, "risk_filters": {}}
    signals = adapter.run(ctx, 20240506)

    assert signals == []
    _reset_adapter_cache()


def test_dry_run_emits_nothing_but_logs(tmp_path, monkeypatch, caplog):
    """dry_run=true → 即便是全新 basket 也 return []; state 不被标记 consumed"""
    _reset_adapter_cache()
    import logging
    from plugins.v79_relay import V79RelayAdapter

    data_dir = tmp_path / "v79data"
    _write_basket(data_dir, [
        {"code": "600519.SH", "weight": 1.0, "sleeve": "equity", "decision_date": "20240503"},
    ])
    monkeypatch.setattr(V79RelayAdapter, "data_dir", data_dir)

    ctx = _FakeCtx(
        cash=1_000_000.0,
        positions={},
        market_data={"600519.SH": _price_df(100.0)},
        state={},
    )
    adapter = V79RelayAdapter()
    adapter._cfg = {"cash_buffer": 0.01, "dry_run": True, "risk_filters": {}}

    caplog.set_level(logging.INFO, logger="plugins.v79_relay")
    signals = adapter.run(ctx, 20240506)

    assert signals == []
    assert ctx._next_state is None, "dry_run 不应标记 consumed"
    log_msgs = [r.getMessage() for r in caplog.records]
    assert any("DRY-RUN" in m for m in log_msgs), f"no DRY-RUN log; logs={log_msgs}"
    _reset_adapter_cache()


def test_diff_sells_before_buys(tmp_path, monkeypatch):
    """当前持仓有 basket 之外的旧 code → 应被 SELL，且排在 BUY 之前"""
    _reset_adapter_cache()
    from plugins.v79_relay import V79RelayAdapter

    data_dir = tmp_path / "v79data"
    _write_basket(data_dir, [
        {"code": "600519.SH", "weight": 1.0, "sleeve": "equity", "decision_date": "20240503"},
    ])
    monkeypatch.setattr(V79RelayAdapter, "data_dir", data_dir)

    ctx = _FakeCtx(
        cash=100_000.0,
        positions={"000001.SZ": 2000},  # old code not in basket
        market_data={
            "600519.SH": _price_df(100.0),
            "000001.SZ": _price_df(10.0),
        },
    )
    adapter = V79RelayAdapter()
    adapter._cfg = {"cash_buffer": 0.01, "dry_run": False, "risk_filters": {}}
    signals = adapter.run(ctx, 20240506)

    assert len(signals) >= 1
    sell_idx = [i for i, s in enumerate(signals) if s.direction == "SELL"]
    buy_idx = [i for i, s in enumerate(signals) if s.direction == "BUY"]
    assert sell_idx, "expected at least one SELL"
    assert any(s.symbol == "000001.SZ" and s.direction == "SELL" for s in signals)
    if buy_idx:
        assert max(sell_idx) < min(buy_idx)
    sell_signal = next(s for s in signals if s.symbol == "000001.SZ")
    assert sell_signal.price_offset == pytest.approx(0.0)
    assert sell_signal.quantity == 2000
    _reset_adapter_cache()


def test_missing_basket_returns_empty(tmp_path, monkeypatch):
    """无 parquet 文件 → run() 返回 []"""
    _reset_adapter_cache()
    from plugins.v79_relay import V79RelayAdapter

    data_dir = tmp_path / "v79data_empty"  # never created
    monkeypatch.setattr(V79RelayAdapter, "data_dir", data_dir)

    ctx = _FakeCtx(cash=1_000_000.0, positions={})
    adapter = V79RelayAdapter()
    adapter._cfg = {"cash_buffer": 0.01, "dry_run": False, "risk_filters": {}}
    signals = adapter.run(ctx, 20240506)

    assert signals == []
    _reset_adapter_cache()


def test_adapter_class_attrs():
    from plugins.v79_relay import V79RelayAdapter
    assert V79RelayAdapter.name == "v79_relay"
    assert V79RelayAdapter.data_files == ["v79_target_latest.parquet"]
