"""V713's explicit first cash entry: dated anchors, idempotency and EOD safety."""
from dataclasses import replace
from datetime import datetime
import json

import pytest

from app.models import InstanceState
from app.oms.cycles import CycleConflict, _sha
from app.oms.models import (OmsBrokerSnapshot, OmsCycle, OmsOrder, OmsOverride, OmsReconciliation,
                            OmsSession, OmsTargetVersion, V713StateRecord)
from app.oms.planner import PlannedOrder, policy_from_dict, publication_plan
from app.oms.schemas import SnapshotIn
from tests.unit.test_oms_stock_leg import (ALIAS, INSTANCE, CLOSES, CAL_LONG, TEMPLATE_V2,
                                         _eod, _service, expand_policy)

NOW = "2026-10-30T18:00:00+08:00"
CAL = CAL_LONG + ["20261118", "20261119", "20261120", "20261123", "20261124"]


def evidence(day="20261030", closes=None):
    date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    return {"trade_date": day, "taken_at": f"{date}T15:30:00+08:00", "quotes": {
        symbol: {"last_price": price, "source_time": f"{date}T15:00:05+08:00",
                 "adjustment": "none", "price_source": "raw_daily_close"}
        for symbol, price in (closes or {"600002.SH": 20.0}).items()}}


def policy():
    return policy_from_dict(expand_policy(json.loads(TEMPLATE_V2.read_text()), ["600001.SH", "600002.SH"]))


def publish(svc, **changes):
    return svc.publish_target(**({"instance_id": INSTANCE, "account_alias": ALIAS,
        "signal_date": "20261030", "weights": {"600002.SH": 1.0}, "signal_closes": CLOSES,
        "calendar": CAL, "source_sha256": "a" * 64, "now": NOW, "policy": policy(),
        "earliest_execution_date": "20261102", "initial_cash_buy": True,
        "reference_close_evidence": evidence()} | changes))


def plan(**changes):
    return publication_plan(**({"policy": policy(), "calendar": CAL, "signal_date": "20261030",
        "now": NOW, "signal_closes": CLOSES, "symbols": ["600002.SH"],
        "earliest_execution_date": "20261102", "initial_cash_buy": True,
        "reference_close_evidence": evidence()} | changes))


@pytest.mark.parametrize("now", [NOW, "2026-11-01T18:00:00+08:00", "2026-11-02T08:59:59+08:00"])
def test_initial_buy_starts_monday_and_has_ten_consecutive_trading_days(now):
    start = plan(now=now)
    assert [(s.trade_date, s.phase) for s in start.schedule] == [
        (day, "BUY") for day in ["20261102", "20261103", "20261104", "20261105", "20261106",
                                "20261109", "20261110", "20261111", "20261112", "20261113"]]
    assert start.reference_date == "20261030" and start.buy_anchor == {"600002.SH": 20.0}


def test_nine_am_cutoff_requires_next_session_and_that_sessions_previous_close():
    with pytest.raises(ValueError, match="20261102"):
        plan(now="2026-11-02T09:00:00+08:00")
    with pytest.raises(ValueError, match="invalid evidence time"):
        plan(now="2026-11-02T09:00:00+08:00", reference_close_evidence=evidence("20261102"))
    late = plan(now="2026-11-02T18:00:00+08:00",
                reference_close_evidence=evidence("20261102", {"600002.SH": 21.0}))
    assert late.schedule[0].trade_date == "20261103" and late.buy_anchor == {"600002.SH": 21.0}
    weekend = plan(now="2026-11-07T18:00:00+08:00", reference_close_evidence=evidence("20261106"))
    assert weekend.schedule[0].trade_date == "20261109" and weekend.reference_date == "20261106"


@pytest.mark.parametrize("field,value", [
    ("last_price", float("inf")), ("last_price", 0), ("last_price", True),
    ("source_time", "2026-10-29T15:00:05+08:00"),
    ("source_time", "2026-10-30T14:59:59+08:00"),
    ("source_time", "2026-10-30T15:00:05"),
    ("source_time", "2026-10-30T16:00:05+08:00"),
    ("adjustment", "forward"), ("price_source", "derived"),
])
def test_initial_price_evidence_rejects_invalid_prices_dates_and_sources(field, value):
    proof = evidence()
    proof["quotes"]["600002.SH"][field] = value
    with pytest.raises(ValueError, match="close evidence"):
        plan(reference_close_evidence=proof)


