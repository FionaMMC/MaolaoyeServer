"""China 15:00 seals local submissions before any broker/server dependency."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from live_client import cli, state as state_module
from live_client.core import validate_order_batch
from live_client.execution_queue import account_submission_lock
from live_client.gateway import AccountSnapshot, SubmissionResult, live_order_remark
from live_client.state import LiveStateStore
from test_live_client import _cfg, _orders


TRADE_DATE = "20260909"
CHINA = timezone(timedelta(hours=8))


def _state(tmp_path, *, with_preflight=False):
    cfg = _cfg(tmp_path, mode="live")
    batch = validate_order_batch(_orders(TRADE_DATE), TRADE_DATE, cfg)
    state = LiveStateStore(cfg.state_db)
    state.save_batch(batch)
    if with_preflight:
        state.record_preflight(batch.batch_sha256, {
            "status": "PASSED", "trade_date": TRADE_DATE,
            "batch_sha256": batch.batch_sha256, "account_alias": cfg.account_alias,
            "account_fingerprint": cfg.expected_account_sha256,
            "risk": {}, "reconciliation": {},
        })
    return cfg, batch, state


def _no_external(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("this path must not construct a broker or HTTP client")

    monkeypatch.setattr(cli, "_gateway", forbidden)
    monkeypatch.setattr(cli, "LiveServerClient", forbidden)


def _clock(monkeypatch, observed_at):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed_at.astimezone(tz) if tz else observed_at

    monkeypatch.setattr(cli, "datetime", Clock)


@pytest.mark.parametrize("instant,expected", [
    ("2026-09-09T06:59:59+00:00", False),
    ("2026-09-09T07:00:00+00:00", True),
    ("2026-09-09T07:00:01+00:00", True),
    ("2026-09-09T14:59:59+08:00", False),
    ("2026-09-09T15:00:00+08:00", True),
    ("2026-09-10T00:00:00+08:00", True),
])
def test_policy_window_elapsed_uses_china_time_not_host_local_time(monkeypatch, instant, expected):
    _clock(monkeypatch, datetime.fromisoformat(instant))
    assert cli._policy_window_elapsed(TRADE_DATE) is expected


def test_live_submit_after_policy_deadline_seals_without_qmt_or_http(tmp_path, monkeypatch):
    cfg, _, state = _state(tmp_path)
    _no_external(monkeypatch)
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: True)
    result = cli.submit(cfg, TRADE_DATE, None)
    assert result == {"status": "EXECUTION_WINDOW_CLOSED", "trade_date": TRADE_DATE}
    receipt = state.workflow_receipt("policy-expiry-window", TRADE_DATE)
    assert receipt["payload"]["expiration_policy_id"] == "QMT_DAY_ORDER_1500_V1"
    assert receipt["payload"]["execution_deadline_at"] == "2026-09-09T15:00:00+08:00"


def test_clock_rollback_cannot_reopen_persisted_policy_window(tmp_path, monkeypatch):
    cfg, _, state = _state(tmp_path)
    _no_external(monkeypatch)
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: True)
    cli.submit(cfg, TRADE_DATE, None)
    original = state.workflow_receipt("policy-expiry-window", TRADE_DATE)
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: False)
    assert cli.submit(cfg, TRADE_DATE, None)["status"] == "EXECUTION_WINDOW_CLOSED"
    assert state.workflow_receipt("policy-expiry-window", TRADE_DATE) == original


def test_elapsed_submit_without_a_local_batch_returns_closed_without_dependencies(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mode="live")
    state = LiveStateStore(cfg.state_db)
    _no_external(monkeypatch)
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: True)
    assert cli.submit(cfg, TRADE_DATE, None)["status"] == "EXECUTION_WINDOW_CLOSED"
    assert state.has_batch(TRADE_DATE) is False
    assert state.workflow_receipt("policy-expiry-window", TRADE_DATE) is not None


@pytest.mark.parametrize("broker_unavailable", [False, True])
def test_settle_seals_before_any_external_dependency_even_when_api_fails(
    tmp_path, monkeypatch, broker_unavailable,
):
    cfg, batch, state = _state(tmp_path)
    submitted, deferred = batch.orders
    for order in batch.orders:
        state.prepare_submission(order["order_id"], batch.batch_sha256, live_order_remark(order))
    state.claim_submission(submitted["order_id"])
    state.complete_submission(submitted["order_id"], "17", "SUBMITTED")
    state.defer_for_cash(deferred["order_id"], {"ready": False})
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: True)
    calls = []

    def assert_sealed():
        assert state.workflow_receipt("policy-expiry-window", TRADE_DATE) is not None
        assert state.submission(deferred["order_id"])["submit_status"] == "NOT_SUBMITTED"

    class Gateway:
        def connect(self):
            assert_sealed()
            calls.append("qmt-connect")
            if broker_unavailable:
                raise RuntimeError("QMT unavailable")

        def settlement_observations(self, _rows):
            assert_sealed()
            calls.append("qmt-observe")
            return {
                "results": [{
                    "order_id": submitted["order_id"], "status": "EXPIRED_BY_POLICY",
                    "filled_quantity": 0, "filled_price": 0.0,
                    "symbol": submitted["symbol"], "direction": submitted["direction"],
                    "qmt_order_id": "17", "raw_qmt_status": 50,
                    "status_observed_at": "2026-09-09T15:00:00+08:00",
                    "expiration_policy_id": "QMT_DAY_ORDER_1500_V1",
                }],
                "pending_order_ids": [], "issues": [],
            }

        def close(self):
            assert_sealed()
            calls.append("qmt-close")

    def gateway_factory(*_args):
        assert_sealed()
        calls.append("qmt-construct")
        return Gateway()

    class FailedServer:
        def __init__(self, *_args, **_kwargs):
            assert_sealed()
            calls.append("http-construct")

        def push_trade_results(self, _date, rows):
            assert_sealed()
            assert any(row["status"] == "NOT_SUBMITTED" for row in rows)
            assert next(row for row in rows if row["status"] == "NOT_SUBMITTED")[
                "not_submitted_reason"
            ] == "EXECUTION_WINDOW_EXPIRED"
            calls.append("http-call")
            raise RuntimeError("HTTP unavailable")

    monkeypatch.setattr(cli, "_gateway", gateway_factory)
    monkeypatch.setattr(cli, "LiveServerClient", FailedServer)
    with pytest.raises(RuntimeError, match="HTTP unavailable"):
        cli.settle(cfg, TRADE_DATE, None)
    assert calls[0] == "qmt-construct"
    assert calls[-1] == "http-call"
    assert_sealed()


@pytest.mark.parametrize("first", ["explicit", "policy"])
def test_explicit_close_intent_and_policy_seal_are_independent_receipts(tmp_path, monkeypatch, first):
    cfg, _, state = _state(tmp_path)
    _clock(monkeypatch, datetime(2026, 9, 9, 15, 1, tzinfo=CHINA))
    server_calls = []

    class Server:
        def __init__(self, *_args, **_kwargs):
            pass

        def close_attempt(self, payload):
            server_calls.append(payload)
            return {
                "status": "CLOSED_PENDING_BROKER", "attempt_id": "ha_test",
                "target_id": "ht_test", "rebalance_id": "hr_test", "execution_domain": "live",
            }

    monkeypatch.setattr(cli, "LiveServerClient", Server)
    monkeypatch.setattr(cli, "_gateway", lambda *_: pytest.fail("no QMT for workflow close"))

    def policy_seal():
        with account_submission_lock(cfg.userdata_dir, cfg.expected_account_sha256):
            assert cli._seal_policy_window(cfg, state, TRADE_DATE) is True

    if first == "policy":
        policy_seal()
    explicit = cli.close_execution_window(cfg, TRADE_DATE, "2026-09-09T15:00:00+08:00")
    policy_seal()
    assert state.workflow_receipt("close-window-intent", TRADE_DATE) is not None
    assert state.workflow_receipt("policy-expiry-window", TRADE_DATE) is not None
    assert cli.close_execution_window(cfg, TRADE_DATE, "2026-09-09T15:00:00+08:00") == explicit
    assert len(server_calls) == 1


def test_cutoff_between_prepare_and_broker_call_records_definitely_unsent_order(tmp_path, monkeypatch):
    cfg, batch, state = _state(tmp_path, with_preflight=True)
    clocks = iter((False, True))
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: next(clocks))

    class Gateway:
        def connect(self):
            pass

        def close(self):
            pass

        def account_snapshot(self):
            return AccountSnapshot(cfg.account_id, 10000, 10000, {}, {})

        def find_existing_submission(self, _order):
            return None

        def submit(self, _order):
            pytest.fail("the cutoff was crossed before the broker call")

    monkeypatch.setattr(cli, "_gateway", lambda *_: Gateway())
    monkeypatch.setattr(cli, "LiveServerClient", lambda *_args, **_kwargs: pytest.fail("offline submit"))
    assert cli.submit(cfg, TRADE_DATE, None)["status"] == "EXECUTION_WINDOW_CLOSED"
    assert state.submission(batch.orders[0]["order_id"])["submit_status"] == "NOT_SUBMITTED"


def test_unstarted_frozen_batch_can_be_reported_as_never_submitted_after_seal(tmp_path, monkeypatch):
    cfg, batch, state = _state(tmp_path)
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: True)
    monkeypatch.setattr(cli, "_gateway", lambda *_: pytest.fail("unsent batch requires no QMT"))
    received = []

    class Server:
        def __init__(self, *_args, **_kwargs):
            pass

        def push_trade_results(self, _date, rows):
            received.extend(rows)
            return {"unmatched_order_ids": []}

    monkeypatch.setattr(cli, "LiveServerClient", Server)
    assert cli.submit(cfg, TRADE_DATE, None)["status"] == "EXECUTION_WINDOW_CLOSED"
    cli.settle(cfg, TRADE_DATE, None)
    assert {row["order_id"] for row in received} == {row["order_id"] for row in batch.orders}
    assert all(row["status"] == "NOT_SUBMITTED" and row["filled_quantity"] == 0 for row in received)
    assert all(row["submit_status"] == "NOT_SUBMITTED" for row in state.submissions_for_date(TRADE_DATE))


def test_marker_replay_finishes_interrupted_local_order_materialization(tmp_path, monkeypatch):
    cfg, batch, state = _state(tmp_path)
    _no_external(monkeypatch)
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: True)
    original = state_module.LiveStateStore.prepare_submission

    def crash_after_first_row(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("crash after durable first row")

    monkeypatch.setattr(state_module.LiveStateStore, "prepare_submission", crash_after_first_row)
    with pytest.raises(RuntimeError, match="crash after durable first row"):
        cli.submit(cfg, TRADE_DATE, None)
    assert state.workflow_receipt("policy-expiry-window", TRADE_DATE) is not None
    assert state.submission(batch.orders[0]["order_id"])["submit_status"] == "PREPARED"
    monkeypatch.setattr(state_module.LiveStateStore, "prepare_submission", original)
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: False)
    assert cli.submit(cfg, TRADE_DATE, None)["status"] == "EXECUTION_WINDOW_CLOSED"
    rows = state.submissions_for_date(TRADE_DATE)
    assert len(rows) == len(batch.orders)
    assert all(row["submit_status"] == "NOT_SUBMITTED" for row in rows)


def test_mid_batch_cutoff_preserves_first_submission_and_closes_only_unsent_remainder(tmp_path, monkeypatch):
    cfg, batch, state = _state(tmp_path, with_preflight=True)
    clocks = iter((False, False, True))
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: next(clocks))
    called = []

    class Gateway:
        def connect(self):
            pass

        def close(self):
            pass

        def account_snapshot(self):
            return AccountSnapshot(cfg.account_id, 10000, 10000, {}, {})

        def find_existing_submission(self, _order):
            return None

        def submit(self, order):
            called.append(order["order_id"])
            return SubmissionResult("17", "SUBMITTED")

    monkeypatch.setattr(cli, "_gateway", lambda *_: Gateway())
    monkeypatch.setattr(cli, "LiveServerClient", lambda *_args, **_kwargs: pytest.fail("offline submit"))
    result = cli.submit(cfg, TRADE_DATE, None)
    assert result["status"] == "EXECUTION_WINDOW_CLOSED"
    assert result["submitted_now"] == 1
    assert called == [batch.orders[0]["order_id"]]
    assert state.submission(batch.orders[0]["order_id"])["submit_status"] == "SUBMITTED"
    assert state.submission(batch.orders[1]["order_id"])["submit_status"] == "NOT_SUBMITTED"


@pytest.mark.parametrize("status", ["SUBMITTING_UNKNOWN", "SUBMITTED"])
def test_policy_seal_does_not_relabel_a_possible_broker_call_as_never_submitted(
    tmp_path, monkeypatch, status,
):
    cfg, batch, state = _state(tmp_path)
    order = batch.orders[0]
    state.prepare_submission(order["order_id"], batch.batch_sha256, live_order_remark(order))
    state.claim_submission(order["order_id"])
    if status == "SUBMITTED":
        state.complete_submission(order["order_id"], "17", "SUBMITTED")
    _no_external(monkeypatch)
    monkeypatch.setattr(cli, "_policy_window_elapsed", lambda _: True)
    assert cli.submit(cfg, TRADE_DATE, None)["status"] == "EXECUTION_WINDOW_CLOSED"
    assert state.submission(order["order_id"])["submit_status"] == status
