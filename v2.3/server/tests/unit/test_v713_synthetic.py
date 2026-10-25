"""Synthetic V713 ledger over the live stock leg and live Hydra (read-only)."""
import importlib.util
import json
from pathlib import Path
import random

import pytest

from app.services.v713_synthetic import (MonthState, SyntheticPolicy, load_synthetic_policy, rebalance,
                                         run_synthetic, unit_nav)

POLICY_FILE = Path(__file__).resolve().parents[2] / "plugins" / "v713" / "synthetic_policy_v1.json"
POLICY = SyntheticPolicy(policy_id="V713_SYNTHETIC_TEST", stock_instance="live_v713_stockleg",
                         hydra_instance="live_hydra_v481_rb", label="test")


def test_unit_nav_chains_flow_adjusted_returns():
    units = unit_nav([("20261202", .1), ("20261201", None), ("20261203", -.5)])
    assert units == pytest.approx({"20261201": 1.0, "20261202": 1.1, "20261203": .55})
    with pytest.raises(ValueError, match="20261202"):
        unit_nav([("20261201", None), ("20261202", None)])


def test_rebalance_matches_the_design_example():
    # TOP50 -> T1 at 400k: virtual stock sale and virtual Hydra purchase, 20bp each.
    values, cash, cost, traded = rebalance([400_000.0, 0.0], 0.0, .5, (.002, .002))
    assert values == pytest.approx([200_000.0, 199_201.596806])
    assert cash == pytest.approx(0.0, abs=1e-9)
    assert cost == pytest.approx(798.403194)
    assert traded == pytest.approx(399_201.596806)


def test_real_leg_trades_are_not_charged_again():
    # AUX exit: the Hydra sale is virtual, the stock rebuild is real and already in its NAV.
    values, _, cost, _ = rebalance([0.0, 400_000.0], 0.0, 1.0, (0.0, .002))
    assert cost == pytest.approx(800.0)
    assert values[0] == pytest.approx(399_200.0)


def test_frozen_policy_file_loads_and_unversioned_policy_is_rejected(tmp_path):
    policy = load_synthetic_policy(POLICY_FILE)
    assert (policy.base_value, policy.cost_bps, policy.primary_bps) == (400_000.0, (0.0, 20.0, 50.0), 20.0)
    bad = json.loads(POLICY_FILE.read_text(encoding="utf-8")) | {"policy_id": "synthetic"}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="V713_SYNTHETIC_"):
        load_synthetic_policy(path)


D = [f"202612{x:02d}" for x in range(1, 21)]
US = {d: 1.0 + .001 * i for i, d in enumerate(D)}
UH = {d: 1.0 for d in D}
T1 = MonthState("20261130", "T1_5050", D[0])
AUX_MONTH = MonthState("20261231", "AUX_HYDRA", D[2])
TOP = MonthState("20270129", "TOP50", D[10])


def _run(months, exposure=None, dates=D, **changes):
    return run_synthetic(POLICY, **({"dates": dates, "unit_stock": US, "unit_hydra": UH,
                                     "exposure": exposure or {d: 0.0 for d in D}, "months": months,
                                     "bps": 20.0} | changes))


def test_init_splits_the_base_by_state_without_cost():
    out = _run([T1])
    assert out["status"] == "CURRENT"
    assert out["events"][0]["kind"] == "INIT" and out["events"][0]["cost"] == 0.0
    assert out["rows"][0]["value"] == pytest.approx(400_000.0)
    assert out["rows"][0]["stock_weight"] == pytest.approx(.5)


def test_virtual_switch_charges_both_sides():
    out = _run([MonthState("20261130", "TOP50", D[0]), MonthState("20261231", "T1_5050", D[5])])
    move = out["events"][1]
    assert (move["kind"], move["date"]) == ("VIRTUAL", D[5])
    sold = 400_000.0 * US[D[5]] / 2
    assert move["cost"] == pytest.approx(sold * .002 + sold * (1 - .002) / (1 + .002) * .002)


def test_aux_entry_waits_for_real_liquidation_then_exit_converts_back():
    exposure = {d: (.99 if d < "20261205" else 0.0) for d in D}
    out = _run([T1, AUX_MONTH, TOP], exposure=exposure)
    kinds = [(e["date"], e["kind"]) for e in out["events"]]
    assert kinds == [(D[0], "INIT"), ("20261205", "ENTER_AUX"), (D[10], "EXIT_AUX")]
    assert [r["date"] for r in out["rows"] if r["pending"]] == ["20261203", "20261204"]
    stock_value = 200_000.0 * US["20261205"]
    assert out["events"][1]["cost"] == pytest.approx(stock_value * .002 / 1.002)
    hydra_value = 200_000.0 + stock_value / 1.002
    assert out["events"][2]["cost"] == pytest.approx(hydra_value * .002)
    assert out["rows"][-1]["stock_weight"] == pytest.approx(1.0)


def test_unfinished_liquidation_is_flagged_and_stops_at_the_next_month():
    stuck = {d: .99 for d in D}
    flagged = _run([T1, AUX_MONTH], exposure=stuck)
    assert flagged["status"] == "REVIEW_REQUIRED"
    assert (flagged["events"][-1]["date"], flagged["events"][-1]["kind"]) == (D[12], "REVIEW_REQUIRED")
    assert len(flagged["rows"]) == len(D)
    stopped = _run([T1, AUX_MONTH, TOP], exposure=stuck)
    assert stopped["status"] == "REVIEW_REQUIRED"
    assert stopped["events"][-1]["kind"] == "STOPPED" and stopped["rows"][-1]["date"] == D[9]


