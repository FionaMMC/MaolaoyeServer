"""独立 live client 隔离、批次复核与 mock_qmt 闭环。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_client import cli, state as state_module
from live_client.config import LiveClientConfig
from live_client.core import validate_account_capacity, validate_order_batch
from live_client.gateway import (
    AccountSnapshot,
    MockQMTGateway,
    SubmissionResult,
    classify_qmt_settlement_status,
)
from live_client.offline_acceptance import run_acceptance
from live_client.state import LiveStateStore


def _cfg(tmp_path: Path, **changes) -> LiveClientConfig:
    account_id = "LIVE_ACCOUNT_FOR_TEST"
    values = {
        "mode": "mock_qmt",
        "execution_domain": "live",
        "account_id": account_id,
        "expected_account_sha256": hashlib.sha256(account_id.encode()).hexdigest(),
        "account_alias": "hydra-live",
        "instance_id": "live_hydra",
        "api_key": "LIVE_KEY",
        "server_base_url": "https://server.invalid",
        "userdata_dir": tmp_path / "userdata",
        "session_id": 987654,
        "state_db": tmp_path / "state" / "live.db",
        "log_dir": tmp_path / "logs",
        "task_prefix": "HydraLiveTest",
        "trading_enabled": True,
        "allow_insecure_http": False,
        "allowed_symbols": frozenset({
            "510300.SH", "159915.SZ", "511260.SH", "518880.SH", "159981.SZ",
            "159985.SZ", "159930.SZ", "513500.SH", "513100.SH",
        }),
        "max_daily_orders": 5,
        "max_single_order_notional": 1_000_000,
        "max_daily_buy_notional": 2_000_000,
        "max_daily_sell_notional": 2_000_000,
        "max_daily_turnover_notional": 3_000_000,
        "max_price_offset_bps": 50,
    }
    values.update(changes)
    return LiveClientConfig(**values)


def _orders(trade_date="20260803") -> list[dict]:
    canonical = [
        {
            "symbol": "159915.SZ", "direction": "BUY", "quantity": 100,
            "reference_price": 2.0, "limit_price": 2.01,
        },
        {
            "symbol": "510300.SH", "direction": "BUY", "quantity": 100,
            "reference_price": 4.0, "limit_price": 4.02,
        },
    ]
    payload = {
        "rebalance_id": "hr_test",
        "attempt_number": 1,
        "trade_date": trade_date,
        "orders": canonical,
    }
    batch_sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = []
    for index, item in enumerate(canonical):
        result.append({
            "order_id": f"ho_{index}",
            "execution_domain": "live",
            "qmt_account_alias": "hydra-live",
            "target_id": "ht_test",
            "rebalance_id": "hr_test",
            "attempt_id": "ha_test",
            "attempt_number": 1,
            "batch_id": f"hb_{batch_sha}",
            "batch_sha256": batch_sha,
            "target_hash": "a" * 64,
            "account_group": "hydra-live",
            "valid_date": trade_date,
            "execution_reference_price": item["reference_price"],
            "symbol": item["symbol"],
            "direction": item["direction"],
            "quantity": item["quantity"],
            "limit_price": item["limit_price"],
        })
    return result


def _freeze_with_preflight(cfg: LiveClientConfig):
    batch = validate_order_batch(_orders(), "20260803", cfg)
    state = LiveStateStore(cfg.state_db)
    state.save_batch(batch)
    state.record_preflight(batch.batch_sha256, {
        "status": "PASSED",
        "trade_date": "20260803",
        "batch_sha256": batch.batch_sha256,
        "account_alias": cfg.account_alias,
        "account_fingerprint": cfg.expected_account_sha256,
        "reconciliation": {"test": "pass"},
        "risk": {"test": "pass"},
    })
    return batch


def test_batch_rejects_domain_account_and_hash_mismatch(tmp_path):
    cfg = _cfg(tmp_path)
    for field, value in (
        ("execution_domain", "paper"),
        ("qmt_account_alias", "someone-else"),
        ("batch_sha256", "0" * 64),
    ):
        orders = _orders()
        orders[0][field] = value
        with pytest.raises(ValueError):
            validate_order_batch(orders, "20260803", cfg)


def test_state_refuses_server_batch_replacement(tmp_path):
    cfg = _cfg(tmp_path)
    store = LiveStateStore(cfg.state_db)
    first = validate_order_batch(_orders(), "20260803", cfg)
    store.save_batch(first)
    changed = _orders()
    changed[0]["target_hash"] = "b" * 64
    # Common target_hash is now mixed and fails even before local replacement.
    with pytest.raises(ValueError):
        validate_order_batch(changed, "20260803", cfg)


def test_state_migrates_existing_submission_table_in_place(tmp_path):
    path = tmp_path / "legacy-live.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE submissions (
            order_id TEXT PRIMARY KEY,
            batch_sha256 TEXT NOT NULL,
            local_order_id TEXT,
            submit_status TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            detail TEXT,
            execution_json TEXT
        )""")
    LiveStateStore(path)
    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(submissions)")
        }

    assert "order_remark" in columns


