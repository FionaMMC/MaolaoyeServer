from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from plugins.v713_relay import V713RelayAdapter, allocation_hash, basket_hash


class FakeCtx:
    def __init__(self, data, state=None, guard=None, positions=None, cash=1_000_000.0):
        self.data, self._state = data, state or {}
        self._guard = guard or {"allowed": True, "blockers": []}
        self._positions = positions or {}
        self._cash = cash
        self._next_state = None
        self.instance_id = "paper_v79_v713_relay"
    def cash(self): return self._cash
    def positions(self): return dict(self._positions)
    def strategy_state(self): return dict(self._state)
    def set_strategy_state(self, value): self._next_state = value
    def risk_blacklist(self): return set()
    def market(self, symbol, **kwargs): return self.data.get(symbol, pd.DataFrame())
    def execution_guard(self): return dict(self._guard)


def prices():
    return pd.DataFrame({"trade_date": [20240701], "close": [100.0], "volume": [1_000_000]})


def write_target(
    folder: Path,
    weight=1.0,
    *,
    decision_date="20240628",
    as_of_date="20240531",
):
    frame = pd.DataFrame([{
        "code": "511260.SH", "weight": weight, "strategy_version": "v7.13-base",
        "sleeve": "AUX_HYDRA", "decision_date": decision_date,
        "as_of_date": as_of_date,
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


def test_shared_ledger_records_attributed_reconciliation(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(data_dir)
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {"cash_buffer": 0.01, "dry_run": False, "risk_filters": {}}
    ctx = FakeCtx(
        {"511260.SH": prices()},
        guard={
            "allowed": True,
            "blockers": [],
            "account_isolation": "shared_ledger",
            "reconciliation_scope": "attributed_ledger",
        },
    )
    assert len(V713RelayAdapter().run(ctx, 20240701)) == 1
    assert ctx._next_state["reconciliation_status"] == "attributed_ledger"
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


def test_decision_date_does_not_change_allocation_identity(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    write_target(first_dir, decision_date="20240807", as_of_date="20240731")
    write_target(second_dir, decision_date="20240810", as_of_date="20240731")
    first = pd.read_parquet(first_dir / "v713_target_latest.parquet")
    second = pd.read_parquet(second_dir / "v713_target_latest.parquet")

    assert basket_hash(first) != basket_hash(second)
    assert allocation_hash(first) == allocation_hash(second)


def test_new_artifact_for_consumed_month_is_not_rebalanced(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(
        data_dir, decision_date="20240810", as_of_date="20240731",
    )
    basket = pd.read_parquet(data_dir / "v713_target_latest.parquet")
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {
        "cash_buffer": 0.01, "dry_run": False,
        "max_target_age_days": 30, "risk_filters": {},
    }
    previous_target = {"511260.SH": 9800}
    ctx = FakeCtx(
        {"511260.SH": prices()},
        state={
            "last_consumed_basket_sha256": "previous-monthly-artifact",
            "last_target_as_of_date": "20240731",
            "last_target_quantities": previous_target,
        },
        positions={"511260.SH": 9800},
    )

    assert V713RelayAdapter().run(ctx, 20240812) == []
    assert ctx._next_state["last_target_quantities"] == previous_target
    assert ctx._next_state["last_ignored_basket_sha256"] == str(
        basket["basket_sha256"].iloc[0]
    )
    assert ctx._next_state["last_ignored_reason"] == "monthly_cycle_already_consumed"
    V713RelayAdapter._cfg = None


def test_stale_new_artifact_for_consumed_month_is_ignored(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(
        data_dir, decision_date="20240801", as_of_date="20240731",
    )
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {
        "cash_buffer": 0.01, "dry_run": False,
        "max_target_age_days": 7, "risk_filters": {},
    }
    ctx = FakeCtx(
        {"511260.SH": prices()},
        state={
            "last_consumed_basket_sha256": "previous-monthly-artifact",
            "last_target_as_of_date": "20240731",
            "last_target_quantities": {"511260.SH": 9800},
        },
        positions={"511260.SH": 9800},
    )

    assert V713RelayAdapter().run(ctx, 20240820) == []
    assert ctx._next_state["last_ignored_reason"] == "monthly_cycle_already_consumed"
    V713RelayAdapter._cfg = None


def test_new_completed_month_allows_rebalance(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(
        data_dir, decision_date="20240803", as_of_date="20240731",
    )
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {
        "cash_buffer": 0.01, "dry_run": False,
        "max_target_age_days": 30, "risk_filters": {},
    }
    ctx = FakeCtx(
        {"511260.SH": prices()},
        state={
            "last_consumed_basket_sha256": "prior-month-artifact",
            "last_target_as_of_date": "20240630",
            "last_target_quantities": {"511260.SH": 9800},
        },
    )

    signals = V713RelayAdapter().run(ctx, 20240805)

    assert len(signals) == 1
    assert ctx._next_state["last_target_as_of_date"] == "20240731"
    assert ctx._next_state["last_target_allocation_sha256"]
    V713RelayAdapter._cfg = None


def test_consumed_basket_retries_only_unfilled_residual(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(data_dir)
    basket = pd.read_parquet(data_dir / "v713_target_latest.parquet")
    basket_id = str(basket["basket_sha256"].iloc[0])
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {
        "cash_buffer": 0.01, "dry_run": False,
        "max_target_age_days": 30, "risk_filters": {},
    }
    ctx = FakeCtx(
        {"511260.SH": prices()},
        state={
            "last_consumed_basket_sha256": basket_id,
            "last_target_quantities": {"511260.SH": 9900},
        },
        positions={"511260.SH": 9800},
    )

    signals = V713RelayAdapter().run(ctx, 20240702)

    assert len(signals) == 1
    assert signals[0].symbol == "511260.SH"
    assert signals[0].direction == "BUY"
    assert signals[0].quantity == 100
    assert ctx._next_state["last_residual_retry_trade_date"] == "20240702"
    assert ctx._next_state["last_target_quantities"] == {"511260.SH": 9900}
    V713RelayAdapter._cfg = None


def test_consumed_basket_still_skips_after_target_is_reached(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(data_dir)
    basket = pd.read_parquet(data_dir / "v713_target_latest.parquet")
    basket_id = str(basket["basket_sha256"].iloc[0])
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {
        "cash_buffer": 0.01, "dry_run": False,
        "max_target_age_days": 30, "risk_filters": {},
    }
    ctx = FakeCtx(
        {"511260.SH": prices()},
        state={
            "last_consumed_basket_sha256": basket_id,
            "last_target_quantities": {"511260.SH": 9900},
        },
        positions={"511260.SH": 9900},
    )

    assert V713RelayAdapter().run(ctx, 20240702) == []
    assert ctx._next_state is None
    V713RelayAdapter._cfg = None


def test_stale_consumed_basket_does_not_retry_residual(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_target(data_dir)
    basket = pd.read_parquet(data_dir / "v713_target_latest.parquet")
    basket_id = str(basket["basket_sha256"].iloc[0])
    monkeypatch.setattr(V713RelayAdapter, "data_dir", data_dir)
    V713RelayAdapter._cfg = {
        "cash_buffer": 0.01, "dry_run": False,
        "max_target_age_days": 7, "risk_filters": {},
    }
    ctx = FakeCtx(
        {"511260.SH": prices()},
        state={
            "last_consumed_basket_sha256": basket_id,
            "last_target_quantities": {"511260.SH": 9900},
        },
        positions={"511260.SH": 9800},
    )

    assert V713RelayAdapter().run(ctx, 20240720) == []
    assert ctx._next_state["last_residual_retry_blocked_reason"] == (
        "stale_consumed_basket"
    )
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
