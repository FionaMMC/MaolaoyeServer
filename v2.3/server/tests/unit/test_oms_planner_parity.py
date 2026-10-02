"""The research replay must use the production planner, not a copy of it."""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "reports" / "hydra_policy_20261002"))
policy_replay = pytest.importorskip("policy_replay")

from app.oms import planner  # noqa: E402


def _synthetic():
    dates = pd.bdate_range("2026-03-02", periods=40)
    syms = ["510300.SH", "511260.SH", "518880.SH"]
    base = {"510300.SH": 4.0, "511260.SH": 130.0, "518880.SH": 9.0}
    rows = []
    for i, d in enumerate(dates):
        for j, s in enumerate(syms):
            p = base[s] * (1 + 0.002 * ((i * (j + 1)) % 7 - 3))
            rows.append(dict(date=d, symbol=s, open=p * 1.001, high=p * 1.01, low=p * 0.99,
                             close=p, volume=1e7, suspendFlag=0))
    raw = pd.DataFrame(rows)
    daily = {(r.date, r.symbol): r._asdict() for r in raw.itertuples(index=False)}
    close = raw.pivot(index="date", columns="symbol", values="close")
    weights = pd.DataFrame([[0.3, 0.6, 0.1], [0.2, 0.7, 0.1]], columns=syms, index=[dates[2], dates[22]])
    return weights, daily, close


def test_replay_uses_planner_functions():
    assert policy_replay.lot_target is planner.lot_target
    assert policy_replay.buy_limit is planner.buy_limit
    assert policy_replay.sell_limit is planner.sell_limit
    assert policy_replay.allocate_buys is planner.allocate_buys


def test_c3_fills_happen_only_in_planned_sessions():
    weights, daily, close = _synthetic()
    c3 = policy_replay.Policy("C3", lot="nearest", size_factor=1.001, buy_bps=planner.Policy().buy_bps,
                              touch=True, sell_schedule=(50., 200.))
    _, _, _, fills, _ = policy_replay.run(weights, daily, close, [], c3, capital=200000.)
    assert not fills.empty
    cal = [d.strftime("%Y%m%d") for d in close.index]
    planned = set()
    for signal in weights.index:
        for sess in planner.session_schedule(cal, signal.strftime("%Y%m%d"), 3):
            planned.add((sess.trade_date, "close" if sess.phase == "SELL" else "open"))
    observed = {(pd.Timestamp(f.date).strftime("%Y%m%d"), f.phase) for f in fills.itertuples()}
    assert observed <= planned