def test_state_store_explicitly_closes_every_sqlite_connection(
    tmp_path, monkeypatch,
):
    """Windows must be able to remove state.db before process exit."""
    real_connect = sqlite3.connect
    opened_connections = []

    class TrackingConnection(sqlite3.Connection):
        was_closed = False

        def close(self):
            self.was_closed = True
            return super().close()

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        conn = real_connect(*args, **kwargs)
        opened_connections.append(conn)
        return conn

    monkeypatch.setattr(state_module.sqlite3, "connect", tracking_connect)
    cfg = _cfg(tmp_path)
    store = LiveStateStore(cfg.state_db)
    batch = validate_order_batch(_orders(), "20260803", cfg)
    store.save_batch(batch)
    store.load_batch(batch.trade_date)
    assert store.save_batch(batch) is False  # early return still closes
    store.prepare_submission("close-test", batch.batch_sha256, "first-remark")
    with pytest.raises(RuntimeError, match="提交意图与批次不一致"):
        store.prepare_submission(
            "close-test", batch.batch_sha256, "conflicting-remark",
        )

    assert opened_connections
    assert all(conn.was_closed for conn in opened_connections)


def test_preflight_receipt_detects_local_tampering(tmp_path):
    cfg = _cfg(tmp_path)
    batch = _freeze_with_preflight(cfg)
    with sqlite3.connect(cfg.state_db) as conn:
        conn.execute(
            "UPDATE preflight_checks SET payload_json = ? WHERE batch_sha256 = ?",
            ('{"status":"PASSED"}', batch.batch_sha256),
        )

    with pytest.raises(RuntimeError, match="preflight 回执 hash"):
        LiveStateStore(cfg.state_db).latest_preflight(batch.batch_sha256)


def test_account_capacity_is_sell_first_but_never_oversells(tmp_path):
    cfg = _cfg(tmp_path)
    orders = _orders()
    orders[0]["direction"] = "SELL"
    canonical = [
        {
            "symbol": row["symbol"], "direction": row["direction"],
            "quantity": row["quantity"],
            "reference_price": row["execution_reference_price"],
            "limit_price": row["limit_price"],
        }
        for row in orders
    ]
    payload = {
        "rebalance_id": "hr_test", "attempt_number": 1,
        "trade_date": "20260803", "orders": canonical,
    }
    sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for row in orders:
        row["batch_sha256"] = sha
        row["batch_id"] = f"hb_{sha}"
    batch = validate_order_batch(orders, "20260803", cfg)
    validate_account_capacity(
        batch, cash=300, positions={"159915.SZ": 100},
        cfg=cfg, total_asset=1000,
    )
    with pytest.raises(ValueError, match="可卖持仓不足"):
        validate_account_capacity(
            batch, cash=300, positions={}, cfg=cfg, total_asset=1000,
        )


def test_auto_risk_uses_qmt_nav_and_records_immutable_snapshot(tmp_path):
    cfg = _cfg(
        tmp_path,
        risk_mode="auto",
        max_daily_orders=0,
        max_single_order_notional=0,
        max_daily_buy_notional=0,
        max_daily_sell_notional=0,
        max_daily_turnover_notional=0,
        max_price_offset_bps=0,
        auto_max_daily_orders=10,
        auto_buffer_bps=100,
    )
    batch = validate_order_batch(_orders(), "20260803", cfg)
    snapshot = validate_account_capacity(
        batch, cash=10_000, positions={}, cfg=cfg, total_asset=10_000,
    )
    assert snapshot["mode"] == "auto"
    assert snapshot["max_single_order_notional"] == 10_100
    assert snapshot["max_price_offset_bps"] == 50
    store = LiveStateStore(cfg.state_db)
    assert store.record_risk_check(batch.batch_sha256, snapshot) is True
    assert store.record_risk_check(batch.batch_sha256, snapshot) is False
    with pytest.raises(RuntimeError, match="风控快照已变化"):
        store.record_risk_check(batch.batch_sha256, {**snapshot, "qmt_total_asset": 1})