def test_initial_price_evidence_cannot_be_flat_or_disagree_with_signal_day_close():
    with pytest.raises(ValueError, match="dated close evidence"):
        plan(reference_close_evidence={"600002.SH": 20.0})
    with pytest.raises(ValueError, match="signal closes differ"):
        plan(reference_close_evidence=evidence(closes={"600002.SH": 21.0}))
    with pytest.raises(ValueError, match="shorter"):
        plan(calendar=["20261030", "20261102"])


@pytest.mark.parametrize("changes", [{"strategy_id": "hydra"}, {"policy_id": "V713_MIXED"},
    {"anchor": "fixed"}, {"require_instrument_specs": False}])
def test_first_buy_requires_explicit_stockleg_policy(changes):
    with pytest.raises(ValueError, match="V713_STOCKLEG"):
        plan(policy=replace(policy(), **changes))


def test_publishes_buy_with_receipt_and_accepts_other_strategy_positions(tmp_path):
    sf, svc = _service(tmp_path)
    with sf() as session:
        session.add(InstanceState(instance_id="hydra", execution_domain="live", account_alias=ALIAS,
            ledger_mode="attributed", virtual_cash=1000, virtual_positions={"510300.SH": 100},
            owned_symbols=["510300.SH"], last_update=NOW))
        session.commit()
    out = publish(svc)
    assert out["first_session"]["phase"] == "BUY" and out["first_session"]["trade_date"] == "20261102"
    assert [(o["quantity"], o["limit_price"]) for o in out["first_session"]["orders"]] == [(9800, 20.1)]
    with sf() as session:
        receipt = session.query(OmsOverride).filter_by(action="INITIAL_CASH_ENTRY").one()
        assert receipt.payload["reference_evidence_sha256"] == _sha(receipt.payload["reference_close_evidence"])
        assert receipt.payload["cycle_id"] == out["cycle_id"]
        assert session.get(InstanceState, INSTANCE).strategy_state["oms_target_metadata"]["initial_cash_buy"]
        assert session.get(OmsCycle, out["cycle_id"]).policy == json.loads(json.dumps(policy().__dict__))


def test_same_target_replay_keeps_frozen_schedule_and_rejects_changed_mode_or_evidence(tmp_path):
    sf, svc = _service(tmp_path)
    out = publish(svc)
    normalized_equivalent = evidence()
    normalized_equivalent["unused"] = "ignored"
    normalized_equivalent["quotes"]["600002.SH"]["source_time"] = "2026-10-30T07:00:05+00:00"
    replay = publish(svc, now="2026-11-03T18:00:00+08:00", reference_close_evidence=normalized_equivalent)
    assert replay["already_published"] and replay["schedule"] == out["schedule"]
    with pytest.raises(CycleConflict, match="mode"):
        publish(svc, initial_cash_buy=False)
    with pytest.raises(CycleConflict, match="different close evidence"):
        publish(svc, reference_close_evidence=evidence(closes={"600002.SH": 21.0}))
    with pytest.raises(CycleConflict, match="calendar"):
        publish(svc, calendar=CAL + ["20261125"])
    with sf() as session:
        assert session.query(OmsCycle).count() == session.query(OmsOverride).count() == 1


@pytest.mark.parametrize("change,error", [({"virtual_positions": {"600001.SH": 1}}, "empty"),
    ({"virtual_cash": 0}, "positive"), ({"ledger_mode": "dedicated"}, "attributed"),
    ({"strategy_state": {"accounting_hold": "review"}}, "accounting review")])
def test_initial_eligibility_is_per_strategy_and_refuses_unsafe_state(tmp_path, change, error):
    sf, svc = _service(tmp_path)
    with sf() as session:
        inst = session.get(InstanceState, INSTANCE)
        for key, value in change.items():
            setattr(inst, key, value)
        session.commit()
    with pytest.raises(CycleConflict, match=error):
        publish(svc)
    with sf() as session:
        assert session.query(OmsCycle).count() == session.query(OmsTargetVersion).count() == 0


