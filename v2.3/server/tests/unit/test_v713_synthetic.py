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



def test_v2_policy_makes_13bp_the_primary_line_with_stress_and_old_lines():
    policy = load_synthetic_policy(POLICY_FILE.with_name("synthetic_policy_v2.json"))
    assert policy.policy_id == "V713_SYNTHETIC_V2"
    assert (policy.primary_bps, policy.cost_bps) == (13.0, (0.0, 13.0, 18.0, 20.0, 23.0))

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


def test_liquidation_after_review_deadline_does_not_switch_without_review():
    exposure = {d: .99 if i < 14 else 0.0 for i, d in enumerate(D)}
    out = _run([T1, AUX_MONTH], exposure=exposure)
    assert out["status"] == "REVIEW_REQUIRED"
    assert [event["kind"] for event in out["events"]] == ["INIT", "REVIEW_REQUIRED"]
    assert out["rows"][-1]["sleeve"] == "T1_5050"
    assert out["rows"][-1]["pending"] == "ENTER_AUX"
    assert out["rows"][-1]["stock_units"] == out["rows"][0]["stock_units"]


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
from app.oms.models import OmsBrokerSnapshot, OmsCycle, OmsOrder, OmsOverride, OmsReconciliation, V713StateRecord
from app.services.v713_synthetic import SyntheticEvidenceUnavailable, load_inputs
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


def _broker_snapshot(snapshot_id, day, orders, *, account_alias="shared"):
    return OmsBrokerSnapshot(snapshot_id=snapshot_id, account_alias=account_alias, kind="EOD",
                             trade_date=day, taken_at=f"{day[:4]}-{day[4:6]}-{day[6:]}T15:30:00+08:00",
                             received_at=f"{day[:4]}-{day[4:6]}-{day[6:]}T15:30:01+08:00",
                             payload={"orders": orders})


def _broker_order(order_id, status=56):
    return {"broker_order_id": "broker-" + order_id, "symbol": "600001.SH", "side": "SELL",
            "quantity": 100, "price": 9.95, "status": status,
            "traded_volume": 100 if status == 56 else 0, "traded_price": 9.95, "remark": order_id}


def _supersession(order_id, trade_date, basis, *, created_at="2026-12-04T18:00:00+08:00"):
    return OmsOverride(account_alias="shared", action="CYCLE_SUPERSEDED", operator="cycle_service",
                       reason="new monthly target", created_at=created_at,
                       payload={"cycle_id": "C00001", "instance_id": "live_v713_stockleg",
                                "signal_date": "20261231", "prior_cycle_status": "PENDING_APPROVAL",
                                "not_submitted_orders": [{"client_order_id": order_id, "trade_date": trade_date,
                                                          "safety_basis": basis}]},
                       effect={"status": "CLOSED", "close_reason": "SUPERSEDED"})


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
        s.add(_sell("S1", D[2], "EXPIRED_DAY", "2026-12-03T15:30:00+08:00"))
        s.add(_sell("S2", D[3], "UNKNOWN", "2026-12-04T14:57:05+08:00"))
        s.add(_broker_snapshot("snap-expiry", D[2], [_broker_order("S1", status=50)]))
        s.add(OmsReconciliation(snapshot_id="snap-expiry", cycle_id="C00001", passed=True,
                                discrepancies=[], created_at="2026-12-03T15:30:01+08:00"))
        s.commit()
    inputs = load_inputs(sf, load_synthetic_policy(POLICY_FILE))
    assert inputs["open_sell_dates"] == {D[3], D[4]}
    assert inputs["exposure"][D[0]] == pytest.approx(.99)