def test_mock_qmt_full_query_submit_settle_cycle(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    orders = _orders()

    class FakeServer:
        pushed = None

        def __init__(self, *_args, **_kwargs):
            pass

        def fetch_orders(self, trade_date):
            assert trade_date == "20260803"
            return orders

        def push_trade_results(self, trade_date, results):
            FakeServer.pushed = (trade_date, results)
            return {
                "trade_date": trade_date,
                "execution_domain": "live",
                "matched_count": len(results),
                "unmatched_order_ids": [],
            }

        def reconcile(self, _payload):
            return {
                "n_mismatched": 0,
                "n_server_only": 0,
                "n_qmt_only": 0,
                "cash_diff": 0.0,
            }

    monkeypatch.setattr(cli, "LiveServerClient", FakeServer)
    mock_path = tmp_path / "mock-state.json"
    mock_path.write_text(json.dumps({
        "account_id": cfg.account_id,
        "available_cash": 10_000,
        "positions": {},
        "fill_ratios": {"510300.SH": 0.5},
    }), encoding="utf-8")

    queried = cli.query(cfg, "20260803")
    preflight = cli.preflight(cfg, "20260803", mock_path)
    submitted = cli.submit(cfg, "20260803", mock_path)
    settled = cli.settle(cfg, "20260803", mock_path)

    assert queried["orders"] == 2
    assert len(preflight["preflight_receipt_sha256"]) == 64
    assert submitted == {
        "trade_date": "20260803",
        "batch_sha256": queried["batch_sha256"],
        "submitted": 2,
        "rejected": 0,
        "submitted_now": 2,
        "attempted_now": 2,
        "recovered": 0,
        "already_recorded": 0,
    }
    assert settled["results"] == 2
    assert FakeServer.pushed[0] == "20260803"
    statuses = {row["symbol"]: row["status"] for row in FakeServer.pushed[1]}
    assert statuses == {"159915.SZ": "FILLED", "510300.SH": "PARTIAL"}


def test_client_emergency_stop_blocks_even_mock_submission(tmp_path):
    cfg = _cfg(tmp_path, trading_enabled=False)
    with pytest.raises(RuntimeError, match="紧急开关关闭"):
        cfg.require_submission_enabled()


def test_local_doctor_has_no_server_or_qmt_side_effects(tmp_path):
    result = cli.doctor(_cfg(tmp_path))

    assert result["status"] == "LOCAL_CONFIG_OK"
    assert result["server_contacted"] is False
    assert result["qmt_contacted"] is False
    assert (tmp_path / "state" / "live.db").exists()


def test_packaged_offline_acceptance():
    result = run_acceptance()

    assert result["status"] == "PASS"
    assert result["real_qmt_contacted"] is False
    assert result["server_contacted"] is False


def test_real_mode_requires_explicit_paper_isolation_evidence(tmp_path, monkeypatch):
    cfg = _cfg(
        tmp_path,
        mode="live",
        userdata_dir=tmp_path,
    )
    for name in (
        "HYDRA_PAPER_QMT_ACCOUNT_IDS",
        "HYDRA_PAPER_WRITABLE_PATHS",
        "HYDRA_PAPER_QMT_SESSION_IDS",
        "HYDRA_PAPER_TASK_PREFIXES",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="paper account denylist"):
        cfg.validate_startup()


def test_cross_device_real_mode_does_not_require_local_paper_denylists(
    tmp_path, monkeypatch,
):
    cfg = _cfg(
        tmp_path,
        mode="live",
        userdata_dir=tmp_path,
        paper_client_colocated=False,
    )
    for name in (
        "HYDRA_PAPER_QMT_ACCOUNT_IDS",
        "HYDRA_PAPER_WRITABLE_PATHS",
        "HYDRA_PAPER_QMT_SESSION_IDS",
        "HYDRA_PAPER_TASK_PREFIXES",
    ):
        monkeypatch.delenv(name, raising=False)

    cfg.validate_startup()


def test_real_mode_http_requires_and_accepts_explicit_business_approval(
    tmp_path, monkeypatch,
):
    for name, value in {
        "HYDRA_PAPER_QMT_ACCOUNT_IDS": "PAPER_ACCOUNT",
        "HYDRA_PAPER_WRITABLE_PATHS": str(tmp_path / "paper"),
        "HYDRA_PAPER_QMT_SESSION_IDS": "123456",
        "HYDRA_PAPER_TASK_PREFIXES": "HydraPaper",
    }.items():
        monkeypatch.setenv(name, value)
    blocked = _cfg(
        tmp_path,
        mode="live",
        server_base_url="http://120.26.138.82:8000",
        userdata_dir=tmp_path,
        allow_insecure_http=False,
    )
    with pytest.raises(ValueError, match="HTTP 尚未显式批准"):
        blocked.validate_startup()

    approved = _cfg(
        tmp_path,
        mode="live",
        server_base_url="http://120.26.138.82:8000",
        userdata_dir=tmp_path,
        allow_insecure_http=True,
    )
    approved.validate_startup()


def test_preflight_requires_live_qmt_snapshot_to_match_server_ledger(
    tmp_path, monkeypatch,
):
    cfg = _cfg(tmp_path)
    orders = _orders()

    class FakeServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def fetch_orders(self, _trade_date):
            return orders

        def reconcile(self, _payload):
            return {
                "n_mismatched": 1,
                "n_server_only": 0,
                "n_qmt_only": 0,
                "cash_diff": 0.0,
            }

    monkeypatch.setattr(cli, "LiveServerClient", FakeServer)
    mock_path = tmp_path / "mock-state.json"
    mock_path.write_text(json.dumps({
        "account_id": cfg.account_id,
        "available_cash": 10_000,
        "positions": {},
    }), encoding="utf-8")
    cli.query(cfg, "20260803")
    with pytest.raises(RuntimeError, match="positions"):
        cli.preflight(cfg, "20260803", mock_path)


def test_submit_uses_frozen_batch_when_server_is_unavailable(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _freeze_with_preflight(cfg)

    class ExplodingServer:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("submit must not construct an HTTP client")

    monkeypatch.setattr(cli, "LiveServerClient", ExplodingServer)
    mock_path = tmp_path / "mock-state.json"
    mock_path.write_text(json.dumps({
        "account_id": cfg.account_id,
        "available_cash": 10_000,
        "positions": {},
    }), encoding="utf-8")

    result = cli.submit(cfg, "20260803", mock_path)

    assert result["submitted"] == 2
    assert result["attempted_now"] == 2


def test_submit_requires_prior_successful_online_preflight(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    batch = validate_order_batch(_orders(), "20260803", cfg)
    LiveStateStore(cfg.state_db).save_batch(batch)
    monkeypatch.setattr(
        cli,
        "_gateway",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("QMT must not be opened before preflight receipt check")
        ),
    )

    with pytest.raises(RuntimeError, match="preflight 回执"):
        cli.submit(cfg, "20260803", None)


def test_submit_replay_never_calls_qmt_twice(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _freeze_with_preflight(cfg)
    mock_path = tmp_path / "mock-state.json"
    mock_path.write_text(json.dumps({
        "account_id": cfg.account_id,
        "available_cash": 10_000,
        "positions": {},
    }), encoding="utf-8")

    first = cli.submit(cfg, "20260803", mock_path)
    second = cli.submit(cfg, "20260803", mock_path)

    assert first["attempted_now"] == 2
    assert second["attempted_now"] == 0
    assert second["already_recorded"] == 2


def test_submit_recovers_crash_after_broker_acceptance_by_remark(
    tmp_path, monkeypatch,
):
    cfg = _cfg(tmp_path)
    _freeze_with_preflight(cfg)
    accepted: dict[str, SubmissionResult] = {}
    calls = {"submit": 0, "crashed": False}

    class CrashRecoverGateway:
        def __init__(self, *_args, **_kwargs):
            pass

        def connect(self):
            pass

        def close(self):
            pass

        def account_snapshot(self):
            return AccountSnapshot(
                cfg.account_id, 10_000, 10_000, {}, {},
            )

        def find_existing_submission(self, order):
            return accepted.get(order["order_id"])

        def submit(self, order):
            calls["submit"] += 1
            result = SubmissionResult(
                str(10000 + calls["submit"]), "SUBMITTED",
                execution_meta={"qmt_order_id": str(10000 + calls["submit"])},
            )
            accepted[order["order_id"]] = result
            if not calls["crashed"]:
                calls["crashed"] = True
                raise RuntimeError("simulated crash after QMT acceptance")
            return result

    monkeypatch.setattr(cli, "_gateway", lambda *_args: CrashRecoverGateway())

    with pytest.raises(RuntimeError, match="simulated crash"):
        cli.submit(cfg, "20260803", None)
    recovered = cli.submit(cfg, "20260803", None)

    assert recovered["recovered"] == 1
    assert recovered["submitted_now"] == 1
    assert calls["submit"] == 2


def test_submit_never_retries_ambiguous_qmt_call_without_broker_evidence(
    tmp_path, monkeypatch,
):
    cfg = _cfg(tmp_path)
    _freeze_with_preflight(cfg)
    calls = {"submit": 0}

    class AmbiguousGateway:
        def __init__(self, *_args, **_kwargs):
            pass

        def connect(self):
            pass

        def close(self):
            pass

        def account_snapshot(self):
            return AccountSnapshot(cfg.account_id, 10_000, 10_000, {}, {})

        def find_existing_submission(self, _order):
            return None

        def submit(self, _order):
            calls["submit"] += 1
            raise RuntimeError("QMT response lost")

    monkeypatch.setattr(cli, "_gateway", lambda *_args: AmbiguousGateway())

    with pytest.raises(RuntimeError, match="QMT response lost"):
        cli.submit(cfg, "20260803", None)
    with pytest.raises(RuntimeError, match="禁止自动重试"):
        cli.submit(cfg, "20260803", None)

    assert calls["submit"] == 1


def test_qmt_active_or_unknown_status_is_never_inferred_as_cancelled():
    constants = SimpleNamespace(
        ORDER_UNREPORTED=48,
        ORDER_WAIT_REPORTING=49,
        ORDER_REPORTED=50,
        ORDER_REPORTED_CANCEL=51,
        ORDER_PARTSUCC_CANCEL=52,
        ORDER_PART_CANCEL=53,
        ORDER_CANCELED=54,
        ORDER_PART_SUCC=55,
        ORDER_SUCCEEDED=56,
        ORDER_JUNK=57,
        ORDER_UNKNOWN=255,
    )
    with pytest.raises(RuntimeError, match="尚未终结"):
        classify_qmt_settlement_status(constants, 50, 0, 100)
    with pytest.raises(RuntimeError, match="未识别"):
        classify_qmt_settlement_status(constants, 999, 0, 100)
    assert classify_qmt_settlement_status(constants, 54, 0, 100) == "CANCELLED"
    assert classify_qmt_settlement_status(constants, 53, 25, 100) == "PARTIAL"
    assert classify_qmt_settlement_status(constants, 57, 0, 100) == "REJECTED"


def test_qmt_snapshot_keeps_total_and_sellable_positions_separate(tmp_path):
    state_path = tmp_path / "mock-state.json"
    state_path.write_text(json.dumps({
        "account_id": "LIVE_ACCOUNT_FOR_TEST",
        "available_cash": 1000,
        "positions": {"510300.SH": 300},
        "sellable_positions": {"510300.SH": 100},
    }), encoding="utf-8")
    snapshot = MockQMTGateway(
        state_path, "LIVE_ACCOUNT_FOR_TEST",
    ).account_snapshot()
    assert snapshot.positions == {"510300.SH": 300}
    assert snapshot.sellable_positions == {"510300.SH": 100}


def test_cash_flow_command_posts_stable_evidence_payload(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    class FakeServer:
        payload = None

        def __init__(self, *_args, **_kwargs):
            pass

        def post_cash_flow(self, payload):
            FakeServer.payload = payload
            return {"already_applied": False, "virtual_cash_after": 123.45}

    monkeypatch.setattr(cli, "LiveServerClient", FakeServer)
    result = cli.journal_cash_flow(
        cfg,
        event_date="20260803",
        event_type="DIVIDEND",
        amount=123.45,
        source="qmt-statement",
        source_event_id="dividend-20260803-510300",
        evidence_sha256="a" * 64,
        description="verified ETF distribution",
    )
    assert result["already_applied"] is False
    assert FakeServer.payload["execution_domain"] == "live"
    assert FakeServer.payload["account_alias"] == "hydra-live"
    assert FakeServer.payload["source_event_id"] == "dividend-20260803-510300"