def test_first_entry_does_not_reinterpret_an_existing_empty_sell_cycle(tmp_path):
    sf, svc = _service(tmp_path)
    old = publish(svc, initial_cash_buy=False)
    assert old["first_session"]["phase"] == "SELL"
    with pytest.raises(CycleConflict, match="mode"):
        publish(svc)
    with pytest.raises(CycleConflict, match="no prior"):
        publish(svc, source_sha256="b" * 64, supersede=True)
    with sf() as session:
        assert session.get(OmsCycle, old["cycle_id"]).status == "PENDING_APPROVAL"


def test_prior_aux_state_and_manual_order_history_are_not_first_entry(tmp_path):
    sf, svc = _service(tmp_path / "aux")
    with sf() as session:
        session.add(V713StateRecord(instance_id=INSTANCE, signal_date="20260930", sleeve="AUX_HYDRA",
            decision_date="20261008", basket_sha256="x", allocation_sha256="x", recorded_at=NOW))
        session.commit()
    with pytest.raises(CycleConflict, match="no prior"):
        publish(svc)
    sf, svc = _service(tmp_path / "manual")
    with sf() as session:
        order = svc.ledger.add_order(session, client_order_id="E261030001", session_id="MANUAL:20261030",
            cycle_id="MANUAL", account_alias=ALIAS, instance_id=INSTANCE, trade_date="20261030",
            target_id="MANUAL", plan_sha256=None, attempt_number=None,
            item=PlannedOrder("600002.SH", "BUY", 100, 20.1, 20.0), now=NOW)
        order.state, order.filled_qty = "FILLED", 100
        session.commit()
    with pytest.raises(CycleConflict, match="order history"):
        publish(svc)


def test_unattributed_open_order_blocks_initial_entry(tmp_path):
    sf, svc = _service(tmp_path)
    with sf() as session:
        session.add(OmsOrder(client_order_id="UNKNOWN", session_id="UNKNOWN", cycle_id="UNKNOWN",
            account_alias=ALIAS, trade_date="20261030", symbol="600002.SH", side="BUY", quantity=100,
            limit_price=20.1, reference_price=20.0, state="PLANNED", created_at=NOW, updated_at=NOW))
        session.commit()
    with pytest.raises(ValueError, match="unattributed OMS order"):
        publish(svc)


def test_invalid_evidence_rolls_back_monthly_state_and_publication_atomically(tmp_path):
    sf, svc = _service(tmp_path)
    state = {"provenance": {"sleeve": "TOP50", "decision_date": "20261102",
                           "basket_sha256": "a" * 64, "allocation_sha256": "a" * 64},
             "signal_date": "20261030", "source_sha256": "a" * 64}
    with pytest.raises(ValueError, match="dated close evidence"):
        publish(svc, reference_close_evidence=None, v713_state=state)
    with sf() as session:
        assert all(session.query(model).count() == 0
                   for model in (V713StateRecord, OmsTargetVersion, OmsCycle, OmsSession, OmsOrder, OmsOverride))


def test_closed_cycle_is_still_prior_history_for_initial_entry(tmp_path):
    sf, svc = _service(tmp_path)
    out = publish(svc, initial_cash_buy=False)
    with sf() as session:
        session.get(OmsCycle, out["cycle_id"]).status = "CLOSED"
        session.commit()
    with pytest.raises(CycleConflict, match="no prior"):
        publish(svc, source_sha256="b" * 64)


@pytest.mark.parametrize("now", ["2026-11-02T09:00:00+08:00", "2026-11-03T08:00:00+08:00"])
def test_late_approval_cannot_bypass_the_first_buy_pre(tmp_path, now):
    sf, svc = _service(tmp_path)
    out = publish(svc)
    with pytest.raises(ValueError, match="09:00 PRE"):
        svc.approve(out["cycle_id"], "test", now)
    with sf() as session:
        assert session.get(OmsCycle, out["cycle_id"]).status == "PENDING_APPROVAL"