def test_reobserving_terminal_sell_cannot_rewrite_historical_aux_entry(tmp_path):
    from app.oms.ledger import OrderLedger
    from app.oms.schemas import BrokerOrder

    sf = _database(tmp_path)
    with sf() as s:
        order = _sell("S1", D[0], "FILLED", "2026-12-03T15:30:00+08:00")
        order.filled_qty = 100
        s.add(order)
        s.add(_broker_snapshot("snap-original", D[2], [_broker_order("S1")]))
        s.commit()
    policy = load_synthetic_policy(POLICY_FILE)
    before = load_inputs(sf, policy)
    with sf() as s:
        OrderLedger._merge_observation(s.get(OmsOrder, "S1"), BrokerOrder(**_broker_order("S1")),
                                       "2026-12-05T15:30:00+08:00", [])
        s.add(_broker_snapshot("snap-repeat", D[4], [_broker_order("S1")]))
        s.commit()
    after = load_inputs(sf, policy)
    assert before["open_sell_dates"] == after["open_sell_dates"] == {D[0], D[1]}
    for inputs in (before, after):
        inputs.update(months=[T1, AUX_MONTH], exposure={d: 0.0 for d in D})
    assert run_synthetic(policy, bps=20, **before) == run_synthetic(policy, bps=20, **after)


@pytest.mark.parametrize("bad_evidence", ["absent", "other_account", "other_symbol", "expiry_without_receipt"])
def test_terminal_sell_requires_matching_immutable_evidence(tmp_path, bad_evidence):
    sf = _database(tmp_path)
    with sf() as s:
        state = "EXPIRED_DAY" if bad_evidence == "expiry_without_receipt" else "FILLED"
        s.add(_sell("S1", D[2], state, "2026-12-03T15:30:00+08:00"))
        if bad_evidence != "absent":
            broker = _broker_order("S1", status=50 if state == "EXPIRED_DAY" else 56)
            if bad_evidence == "other_symbol":
                broker["symbol"] = "600002.SH"
            s.add(_broker_snapshot("invalid", D[2], [broker],
                                   account_alias="unrelated" if bad_evidence == "other_account" else "shared"))
        s.commit()
    with pytest.raises(SyntheticEvidenceUnavailable, match="S1"):
        load_inputs(sf, load_synthetic_policy(POLICY_FILE))
    output = tmp_path / "synthetic.json"
    output.write_text('{"status": "CURRENT"}')
    assert run_v713_synthetic.main(["--policy", str(POLICY_FILE), "--output", str(output)],
                                   session_factory=sf) == 0
    document = json.loads(output.read_text())
    assert document["status"] == "WAITING_ORDER_EVIDENCE"
    assert document["missing_order_evidence"] == ["S1"]
    assert document["as_of_date"] is None


def test_cash_only_not_submitted_sell_needs_completed_eod_receipt(tmp_path):
    sf = _database(tmp_path)
    with sf() as s:
        s.add(_sell("S1", D[2], "NOT_SUBMITTED", "2026-12-03T14:56:00+08:00"))
        s.add(_broker_snapshot("empty-eod", D[2], []))
        s.commit()
    with pytest.raises(SyntheticEvidenceUnavailable):
        load_inputs(sf, load_synthetic_policy(POLICY_FILE))
    with sf() as s:
        s.add(OmsReconciliation(snapshot_id="empty-eod", cycle_id="C00001", passed=True,
                                discrepancies=[], created_at="2026-12-03T15:30:01+08:00"))
        s.commit()
    assert not load_inputs(sf, load_synthetic_policy(POLICY_FILE))["open_sell_dates"]


