"""Cash arrival is asynchronous; an accepted sell is not spending power."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from live_client import cli
from live_client.core import validate_order_batch
from live_client.execution_queue import account_submission_lock, cash_readiness
from live_client.gateway import AccountSnapshot, SubmissionResult, XtQMTGateway, live_order_remark
from live_client.state import LiveStateStore
from test_live_client import _cfg, _orders, _qmt_order


def _freeze(cfg, *, rotate=True, cash=0):
    orders = _orders()
    if rotate:
        orders[1]["direction"] = "SELL"
    canonical = [{
        "symbol": row["symbol"], "direction": row["direction"],
        "quantity": row["quantity"], "reference_price": row["execution_reference_price"],
        "limit_price": row["limit_price"],
    } for row in orders]
    digest = hashlib.sha256(json.dumps({
        "rebalance_id": "hr_test", "attempt_number": 1,
        "trade_date": "20260803", "orders": canonical,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    for row in orders:
        row.update(batch_sha256=digest, batch_id=f"hb_{digest}")
    batch = validate_order_batch(orders, "20260803", cfg)
    state = LiveStateStore(cfg.state_db)
    state.save_batch(batch)
    state.record_preflight(digest, {
        "status": "PASSED", "trade_date": "20260803", "batch_sha256": digest,
        "account_alias": cfg.account_alias,
        "account_fingerprint": cfg.expected_account_sha256,
        "reconciliation": {
            "reconciliation_scope": "portfolio_attributed", "managed_cash": cash,
        },
        "risk": {
            "managed_sellable_positions": {"510300.SH": 100} if rotate else {},
            "qmt_total_asset": cash + (402 if rotate else 0),
        },
    })
    return batch, state


class QueueGateway:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cash = 19_000_000.0
        self.filled = 0
        self.reject_sell = False
        self.calls = []
        self.accepted = {}
        self.snapshots = 0

    def connect(self):
        pass

    def close(self):
        pass

    def account_snapshot(self):
        self.snapshots += 1
        return AccountSnapshot(
            self.cfg.account_id, self.cash, 19_000_402,
            {"510300.SH": 100}, {"510300.SH": 100},
        )

    def find_existing_submission(self, order):
        return self.accepted.get(order["order_id"])

    def submit(self, order):
        self.calls.append(order["direction"])
        if self.reject_sell and order["direction"] == "SELL":
            return SubmissionResult(None, "REJECTED")
        result = SubmissionResult(str(len(self.calls)), "SUBMITTED")
        self.accepted[order["order_id"]] = result
        return result

    def confirmed_sell_fills(self, submissions):
        return {row["order_id"]: {
            "filled_quantity": self.filled, "filled_price": 4.02 if self.filled else 0,
        } for row in submissions}

    def settlement_results(self, submissions):
        return [{
            "order_id": row["order_id"], "filled_quantity": self.filled,
            "filled_price": 4.02 if self.filled else 0,
            "symbol": row["symbol"], "direction": row["direction"],
            "status": "FILLED" if self.filled == 100 else "CANCELLED",
        } for row in submissions]


def _setup(tmp_path, monkeypatch, *, rotate=True, cash=0):
    cfg = _cfg(tmp_path, ledger_mode="attributed", initial_allocated_cash=cash)
    batch, state = _freeze(cfg, rotate=rotate, cash=cash)
    gateway = QueueGateway(cfg)
    monkeypatch.setattr(cli, "_gateway", lambda *_: gateway)

    class NoServer:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("offline queue must never access the server")

    monkeypatch.setattr(cli, "LiveServerClient", NoServer)
    return cfg, batch, state, gateway


def test_reported_sell_does_not_borrow_nineteen_million_of_reserve(tmp_path, monkeypatch):
    cfg, _, state, gateway = _setup(tmp_path, monkeypatch)
    result = cli.submit(cfg, "20260803", None)
    assert gateway.calls == ["SELL"]
    assert result["status"] == "WAITING_FOR_CASH"
    assert result["rejected"] == 0
    assert state.submission("ho_0")["submit_status"] == "DEFERRED_CASH"
    assert result["deferred_cash"][0]["owned_remaining_cash"] == 0


def test_confirmed_proceeds_resume_only_unsubmitted_buy(tmp_path, monkeypatch):
    cfg, _, state, gateway = _setup(tmp_path, monkeypatch)
    cli.submit(cfg, "20260803", None)
    gateway.filled = 100
    result = cli.submit(cfg, "20260803", None)
    assert gateway.calls == ["SELL", "BUY"]
    assert result["submitted_now"] == 1
    assert state.submission("ho_0")["submit_status"] == "SUBMITTED"
    assert cli.submit(cfg, "20260803", None)["attempted_now"] == 0


def test_sell_rejection_defers_buy_without_rejecting_or_borrowing(tmp_path, monkeypatch):
    cfg, _, _, gateway = _setup(tmp_path, monkeypatch)
    gateway.reject_sell = True
    result = cli.submit(cfg, "20260803", None)
    assert gateway.calls == ["SELL"]
    assert result["rejected"] == 1
    assert result["status"] == "WAITING_FOR_CASH"


def test_partial_sell_and_delayed_available_cash_are_separate(tmp_path, monkeypatch):
    cfg, _, _, gateway = _setup(tmp_path, monkeypatch)
    gateway.filled = 25
    assert cli.submit(cfg, "20260803", None)["status"] == "WAITING_FOR_CASH"
    gateway.filled = 100
    gateway.cash = 0
    assert cli.submit(cfg, "20260803", None)["status"] == "WAITING_FOR_CASH"
    gateway.cash = 500
    assert cli.submit(cfg, "20260803", None)["submitted_now"] == 1
    assert gateway.calls == ["SELL", "BUY"]


def test_lagging_asset_snapshot_cannot_spend_physical_cash_twice(tmp_path, monkeypatch):
    cfg, _, _, gateway = _setup(tmp_path, monkeypatch, rotate=False, cash=1000)
    gateway.cash = 500
    result = cli.submit(cfg, "20260803", None)
    assert gateway.calls == ["BUY"]
    assert result["status"] == "WAITING_FOR_CASH"
    assert gateway.snapshots == 3  # initial + immediately before each buy
    assert result["deferred_cash"][0]["physical_qmt_available_cash"] == 500
    assert result["deferred_cash"][0]["qmt_available_cash"] < 300


def test_explicit_close_reports_never_submitted_truth_not_broker_rejection(tmp_path, monkeypatch):
    cfg, _, state, gateway = _setup(tmp_path, monkeypatch)
    cli.submit(cfg, "20260803", None)
    captured = []

    class Server:
        def __init__(self, *_args, **_kwargs):
            pass

        def push_trade_results(self, _date, results):
            captured.extend(results)
            return {"matched_count": len(results)}

    monkeypatch.setattr(cli, "LiveServerClient", Server)
    cli.settle(cfg, "20260803", None, close_deferred=True)
    row = next(row for row in captured if row["order_id"] == "ho_0")
    assert row["status"] == "NOT_SUBMITTED"
    assert row["filled_quantity"] == 0
    assert row["not_submitted_reason"] == "INSUFFICIENT_CASH"
    assert "qmt_order_id" not in row
    assert state.submission("ho_0")["submit_status"] == "NOT_SUBMITTED"
    gateway.filled = 100
    assert cli.submit(cfg, "20260803", None)["attempted_now"] == 0
    assert gateway.calls == ["SELL"]


def test_confirmed_sell_observations_must_not_regress_after_restart(tmp_path):
    state = LiveStateStore(tmp_path / "state.db")
    state.observe_sell_fills({"sell": {"filled_quantity": 50, "filled_price": 4.0}})
    state = LiveStateStore(tmp_path / "state.db")
    with pytest.raises(RuntimeError, match="倒退"):
        state.observe_sell_fills({"sell": {"filled_quantity": 25, "filled_price": 4.0}})
    with pytest.raises(RuntimeError, match="倒退"):
        state.observe_sell_fills({"sell": {"filled_quantity": 50, "filled_price": 3.9}})


def test_account_writer_lock_is_shared_across_strategy_state_locations(tmp_path):
    with account_submission_lock(tmp_path, "a" * 64):
        with pytest.raises(RuntimeError, match="已有离线提交进程"):
            with account_submission_lock(tmp_path, "a" * 64):
                pass
        with account_submission_lock(tmp_path, "b" * 64):
            pass
    with account_submission_lock(tmp_path, "a" * 64):
        pass


def test_own_confirmed_proceeds_are_not_counted_twice_for_two_buys():
    sell = {"order_id": "sell", "direction": "SELL", "quantity": 100, "submit_status": "SUBMITTED"}
    buy = {"order_id": "buy", "direction": "BUY", "quantity": 100, "limit_price": 3.0, "submit_status": "SUBMITTED"}
    readiness = cash_readiness(
        buy, initial_owned_cash=0, qmt_available_cash=19_000_000,
        submissions=[sell, buy],
        confirmed_sell_fills={"sell": {"filled_quantity": 100, "filled_price": 4.0}},
    )
    assert readiness["ready"] is False
    assert readiness["owned_remaining_cash"] == 90.0


def test_minimum_commission_is_reserved_on_both_sells_and_buys():
    sell = {"order_id": "sell", "direction": "SELL", "quantity": 100, "submit_status": "SUBMITTED"}
    buy = {"direction": "BUY", "quantity": 100, "limit_price": 10.0}
    readiness = cash_readiness(
        buy, initial_owned_cash=5, qmt_available_cash=19_000_000,
        submissions=[sell],
        confirmed_sell_fills={"sell": {"filled_quantity": 100, "filled_price": 10.0}},
    )
    assert readiness["required_cash"] == 1005.0
    assert readiness["confirmed_sell_proceeds"] == 995.0
    assert readiness["owned_remaining_cash"] == 1000.0
    assert readiness["ready"] is False


def test_queue_fee_policy_is_frozen_against_later_private_config_change(tmp_path, monkeypatch):
    cfg, batch, state, gateway = _setup(tmp_path, monkeypatch)
    cli.submit(cfg, "20260803", None)
    assert state.risk_check(batch.batch_sha256)["execution_cost_policy"] == {
        "execution_cost_reserve_bps": 10.0, "execution_min_commission": 5.0,
    }
    gateway.filled = 50  # gross 201 - minimum 5 = 196; buy needs 201 + 5
    changed_cfg = replace(cfg, execution_min_commission=0, execution_cost_reserve_bps=0)
    result = cli.submit(changed_cfg, "20260803", None)
    assert result["status"] == "WAITING_FOR_CASH"
    assert result["deferred_cash"][0]["required_cash"] == 206.0


def test_close_window_needs_no_qmt_and_does_not_invent_cash(tmp_path, monkeypatch):
    cfg, _, state, _ = _setup(tmp_path, monkeypatch)
    payloads = []

    class Server:
        def __init__(self, *_args, **_kwargs):
            pass

        def close_attempt(self, payload):
            payloads.append(payload)
            return {
                "status": "CLOSED_PENDING_BROKER", "attempt_id": "ha_test",
                "target_id": "ht_test", "rebalance_id": "hr_test",
                "execution_domain": "live",
            }

    def no_qmt(*_args):
        raise AssertionError("close-window must not construct QMT")

    monkeypatch.setattr(cli, "_gateway", no_qmt)
    monkeypatch.setattr(cli, "LiveServerClient", Server)
    # Closing is allowed even after new-order policy has been disabled.
    stopped_cfg = replace(cfg, risk_mode="disabled", trading_enabled=False)
    result = cli.close_execution_window(stopped_cfg, "20260803", "2026-08-03T15:00:00+08:00")
    assert result["status"] == "CLOSED_PENDING_BROKER"
    assert "actual_cash" not in payloads[0]
    assert "actual_positions" not in payloads[0]
    assert payloads[0]["close_mode"] == "execution_deadline"
    assert state.workflow_receipt("close-window", "20260803") is not None
    assert state.workflow_receipt("close", "20260803") is None
    assert cli.submit(cfg, "20260803", None)["status"] == "EXECUTION_WINDOW_CLOSED"
    assert cli.close_execution_window(cfg, "20260803", "2026-08-03T15:00:00+08:00") == result
    assert len(payloads) == 1


def test_close_window_server_failure_still_stops_new_local_submissions(tmp_path, monkeypatch):
    cfg, _, state, gateway = _setup(tmp_path, monkeypatch)

    class FailingServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def close_attempt(self, _payload):
            raise RuntimeError("server unavailable")

    monkeypatch.setattr(cli, "LiveServerClient", FailingServer)
    with pytest.raises(RuntimeError, match="server unavailable"):
        cli.close_execution_window(cfg, "20260803", "2026-08-03T15:00:00+08:00")
    assert state.workflow_receipt("close-window-intent", "20260803") is not None
    assert cli.submit(cfg, "20260803", None)["status"] == "EXECUTION_WINDOW_CLOSED"
    assert gateway.calls == []


def test_close_window_requires_zoned_time_and_exact_response_identity(tmp_path, monkeypatch):
    cfg, _, state, _ = _setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="带时区"):
        cli.close_execution_window(cfg, "20260803", "2026-08-03T15:00:00")
    with pytest.raises(ValueError, match="中国交易日"):
        cli.close_execution_window(cfg, "20260803", "2026-08-02T15:00:00+08:00")
    assert state.workflow_receipt("close-window-intent", "20260803") is None

    class WrongServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def close_attempt(self, _payload):
            return {"status": "CLOSED_PENDING_BROKER", "attempt_id": "different"}

    monkeypatch.setattr(cli, "LiveServerClient", WrongServer)
    with pytest.raises(RuntimeError, match="不一致"):
        cli.close_execution_window(cfg, "20260803", "2026-08-03T15:00:00+08:00")
    assert state.workflow_receipt("close-window", "20260803") is None


def test_active_partial_fill_can_fund_buy_only_after_broker_identity_match(tmp_path):
    cfg = _cfg(tmp_path)
    gateway = XtQMTGateway(cfg)
    gateway.xtconstant = SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24)
    order = {**_orders()[1], "direction": "SELL", "local_order_id": "17"}
    order["order_remark"] = live_order_remark(order)
    broker = _qmt_order(order_id=17, status=55, remark=order["order_remark"], traded_volume=25)
    broker.order_type = 24
    gateway._query_orders_for_settlement = lambda: [broker]
    result = gateway.confirmed_sell_fills([order])
    assert result[order["order_id"]]["filled_quantity"] == 25
    broker.strategy_name = "other_strategy"
    with pytest.raises(RuntimeError, match="不一致"):
        gateway.confirmed_sell_fills([order])