def test_first_buy_progresses_across_weekend_and_closes_after_ten_sessions(tmp_path):
    sf, svc = _service(tmp_path)
    out = publish(svc)
    svc.approve(out["cycle_id"], "test", "2026-11-02T08:59:59+08:00")
    for i, spec in enumerate(out["schedule"]):
        result = _eod(svc, spec["trade_date"], {"600002.SH": 20.0 + i}, {}, 200000.0)
        assert result["reconciliation"]["passed"]
        if i < 9:
            buy = result["planned_sessions"][0]
            assert buy["phase"] == "BUY" and buy["trade_date"] == out["schedule"][i + 1]["trade_date"]
            assert buy["orders"][0]["reference_price"] == 20.0 + i
        else:
            assert result["cycle_status"] == "CLOSED" and not result["planned_sessions"]
    with sf() as session:
        assert session.query(OmsSession).filter_by(cycle_id=out["cycle_id"]).count() == 10
        assert session.get(OmsCycle, out["cycle_id"]).close_reason == "WINDOW_COMPLETE"


def test_missing_initial_buy_eod_holds_instead_of_planning_over_old_responsibility(tmp_path):
    sf, svc = _service(tmp_path)
    out = publish(svc)
    svc.approve(out["cycle_id"], "test", NOW)
    result = _eod(svc, "20261103", {"600002.SH": 20.0}, {}, 200000.0)
    assert result["cycle_status"] == "HELD" and not result["planned_sessions"]
    with sf() as session:
        assert session.query(OmsSession).count() == 1
        assert session.query(OmsOrder).one().state == "PLANNED"


def test_initial_retry_never_reuses_stale_quote_for_a_missing_symbol(tmp_path):
    sf, svc = _service(tmp_path)
    out = publish(svc, weights={"600001.SH": .5, "600002.SH": .5},
                  reference_close_evidence=evidence(closes={"600001.SH": 10.0, "600002.SH": 20.0}))
    svc.approve(out["cycle_id"], "test", NOW)
    result = _eod(svc, "20261102", {"600001.SH": 10.1}, {}, 200000.0)
    buy = result["planned_sessions"][0]
    assert [x["symbol"] for x in buy["orders"]] == ["600001.SH"]
    assert any(x["symbol"] == "600002.SH" and x["reason"] == "NO_ANCHOR" for x in buy["deferrals"])
    missing = _eod(svc, "20261103", {}, {}, 200000.0)
    assert missing["cycle_status"] == "HELD" and not missing["planned_sessions"]


def test_new_cash_grant_requires_post_approval_nine_am_pre_but_allows_later_adhoc(tmp_path):
    sf, svc = _service(tmp_path)
    svc.account_fingerprint = "test-only-account"

    def snap(kind, stamp):
        parsed = datetime.fromisoformat(stamp)
        payload = SnapshotIn(account_alias=ALIAS, kind=kind, trade_date=parsed.strftime("%Y%m%d"),
            taken_at=parsed, available_cash=200000.0, total_asset=200000.0,
            positions={}, sellable={}, orders=[], trades=None, quotes={})
        assert svc.ingest_snapshot(payload, stamp)["reconciliation"]["passed"]

    snap("PRE", "2026-11-02T08:58:00+08:00")
    out = publish(svc, now="2026-11-02T08:59:00+08:00")
    svc.approve(out["cycle_id"], "test", "2026-11-02T08:59:30+08:00")
    with sf() as session:
        inst = session.get(InstanceState, INSTANCE)
        inst.strategy_state = {**inst.strategy_state, "oms_enabled": True}
        session.commit()

    def grant():
        return svc.plan_for(ALIAS, "20261102", "BUY", executable_flag=True, wire_version=2,
                            now="2026-11-02T09:15:05+08:00")

    assert grant().executable is False
    snap("ADHOC", "2026-11-02T09:00:00+08:00")
    assert grant().executable is False
    snap("PRE", "2026-11-02T09:00:00+08:00")
    assert grant().executable is True
    snap("ADHOC", "2026-11-02T09:14:59+08:00")
    assert grant().executable is True

    # A newly observed open broker order in the new target prevents execution.
    unknown = SnapshotIn(account_alias=ALIAS, kind="PRE", trade_date="20261102",
        taken_at=datetime.fromisoformat("2026-11-02T09:15:00+08:00"), available_cash=200000,
        total_asset=200000, positions={}, sellable={}, quotes={}, trades=None,
        orders=[{"broker_order_id": "external", "symbol": "600002.SH", "side": "BUY",
                 "quantity": 100, "price": 20.0, "traded_volume": 0, "traded_price": 0,
                 "status": 50, "remark": "manual"}])
    result = svc.ingest_snapshot(unknown, unknown.taken_at.isoformat())
    assert not result["reconciliation"]["passed"] and result["cycle_status"] == "HELD"
    assert grant().executable is False


