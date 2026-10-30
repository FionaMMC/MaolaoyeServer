"""Independent shared-account regressions for grant, recovery and ownership safety."""
from datetime import datetime
from copy import deepcopy

from sqlalchemy import select

from app.models import DividendEntitlement, InstanceState
from app.oms.account import open_reservations, strategy_resources
from app.oms.manual import DividendIn, ManualOrderIn, ManualService
from app.oms.models import OmsBrokerSnapshot, OmsCycle, OmsOrder
from app.oms.schemas import plan_content_sha256
from live_client.oms_journal import OmsJournal
from tests.unit.test_oms_shared_account import ALIAS, CN, HYDRA, V713, setup_shared, snapshot


def manual_buy(manual, *, owner=V713, now=None):
    return manual.create_order(ManualOrderIn(account_alias=ALIAS, instance_id=owner,
        symbol="510300.SH", side="BUY", quantity=100, limit_price=4.,
        reason="reviewed synthetic manual purchase", operator="test", confirm=True),
        now or datetime(2026, 10, 19, 10, tzinfo=CN))


def grant(service, *, now="2026-10-19T14:46:00+08:00"):
    return service.plan_for(ALIAS, "20261019", "SELL", executable_flag=True,
                            instance_id=V713, wire_version=2, now=now)


def test_latest_failed_adhoc_reconciliation_revokes_execution_grant(tmp_path):
    _, service, _, _, _ = setup_shared(tmp_path)
    service.ingest_snapshot(snapshot(), "2026-10-19T14:45:00+08:00")
    assert grant(service).executable
    result = service.ingest_snapshot(snapshot(hhmm="1447", kind="ADHOC",
        positions={"510300.SH": 900, "000001.SZ": 1000}), "2026-10-19T14:47:00+08:00")
    assert not result["reconciliation"]["passed"]
    assert not grant(service, now="2026-10-19T14:48:00+08:00").executable


def test_adhoc_cash_shortfall_revokes_execution_grant(tmp_path):
    _, service, _, _, _ = setup_shared(tmp_path)
    service.ingest_snapshot(snapshot(), "2026-10-19T14:45:00+08:00")
    assert grant(service).executable
    result = service.ingest_snapshot(snapshot(hhmm="1447", kind="ADHOC", available_cash=1.),
                                     "2026-10-19T14:47:00+08:00")
    assert not result["reconciliation"]["passed"]
    assert any(item["type"] == "CASH_SHORTFALL" for item in result["reconciliation"]["discrepancies"])
    assert not grant(service, now="2026-10-19T14:48:00+08:00").executable


def test_shared_open_buy_requires_observed_frozen_cash_and_uses_that_amount(tmp_path):
    sf, service, _, _, _ = setup_shared(tmp_path)
    manual = ManualService(sf, service)
    created = manual.create_order(ManualOrderIn(account_alias=ALIAS, instance_id=HYDRA,
        symbol="510300.SH", side="BUY", quantity=1000, limit_price=4.,
        reason="synthetic broker frozen cash evidence", operator="test", confirm=True),
        datetime(2026, 10, 19, 10, tzinfo=CN))
    broker = dict(broker_order_id="synthetic-open-buy", symbol="510300.SH", side="BUY",
                  quantity=1000, price=4., traded_volume=0, traded_price=0., status=50,
                  remark=created["client_order_id"])
    missing = service.ingest_snapshot(snapshot(hhmm="1445", kind="ADHOC", available_cash=25995.,
                                               orders=[broker]), "2026-10-19T14:45:00+08:00")
    assert not missing["reconciliation"]["passed"]
    assert any(item["type"] == "FROZEN_CASH_UNOBSERVED"
               for item in missing["reconciliation"]["discrepancies"])
    assert not grant(service).executable

    # QMT's actual freeze can differ from the strategy's planning fee budget.
    # A larger observed freeze is added back in full, without a false shortfall.
    observed = service.ingest_snapshot(snapshot(hhmm="1447", kind="ADHOC", available_cash=25800.,
        frozen_cash=4200., orders=[broker]), "2026-10-19T14:47:00+08:00")
    assert observed["reconciliation"]["passed"]
    assert grant(service, now="2026-10-19T14:48:00+08:00").executable

    # Conversely, the old 4000*1.001+5 estimate would hide this real 2 CNY gap.
    shortfall = service.ingest_snapshot(snapshot(hhmm="1449", kind="ADHOC", available_cash=25993.,
        frozen_cash=4005., orders=[broker]), "2026-10-19T14:49:00+08:00")
    discrepancy = next(item for item in shortfall["reconciliation"]["discrepancies"]
                       if item["type"] == "CASH_SHORTFALL")
    assert discrepancy["broker_cash"] == 29998.
    assert not shortfall["reconciliation"]["passed"]
    assert not grant(service, now="2026-10-19T14:50:00+08:00").executable