def test_missing_leg_nav_on_an_anchor_is_an_error():
    with pytest.raises(ValueError, match="20261203"):
        _run([T1, AUX_MONTH], dates=[d for d in D if d != D[2]])
    assert _run([])["status"] == "WAITING_START"


def test_an_unfinished_sell_order_blocks_the_aux_switch():
    # Residual stock is already <= 1% on 12/03, but a sell order is still open that close.
    out = _run([T1, AUX_MONTH], open_sell_dates={"20261203", "20261204"})
    assert [(e["date"], e["kind"]) for e in out["events"]] == [(D[0], "INIT"), ("20261205", "ENTER_AUX")]


RESEARCH = Path(__file__).resolve().parents[4] / "tools" / "v713_combination_validation.py"


def _research():
    spec = importlib.util.spec_from_file_location("v713_combination_research", RESEARCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("bps", [0.0, 20.0, 50.0])
def test_all_virtual_replay_equals_the_oct16_engine(bps):
    rng = random.Random(7)
    bounds = [f"{2020 + k // 12}-{k % 12 + 1:02d}-28" for k in range(61)]
    rows = [{"start": a, "end": b, "known_at": a, "sleeve": rng.choices(["TOP50", "T1_5050", "AUX_HYDRA"], (5, 4, 1))[0],
             "stock_return": rng.uniform(-.1, .1), "hydra_return": rng.uniform(-.03, .03)}
            for a, b in zip(bounds, bounds[1:])]
    reference = _research().simulate_periods(rows, initial_capital=400_000, rebalance="monthly",
                                             switch_bps=bps)["summary"]["final_nav"]
    out = run_synthetic(POLICY, dates=bounds, exposure={}, bps=bps, all_virtual=True,
                        unit_stock=unit_nav([(bounds[0], None)] + [(r["end"], r["stock_return"]) for r in rows]),
                        unit_hydra=unit_nav([(bounds[0], None)] + [(r["end"], r["hydra_return"]) for r in rows]),
                        months=[MonthState(r["start"], r["sleeve"], r["start"]) for r in rows])
    assert out["rows"][-1]["value"] == pytest.approx(reference, rel=1e-12)


from app.db import init_db, make_engine, make_session_factory
from app.models import PerfSnapshot, PerfValuation
from app.oms.models import OmsCycle, OmsOrder, V713StateRecord
from app.services.v713_synthetic import load_inputs
from scripts import run_v713_synthetic


def _database(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/synthetic.db")
    init_db(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        for i, day in enumerate(D[:5]):
            s.add(PerfSnapshot(instance_id="live_v713_stockleg", date=day, execution_domain="live",
                               nav=200_000.0 * 1.01 ** i, daily_return=None if i == 0 else .01,
                               positions_snapshot={}))
            s.add(PerfSnapshot(instance_id="live_hydra_v481_rb", date=day, execution_domain="live",
                               nav=300_000.0, daily_return=None if i == 0 else 0.0, positions_snapshot={}))
            s.add(PerfValuation(instance_id="live_v713_stockleg", date=day, execution_domain="live",
                                nav=200_000.0, cash=2_000.0, dividend_receivable=0.0))
        s.add(V713StateRecord(instance_id="live_v713_stockleg", signal_date="20261130", sleeve="T1_5050",
                              decision_date=D[1], basket_sha256="b" * 64, allocation_sha256="a" * 64,
                              target_version_id="tv_x", recorded_at="2026-11-30T20:00:00+08:00"))
        s.add(OmsCycle(cycle_id="C00001", cycle_no=1, target_version_id="tv_x", account_alias="shared",
                       instance_id="live_v713_stockleg", policy={}, nav_at_signal=200_000.0, frozen_target={},
                       sell_anchor={}, lot_gap=0.0, status="ACTIVE", created_at="2026-11-30T20:00:00+08:00",
                       schedule=[{"seq": 1, "trade_date": D[0], "phase": "SELL", "sell_attempt": 0}]))
        s.commit()
    return sf


def _sell(client_order_id, trade_date, state, updated_at):
    return OmsOrder(client_order_id=client_order_id, session_id="C00001:1", cycle_id="C00001", account_alias="shared",
                    trade_date=trade_date, symbol="600001.SH", side="SELL", quantity=100, limit_price=9.95,
                    reference_price=10.0, state=state, created_at=updated_at, updated_at=updated_at)


def test_job_reads_leg_navs_and_states_and_writes_every_cost_line(tmp_path):
    sf = _database(tmp_path)
    output = tmp_path / "v713_synthetic.json"
    assert run_v713_synthetic.main(["--policy", str(POLICY_FILE), "--output", str(output)], session_factory=sf,
                                   now="2026-12-05T17:00:00+08:00") == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["policy_id"] == "V713_SYNTHETIC_V1" and document["status"] == "CURRENT"
    assert set(document["lines"]) == {"0", "20", "50"}
    rows = document["lines"]["20"]["rows"]
    assert rows[0]["date"] == D[0]  # the cycle's first session, not the decision date
    assert rows[-1]["value"] == pytest.approx(400_000.0 * (.5 * 1.01 ** 4 + .5))
    assert not list(tmp_path.glob("*.tmp"))


def test_a_sell_is_open_until_the_close_of_the_day_it_became_terminal(tmp_path):
    sf = _database(tmp_path)
    with sf() as s:
        s.add(_sell("S1", D[0], "EXPIRED_DAY", "2026-12-03T15:30:00+08:00"))
        s.add(_sell("S2", D[3], "UNKNOWN", "2026-12-04T14:57:05+08:00"))
        s.commit()
    inputs = load_inputs(sf, load_synthetic_policy(POLICY_FILE))
    assert inputs["open_sell_dates"] == {D[0], D[1], D[3], D[4]}
    assert inputs["exposure"][D[0]] == pytest.approx(.99)