def test_pre_client_clock_ninety_seconds_ahead_cannot_authorize_a_later_publication(tmp_path):
    sf, svc = _service(tmp_path)
    svc.account_fingerprint = "test-only-account"
    early = SnapshotIn(account_alias=ALIAS, kind="PRE", trade_date="20261102",
        taken_at=datetime.fromisoformat("2026-11-02T09:00:00+08:00"), available_cash=200000,
        total_asset=200000, positions={}, sellable={}, orders=[], quotes={}, trades=None)
    svc.ingest_snapshot(early, "2026-11-02T08:58:30+08:00")
    out = publish(svc, now="2026-11-02T08:59:00+08:00")
    svc.approve(out["cycle_id"], "test", "2026-11-02T08:59:30+08:00")
    with sf() as session:
        inst = session.get(InstanceState, INSTANCE)
        inst.strategy_state = {**inst.strategy_state, "oms_enabled": True}
        assert session.query(OmsReconciliation).one().cycle_id is None
        session.commit()
    grant = svc.plan_for(ALIAS, "20261102", "BUY", executable_flag=True, wire_version=2,
                         now="2026-11-02T09:15:05+08:00")
    assert grant.executable is False


@pytest.mark.parametrize("invalid", ["received_before_approval", "reconciled_before_approval",
    "received_in_future", "reconciled_in_future", "wrong_cycle", "other_strategy_failed"])
def test_initial_grant_requires_server_receipt_and_current_cycle_reconciliation(tmp_path, invalid):
    sf, svc = _service(tmp_path)
    svc.account_fingerprint = "test-only-account"
    out = publish(svc, now="2026-11-02T08:59:00+08:00")
    svc.approve(out["cycle_id"], "test", "2026-11-02T08:59:30+08:00")
    pre = SnapshotIn(account_alias=ALIAS, kind="PRE", trade_date="20261102",
        taken_at=datetime.fromisoformat("2026-11-02T09:00:00+08:00"), available_cash=200000,
        total_asset=200000, positions={}, sellable={}, orders=[], quotes={}, trades=None)
    svc.ingest_snapshot(pre, "2026-11-02T09:00:00+08:00")
    with sf() as session:
        inst = session.get(InstanceState, INSTANCE)
        inst.strategy_state = {**inst.strategy_state, "oms_enabled": True}
        receipt = session.query(OmsBrokerSnapshot).one()
        recon = session.query(OmsReconciliation).one()
        assert recon.cycle_id == out["cycle_id"]
        if invalid == "received_before_approval":
            receipt.received_at = "2026-11-02T08:59:10+08:00"
        elif invalid == "reconciled_before_approval":
            recon.created_at = "2026-11-02T08:59:10+08:00"
        elif invalid == "received_in_future":
            receipt.received_at = "2026-11-02T09:16:00+08:00"
        elif invalid == "reconciled_in_future":
            recon.created_at = "2026-11-02T09:16:00+08:00"
        elif invalid == "wrong_cycle":
            recon.cycle_id = "another-cycle"
        else:
            session.add(OmsReconciliation(snapshot_id=receipt.snapshot_id, cycle_id="hydra-cycle",
                passed=False, discrepancies=[{"type": "UNKNOWN_OPEN_BROKER_ORDER"}],
                created_at="2026-11-02T09:00:00+08:00"))
        session.commit()
    grant = svc.plan_for(ALIAS, "20261102", "BUY", executable_flag=True, wire_version=2,
                         now="2026-11-02T09:15:05+08:00")
    assert grant.executable is False
