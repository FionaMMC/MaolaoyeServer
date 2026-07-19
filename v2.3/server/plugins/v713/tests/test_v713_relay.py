from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from plugins.v713_relay import V713RelayAdapter, basket_hash


class FakeCtx:
    def __init__(self, data, state=None, guard=None):
        self.data, self._state = data, state or {}
        self._guard = guard or {"allowed": True, "blockers": []}
        self._next_state = None
        self.instance_id = "paper_v713_v713_relay"
    def cash(self): return 1_000_000.0
    def positions(self): return {}
    def strategy_state(self): return dict(self._state)
    def set_strategy_state(self, value): self._next_state = value
    def risk_blacklist(self): return set()
    def market(self, symbol, **kwargs): return self.data.get(symbol, pd.DataFrame())
    def execution_guard(self): return dict(self._guard)


def prices():
    return pd.DataFrame({"trade_date": [20240701], "close": [100.0], "volume": [1_000_000]})


def write_target(folder: Path, weight=1.0):
    frame = pd.DataFrame([{
        "code": "511260.SH", "weight": weight, "strategy_version": "v7.13-base",
        "sleeve": "AUX_HYDRA", "decision_date": "20240628", "as_of_date": "20240531",
    }])
    frame["basket_sha256"] = basket_hash(frame)
    folder.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(folder / "v713_target_latest.parquet", index=False)


def test_verified_basket_emits_and_uses_content_hash(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(data_dir)
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {"cash_buffer": 0.01, "dry_run": False, "risk_filters": {}}
    adapter = V713RelayAdapter()
    ctx = FakeCtx({"511260.SH": prices()})
    signals = adapter.run(ctx, 20240701)
    assert len(signals) == 1 and signals[0].direction == "BUY"
    assert ctx._next_state["last_consumed_basket_sha256"]
    V713RelayAdapter._cfg = None


def test_rejects_tampered_basket(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(data_dir)
    path = data_dir / "v713_target_latest.parquet"
    frame = pd.read_parquet(path)
    frame.loc[0, "weight"] = 0.9
    frame.to_parquet(path, index=False)
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    with pytest.raises(ValueError, match="weights must sum"):
        V713RelayAdapter()._read_latest_basket()


def test_dry_run_replay_is_idempotent(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(data_dir)
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {
        "cash_buffer": 0.01, "dry_run": True,
        "max_target_age_days": 30, "risk_filters": {},
    }
    first = FakeCtx({"511260.SH": prices()})
    assert V713RelayAdapter().run(first, 20240701) == []
    assert first._next_state["last_replayed_basket_sha256"]
    second = FakeCtx({"511260.SH": prices()}, state=first._next_state)
    assert V713RelayAdapter().run(second, 20240701) == []
    assert second._next_state is None
    V713RelayAdapter._cfg = None


def test_live_run_fails_closed_when_server_guard_blocks(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(data_dir)
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {
        "cash_buffer": 0.01, "dry_run": False,
        "max_target_age_days": 30, "risk_filters": {},
    }
    ctx = FakeCtx(
        {"511260.SH": prices()},
        guard={"allowed": False, "blockers": ["unresolved_order"]},
    )
    assert V713RelayAdapter().run(ctx, 20240701) == []
    assert ctx._next_state["last_execution_blockers"] == ["unresolved_order"]
    V713RelayAdapter._cfg = None


def test_rejects_forbidden_sleeve_even_with_valid_hash(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(data_dir)
    path = data_dir / "v713_target_latest.parquet"
    frame = pd.read_parquet(path)
    frame["sleeve"] = "NARROW_TOP2"
    frame["basket_sha256"] = basket_hash(frame)
    frame.to_parquet(path, index=False)
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    with pytest.raises(ValueError, match="forbidden sleeve"):
        V713RelayAdapter()._read_latest_basket()