def test_fresh_snapshot_without_finished_reconciliation_never_grants(tmp_path):
    sf, service, _, _, _ = setup_shared(tmp_path)
    result = service.ingest_snapshot(snapshot(), "2026-10-19T14:45:00+08:00")
    assert grant(service).executable
    with sf() as session:
        original = session.get(OmsBrokerSnapshot, result["snapshot_id"])
        session.add(OmsBrokerSnapshot(snapshot_id="unfinished-latest", account_alias=ALIAS,
            kind="ADHOC", trade_date="20261019", taken_at="2026-10-19T14:47:00+08:00",
            received_at="2026-10-19T14:47:01+08:00", payload=deepcopy(original.payload)))
        session.commit()
    assert not grant(service, now="2026-10-19T14:48:00+08:00").executable


def test_shared_etf_dividend_rights_have_distinct_owner_payment_identity(tmp_path):
    sf, service, _, _, _ = setup_shared(tmp_path)
    manual = ManualService(sf, service)
    now = datetime(2026, 10, 19, 10, tzinfo=CN)
    requests = [DividendIn(account_alias=ALIAS, instance_id=owner, symbol="510300.SH",
        record_date="20261016", ex_date="20261019", pay_date="20261020", entitled_quantity=quantity,
        cash_per_share=".1", evidence=f"synthetic ownership evidence for {owner}",
        reason="reviewed synthetic dividend right", operator="test")
        for owner, quantity in ((HYDRA, 1000), (V713, 500))]
    for req in requests:
        result = manual.dividend(req, now, apply=True)
        assert result["applied"] and not result["already_registered"]
        assert manual.dividend(req, now, apply=True)["already_registered"]
    with sf() as session:
        rights = session.scalars(select(DividendEntitlement)).all()
        assert len(rights) == 2
        assert len({right.settlement_event_id for right in rights}) == 2
        assert {right.instance_id: right.amount for right in rights} == {HYDRA: "100.00", V713: "50.00"}
        assert session.get(InstanceState, HYDRA).virtual_cash == 10000
        assert session.get(InstanceState, V713).virtual_cash == 20000


def test_manual_buy_has_persisted_room_and_hash_survives_balance_refresh(tmp_path):
    sf, service, _, _, _ = setup_shared(tmp_path)
    manual = ManualService(sf, service)
    service.ingest_snapshot(snapshot(hhmm="1000", kind="ADHOC"), "2026-10-19T10:00:00+08:00")
    created = manual_buy(manual)
    first = manual.pending(ALIAS, "20261019", now="2026-10-19T10:01:00+08:00")["instructions"][0]["execution_plan"]
    assert first["orders"][0]["client_order_id"] == created["client_order_id"]
    assert first["executable"] and first["phase"] == "MANUAL"
    assert first["frozen_target"]["510300.SH"] - first["strategy_positions"].get("510300.SH", 0) == 100
    assert first["plan_sha256"] == plan_content_sha256(first)
    # A later balance observation must not silently change an approved intent
    # and cause a different hash for the same client order ID.
    with sf() as session:
        state = session.get(InstanceState, V713)
        state.virtual_positions = {**state.virtual_positions, "510300.SH": 50}
        state.virtual_cash -= 200
        session.commit()
    service.ingest_snapshot(snapshot(hhmm="1002", kind="ADHOC", available_cash=29800,
        positions={"510300.SH": 1050, "000001.SZ": 1000}), "2026-10-19T10:02:00+08:00")
    second = manual.pending(ALIAS, "20261019", now="2026-10-19T10:03:00+08:00")["instructions"][0]["execution_plan"]
    assert second["executable"]
    assert second["strategy_positions"]["510300.SH"] == 50
    assert second["frozen_target"] == first["frozen_target"]
    assert second["plan_sha256"] == first["plan_sha256"]