def test_future_superseded_intent_does_not_block_earlier_closes(tmp_path):
    sf = _database(tmp_path)
    policy = load_synthetic_policy(POLICY_FILE)
    before = run_synthetic(policy, bps=20, **load_inputs(sf, policy))
    with sf() as s:
        # This is a historical unsafe supersede shape: the current terminal flag
        # alone does not prove a previously cached offline plan was revoked.
        s.add(_sell("future", D[6], "NOT_SUBMITTED", "2026-12-04T18:00:00+08:00"))
        s.commit()
    assert run_synthetic(policy, bps=20, **load_inputs(sf, policy)) == before
    with sf() as s:
        for instance in ("live_v713_stockleg", "live_hydra_v481_rb"):
            s.add(PerfSnapshot(instance_id=instance, date=D[6], execution_domain="live",
                               nav=200_000.0, daily_return=0.0, positions_snapshot={}))
        s.commit()
    # Reaching the intent's actual trade date still requires proof; do not silently
    # treat the mutable NOT_SUBMITTED flag as a confirmed cancellation.
    with pytest.raises(SyntheticEvidenceUnavailable, match="future"):
        load_inputs(sf, policy)
    with sf() as s:
        # The immutable same-transaction cancellation receipt is sufficient even
        # after the never-issued intent's future trade date has passed.
        s.add(_supersession("future", D[6], "FUTURE_TRADE_DATE"))
        s.commit()
    assert not load_inputs(sf, policy)["open_sell_dates"]


def test_never_approved_cycle_cancellation_has_immutable_evidence(tmp_path):
    sf = _database(tmp_path)
    with sf() as s:
        s.add(_sell("unapproved", D[1], "NOT_SUBMITTED", "2026-12-04T18:00:00+08:00"))
        s.add(_supersession("unapproved", D[1], "UNAPPROVED_CYCLE"))
        s.commit()
    # The intent was safely terminated on Dec 4, not at its mutable updated_at.
    assert load_inputs(sf, load_synthetic_policy(POLICY_FILE))["open_sell_dates"] == {D[1], D[2]}


@pytest.mark.parametrize("bad_receipt", ["wrong_account", "wrong_order", "wrong_instance",
                                       "not_future", "approved_cycle"])
def test_supersession_evidence_must_prove_this_intent_was_safe(tmp_path, bad_receipt):
    sf = _database(tmp_path)
    with sf() as s:
        s.add(_sell("S1", D[4], "NOT_SUBMITTED", "2026-12-04T18:00:00+08:00"))
        receipt = _supersession("S1", D[4], "FUTURE_TRADE_DATE")
        if bad_receipt == "wrong_account":
            receipt.account_alias = "other"
        elif bad_receipt == "wrong_order":
            receipt.payload["not_submitted_orders"][0]["client_order_id"] = "different"
        elif bad_receipt == "wrong_instance":
            receipt.payload["instance_id"] = "another-strategy"
        elif bad_receipt == "not_future":
            receipt.created_at = "2026-12-05T18:00:00+08:00"
        else:
            receipt.payload["not_submitted_orders"][0]["safety_basis"] = "UNAPPROVED_CYCLE"
            receipt.payload["prior_cycle_status"] = "ACTIVE"
        s.add(receipt)
        s.commit()
    with pytest.raises(SyntheticEvidenceUnavailable, match="S1"):
        load_inputs(sf, load_synthetic_policy(POLICY_FILE))


def test_one_stale_leg_is_waiting_even_when_the_job_is_new(tmp_path):
    sf = _database(tmp_path)
    with sf() as s:
        s.delete(s.get(PerfSnapshot, ("live_hydra_v481_rb", D[4])))
        s.commit()
    output = tmp_path / "synthetic.json"
    run_v713_synthetic.main(["--policy", str(POLICY_FILE), "--output", str(output)], session_factory=sf,
                            now="2026-12-05T18:00:00+08:00")
    document = json.loads(output.read_text())
    assert document["status"] == "WAITING_LEG_NAV"
    assert document["as_of_date"] == D[3] and document["expected_date"] == D[4]


def test_explicit_expected_session_detects_both_legs_stale(tmp_path):
    sf = _database(tmp_path)
    output = tmp_path / "synthetic.json"
    run_v713_synthetic.main(["--policy", str(POLICY_FILE), "--output", str(output), "--as-of", D[7]],
                            session_factory=sf)
    document = json.loads(output.read_text())
    assert document["status"] == "WAITING_LEG_NAV"
    assert document["as_of_date"] == D[4] and document["expected_date"] == D[7]
