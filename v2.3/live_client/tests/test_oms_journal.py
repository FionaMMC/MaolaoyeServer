"""Local OMS journal: immutable plan cache, durable intents, event outbox."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from live_client.oms_journal import JournalConflict, OmsJournal

CST = timezone(timedelta(hours=8))
FIXED = datetime(2026, 10, 8, 14, 57, 5, tzinfo=CST)


def _plan(**changes) -> dict:
    plan = {
        "account_alias": "hydra-live",
        "cycle_id": "C00001",
        "session_id": "C00001-01",
        "trade_date": "20261008",
        "phase": "SELL",
        "executable": True,
        "plan_sha256": "a" * 64,
        "frozen_target": {"510300.SH": 1000, "518880.SH": 0},
        "orders": [
            {"client_order_id": "H000010101", "symbol": "510300.SH", "side": "SELL",
             "quantity": 1500, "limit_price": 4.598},
            {"client_order_id": "H000010102", "symbol": "518880.SH", "side": "SELL",
             "quantity": 800, "limit_price": 9.059},
        ],
    }
    plan.update(changes)
    return plan


def _journal(tmp_path) -> OmsJournal:
    return OmsJournal(tmp_path / "oms" / "oms-agent.db", clock=lambda: FIXED)


def test_cached_plan_round_trips_and_identical_recache_is_a_no_op(tmp_path):
    journal = _journal(tmp_path)
    assert journal.plan("20261008", "SELL") is None

    journal.cache_plan(_plan())
    journal.cache_plan(_plan())

    assert journal.plan("20261008", "SELL") == _plan()
    assert journal.plan("20261008", "BUY") is None
    assert journal.plan("20261009", "SELL") is None


def test_cached_plan_cannot_be_overwritten(tmp_path):
    journal = _journal(tmp_path)
    journal.cache_plan(_plan())

    changed_orders = _plan()
    changed_orders["orders"][0]["quantity"] = 1600
    with pytest.raises(JournalConflict, match="plan_sha256"):
        journal.cache_plan(changed_orders)

    with pytest.raises(JournalConflict, match="session_id"):
        journal.cache_plan({**changed_orders, "plan_sha256": "b" * 64})

    reused_order_id = _plan(session_id="C00001-03", trade_date="20261012", plan_sha256="c" * 64)
    with pytest.raises(JournalConflict, match="H000010101"):
        journal.cache_plan(reused_order_id)

    # Nothing from the rejected attempts leaked into the cache.
    assert journal.plan("20261008", "SELL") == _plan()
    assert journal.plan("20261012", "SELL") is None


def test_executable_flag_follows_latest_server_answer_but_orders_stay_frozen(tmp_path):
    journal = _journal(tmp_path)
    journal.cache_plan(_plan(executable=False))   # fetched before approval
    journal.cache_plan(_plan(executable=True))    # same plan after approval
    assert journal.plan("20261008", "SELL")["executable"] is True
    journal.cache_plan(_plan(executable=False))   # cycle held later
    assert journal.plan("20261008", "SELL")["executable"] is False


def test_cache_plan_rejects_malformed_payload(tmp_path):
    journal = _journal(tmp_path)
    broken = _plan()
    del broken["orders"][0]["client_order_id"]
    with pytest.raises(ValueError):
        journal.cache_plan(broken)
    with pytest.raises(ValueError):
        journal.cache_plan(_plan(phase="LUNCH"))
    with pytest.raises(ValueError):
        journal.cache_plan(_plan(trade_date="2026-10-08"))
    assert journal.plan("20261008", "SELL") is None


def test_mark_writes_intent_and_outbox_event_in_order(tmp_path):
    journal = _journal(tmp_path)
    journal.cache_plan(_plan())

    started = journal.mark("H000010101", "SUBMITTING", None, None)
    assert started["state"] == "SUBMITTING"
    assert started["symbol"] == "510300.SH" and started["side"] == "SELL"
    assert started["quantity"] == 1500 and started["limit_price"] == 4.598
    acked = journal.mark("H000010101", "ACKED", "12345", "QMT accepted")
    assert acked["state"] == "ACKED" and acked["broker_order_id"] == "12345"
    journal.mark("H000010102", "SUBMITTING", None, None)
    journal.mark("H000010102", "REJECTED", None, "order_stock returned -1")

    events = journal.outbox()
    assert [(e["client_order_id"], e["kind"]) for e in events] == [
        ("H000010101", "SUBMIT_STARTED"),
        ("H000010101", "ACKED"),
        ("H000010102", "SUBMIT_STARTED"),
        ("H000010102", "SUBMIT_REJECTED"),
    ]
    assert set(events[1]) == {
        "event_id", "client_order_id", "kind", "observed_at", "broker_order_id", "detail",
    }
    assert events[1]["broker_order_id"] == "12345"
    assert events[1]["detail"] == "QMT accepted"
    assert events[1]["observed_at"] == FIXED.isoformat()
    assert len({e["event_id"] for e in events}) == 4
    assert all(len(e["event_id"]) >= 8 for e in events)
    assert len(journal.outbox(limit=2)) == 2


def test_unknown_state_maps_to_submit_unknown_and_can_be_recovered(tmp_path):
    journal = _journal(tmp_path)
    journal.cache_plan(_plan())
    journal.mark("H000010101", "SUBMITTING", None, None)
    journal.mark("H000010101", "UNKNOWN", None, "order_stock raised TimeoutError")
    recovered = journal.mark("H000010101", "ACKED", "777", "found by remark")
    assert recovered["state"] == "ACKED"
    assert [e["kind"] for e in journal.outbox()] == ["SUBMIT_STARTED", "SUBMIT_UNKNOWN", "ACKED"]


def test_order_found_by_remark_before_submit_can_be_acked_directly(tmp_path):
    journal = _journal(tmp_path)
    journal.cache_plan(_plan())
    assert journal.mark("H000010101", "ACKED", "555", "found by remark")["state"] == "ACKED"
    assert [e["kind"] for e in journal.outbox()] == ["ACKED"]


def test_submitting_is_a_one_time_claim_and_illegal_moves_raise(tmp_path):
    journal = _journal(tmp_path)
    journal.cache_plan(_plan())
    journal.mark("H000010101", "SUBMITTING", None, None)
    with pytest.raises(JournalConflict):
        journal.mark("H000010101", "SUBMITTING", None, None)

    journal.mark("H000010101", "ACKED", "12345", None)
    # Same acknowledgement again is idempotent and does not emit a second event.
    assert journal.mark("H000010101", "ACKED", "12345", None)["state"] == "ACKED"
    assert len(journal.outbox()) == 2
    with pytest.raises(JournalConflict):
        journal.mark("H000010101", "ACKED", "99999", None)
    with pytest.raises(JournalConflict):
        journal.mark("H000010101", "UNKNOWN", None, None)

    journal.mark("H000010102", "SUBMITTING", None, None)
    journal.mark("H000010102", "REJECTED", None, None)
    with pytest.raises(JournalConflict):
        journal.mark("H000010102", "SUBMITTING", None, None)
    with pytest.raises(JournalConflict):
        journal.mark("H000010102", "ACKED", "1", None)


def test_mark_validates_state_identity_and_broker_id(tmp_path):
    journal = _journal(tmp_path)
    journal.cache_plan(_plan())
    with pytest.raises(ValueError):
        journal.mark("H000010101", "FILLED", None, None)
    with pytest.raises(KeyError):
        journal.mark("H999990101", "SUBMITTING", None, None)
    with pytest.raises(ValueError):
        journal.mark("H000010101", "ACKED", None, None)
    assert journal.intent("H000010101") is None
    assert journal.outbox() == []


def test_acknowledge_empties_outbox_and_ignores_unknown_ids(tmp_path):
    journal = _journal(tmp_path)
    journal.cache_plan(_plan())
    journal.mark("H000010101", "SUBMITTING", None, None)
    journal.mark("H000010101", "ACKED", "12345", None)
    first, second = journal.outbox()

    journal.acknowledge([first["event_id"], "not-an-event"])
    assert journal.outbox() == [second]
    journal.acknowledge([second["event_id"]])
    journal.acknowledge([second["event_id"]])
    assert journal.outbox() == []


def test_restart_still_sees_submitting_intent_and_pending_events(tmp_path):
    journal = _journal(tmp_path)
    plan = _plan()
    journal.cache_plan(copy.deepcopy(plan))
    journal.mark("H000010101", "SUBMITTING", None, None)
    del journal  # simulated crash between the journal write and the broker call

    reopened = _journal(tmp_path)
    assert reopened.intent("H000010101")["state"] == "SUBMITTING"
    assert reopened.intent("H000010102") is None
    assert [e["kind"] for e in reopened.outbox()] == ["SUBMIT_STARTED"]
    assert reopened.plan("20261008", "SELL") == plan
    with pytest.raises(JournalConflict):
        reopened.mark("H000010101", "SUBMITTING", None, None)


def test_hold_flag_is_a_file_next_to_the_journal(tmp_path):
    journal = _journal(tmp_path)
    assert journal.hold_flag() is False
    (tmp_path / "oms" / "HOLD").write_text("operator hold", encoding="utf-8")
    assert journal.hold_flag() is True