def test_pending_order_reserve_uses_frozen_policy_after_new_strategy_policy(tmp_path):
    sf, service, _, _, _ = setup_shared(tmp_path)
    manual = ManualService(sf, service)
    created = manual_buy(manual)
    with sf() as session:
        first = open_reservations(session, ALIAS)[V713]["cash"]
        assert created["client_order_id"] in open_reservations(session, ALIAS)[V713]["order_ids"]
        inst = session.get(InstanceState, V713)
        state = deepcopy(inst.strategy_state)
        for key in ("oms_instruments",):
            state[key]["510300.SH"]["commission_rate"] = .5
        state["oms_policy"]["instruments"]["510300.SH"]["commission_rate"] = .5
        inst.strategy_state = state
        session.commit()
    with sf() as session:
        assert open_reservations(session, ALIAS)[V713]["cash"] == first == 405


def test_other_strategy_terminal_proof_is_in_current_strategy_resource_grant(tmp_path):
    sf, service, _, _, _ = setup_shared(tmp_path)
    manual = ManualService(sf, service)
    created = manual_buy(manual, owner=HYDRA, now=datetime(2026, 10, 19, 10, tzinfo=CN))
    service.ingest_snapshot(snapshot(hhmm="1000", kind="ADHOC"), "2026-10-19T10:00:00+08:00")
    plan = manual.pending(ALIAS, "20261019", now="2026-10-19T10:01:00+08:00")["instructions"][0]["execution_plan"]
    journal = OmsJournal(tmp_path / "oms-agent.db", clock=lambda: datetime(2026, 10, 19, 10, 1, tzinfo=CN))
    journal.cache_plan(plan)
    coid = created["client_order_id"]
    journal.mark(coid, "SUBMITTING", None, None, submission={**plan["orders"][0],
        "instance_id": HYDRA, "trade_date": "20261019", "reserve_cash": 405.})
    journal.mark(coid, "UNKNOWN", None, "synthetic unconfirmed call; broker accepted nothing")
    # The broker's actual EOD absence must generate and project the terminal
    # proof, including its time; do not seed a terminal state in the database.
    result = service.ingest_snapshot(snapshot(hhmm="1505", kind="EOD"), "2026-10-19T15:05:00+08:00")
    assert result["reconciliation"]["passed"]
    with sf() as session:
        resources = strategy_resources(session, session.get(InstanceState, V713), "20261020")
        proof = resources["server_terminal_orders"][coid]
        assert proof["state"] == "NOT_SUBMITTED" and proof["projected_qty"] == proof["filled_qty"] == 0
        assert proof["last_observed_at"] == "2026-10-19T15:05:00+08:00"
        assert coid not in resources["server_accounted_fill_quantities"]
    journal.record_terminal_proof(coid, proof)
    assert journal.terminal_proof(coid) == proof
    assert journal.intent(coid)["state"] == "UNKNOWN"


def test_same_day_morning_tick_is_not_accepted_as_v713_close_anchor(tmp_path):
    sf, service, _, _, v713 = setup_shared(tmp_path)
    snap = snapshot(hhmm="1505", kind="EOD")
    snap.quotes["000002.SZ"].source_time = "2026-10-19T09:30:00+08:00"
    result = service.ingest_snapshot(snap, "2026-10-19T15:05:00+08:00")
    assert result["reconciliation"]["passed"]
    with sf() as session:
        cycle = session.get(OmsCycle, v713["cycle_id"])
        assert cycle.status == "ACTIVE" and "000002.SZ" not in cycle.buy_anchor
        assert all(o.symbol != "000002.SZ" for o in session.scalars(select(OmsOrder).where(
            OmsOrder.cycle_id == cycle.cycle_id, OmsOrder.side == "BUY")))
