"""Destructive-I/O-free acceptance for Hydra's local-only submit path."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from live_client import cli
from live_client.config import HYDRA_LIVE_EXECUTABLE_SYMBOLS, LiveClientConfig
from live_client.core import validate_order_batch
from live_client.gateway import AccountSnapshot, MockQMTGateway, SubmissionResult
from live_client.state import LiveStateStore

TRADE_DATE = "20990105"


def _config(root: Path, scenario: str) -> LiveClientConfig:
    account_id = "OFFLINE_ACCEPTANCE_ACCOUNT"
    return LiveClientConfig(
        mode="mock_qmt",
        execution_domain="live",
        account_id=account_id,
        expected_account_sha256=hashlib.sha256(account_id.encode()).hexdigest(),
        account_alias="hydra-live-acceptance",
        instance_id="hydra-offline-acceptance",
        api_key="NOT_USED_OFFLINE",
        server_base_url="https://server.invalid",
        userdata_dir=root / "never-opened-userdata",
        session_id=987654321,
        state_db=root / scenario / "state.db",
        log_dir=root / scenario / "logs",
        task_prefix="HydraOfflineAcceptance",
        trading_enabled=True,
        allow_insecure_http=False,
        allowed_symbols=HYDRA_LIVE_EXECUTABLE_SYMBOLS,
        max_daily_orders=10,
        max_single_order_notional=100_000,
        max_daily_buy_notional=100_000,
        max_daily_sell_notional=100_000,
        max_daily_turnover_notional=200_000,
        max_price_offset_bps=50,
    )


def _orders(cfg: LiveClientConfig) -> list[dict]:
    canonical = [
        {
            "symbol": "159915.SZ",
            "direction": "BUY",
            "quantity": 100,
            "reference_price": 2.0,
            "limit_price": 2.001,
        },
        {
            "symbol": "510300.SH",
            "direction": "BUY",
            "quantity": 100,
            "reference_price": 4.0,
            "limit_price": 4.001,
        },
    ]
    payload = {
        "rebalance_id": "hr_offline_acceptance",
        "attempt_number": 1,
        "trade_date": TRADE_DATE,
        "orders": canonical,
    }
    batch_sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return [
        {
            "order_id": f"ho_offline_acceptance_{index}",
            "execution_domain": "live",
            "qmt_account_alias": cfg.account_alias,
            "target_id": "ht_offline_acceptance",
            "rebalance_id": payload["rebalance_id"],
            "attempt_id": "ha_offline_acceptance",
            "attempt_number": 1,
            "batch_id": f"hb_{batch_sha}",
            "batch_sha256": batch_sha,
            "target_hash": "a" * 64,
            "account_group": cfg.account_alias,
            "valid_date": TRADE_DATE,
            "execution_reference_price": item["reference_price"],
            **item,
        }
        for index, item in enumerate(canonical)
    ]


def _freeze(cfg: LiveClientConfig) -> None:
    batch = validate_order_batch(_orders(cfg), TRADE_DATE, cfg)
    state = LiveStateStore(cfg.state_db)
    state.save_batch(batch)
    state.record_preflight(batch.batch_sha256, {
        "status": "PASSED",
        "trade_date": TRADE_DATE,
        "batch_sha256": batch.batch_sha256,
        "account_alias": cfg.account_alias,
        "account_fingerprint": cfg.expected_account_sha256,
        "reconciliation": {"synthetic_acceptance": True},
        "risk": {"synthetic_acceptance": True},
    })


def _mock_state(root: Path, cfg: LiveClientConfig, scenario: str) -> Path:
    path = root / scenario / "mock-qmt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "account_id": cfg.account_id,
        "available_cash": 10_000,
        "total_asset": 10_000,
        "positions": {},
    }), encoding="utf-8")
    return path


class _ExplodingServer:
    def __init__(self, *_args, **_kwargs):
        raise AssertionError("offline submit constructed a server client")


def _missing_preflight_blocks_before_qmt(root: Path) -> dict:
    cfg = _config(root, "missing-preflight")
    batch = validate_order_batch(_orders(cfg), TRADE_DATE, cfg)
    LiveStateStore(cfg.state_db).save_batch(batch)
    qmt_opened = {"value": False}

    def forbidden_gateway(*_args):
        qmt_opened["value"] = True
        raise AssertionError("QMT opened without a preflight receipt")

    original_gateway = cli._gateway
    cli._gateway = forbidden_gateway
    try:
        try:
            cli.submit(cfg, TRADE_DATE, None)
        except RuntimeError as exc:
            assert "preflight 回执" in str(exc)
        else:
            raise AssertionError("submit accepted a batch without preflight")
    finally:
        cli._gateway = original_gateway
    assert qmt_opened["value"] is False
    return {"status": "PASS", "qmt_opened": False}


def _server_down_and_replay(root: Path) -> dict:
    cfg = _config(root, "server-down-replay")
    _freeze(cfg)
    mock_path = _mock_state(root, cfg, "server-down-replay")
    calls = {"submit": 0}

    class CountingGateway(MockQMTGateway):
        def submit(self, order):
            calls["submit"] += 1
            return super().submit(order)

    original_server = cli.LiveServerClient
    original_gateway = cli._gateway
    cli.LiveServerClient = _ExplodingServer
    cli._gateway = lambda *_args: CountingGateway(mock_path, cfg.account_id)
    try:
        first = cli.submit(cfg, TRADE_DATE, mock_path)
        second = cli.submit(cfg, TRADE_DATE, mock_path)
    finally:
        cli.LiveServerClient = original_server
        cli._gateway = original_gateway
    assert first["attempted_now"] == 2
    assert second["attempted_now"] == 0
    assert second["already_recorded"] == 2
    assert calls["submit"] == 2
    return {
        "status": "PASS",
        "first_qmt_calls": first["attempted_now"],
        "replay_qmt_calls": second["attempted_now"],
        "server_contacted": False,
    }


def _crash_recovery(root: Path) -> dict:
    cfg = _config(root, "crash-recovery")
    _freeze(cfg)
    accepted: dict[str, SubmissionResult] = {}
    calls = {"submit": 0, "crashed": False}

    class CrashRecoverGateway:
        def connect(self):
            return None

        def close(self):
            return None

        def account_snapshot(self):
            return AccountSnapshot(cfg.account_id, 10_000, 10_000, {}, {})

        def find_existing_submission(self, order):
            return accepted.get(order["order_id"])

        def submit(self, order):
            calls["submit"] += 1
            result = SubmissionResult(
                str(20_000 + calls["submit"]),
                "SUBMITTED",
                execution_meta={"qmt_order_id": str(20_000 + calls["submit"])},
            )
            accepted[order["order_id"]] = result
            if not calls["crashed"]:
                calls["crashed"] = True
                raise RuntimeError("simulated crash after broker acceptance")
            return result

    original_gateway = cli._gateway
    cli._gateway = lambda *_args: CrashRecoverGateway()
    try:
        try:
            cli.submit(cfg, TRADE_DATE, None)
        except RuntimeError as exc:
            assert "simulated crash" in str(exc)
        else:
            raise AssertionError("simulated crash was not observed")
        recovered = cli.submit(cfg, TRADE_DATE, None)
    finally:
        cli._gateway = original_gateway
    assert recovered["recovered"] == 1
    assert recovered["submitted_now"] == 1
    assert calls["submit"] == 2
    return {
        "status": "PASS",
        "broker_submit_calls": calls["submit"],
        "recovered_by_remark": recovered["recovered"],
    }


def _ambiguous_no_replay(root: Path) -> dict:
    cfg = _config(root, "ambiguous-no-replay")
    _freeze(cfg)
    calls = {"submit": 0}

    class AmbiguousGateway:
        def connect(self):
            return None

        def close(self):
            return None

        def account_snapshot(self):
            return AccountSnapshot(cfg.account_id, 10_000, 10_000, {}, {})

        def find_existing_submission(self, _order):
            return None

        def submit(self, _order):
            calls["submit"] += 1
            raise RuntimeError("simulated QMT response loss")

    original_gateway = cli._gateway
    cli._gateway = lambda *_args: AmbiguousGateway()
    try:
        try:
            cli.submit(cfg, TRADE_DATE, None)
        except RuntimeError as exc:
            assert "response loss" in str(exc)
        else:
            raise AssertionError("ambiguous QMT call was not observed")
        try:
            cli.submit(cfg, TRADE_DATE, None)
        except RuntimeError as exc:
            assert "禁止自动重试" in str(exc)
        else:
            raise AssertionError("ambiguous QMT call was automatically replayed")
    finally:
        cli._gateway = original_gateway
    assert calls["submit"] == 1
    return {"status": "PASS", "broker_submit_calls": calls["submit"]}


def run_acceptance() -> dict:
    with tempfile.TemporaryDirectory(prefix="hydra-offline-acceptance-") as temp:
        root = Path(temp)
        scenarios = {
            "missing_preflight_blocks_before_qmt": (
                _missing_preflight_blocks_before_qmt(root)
            ),
            "server_down_and_repeat_submit": _server_down_and_replay(root),
            "crash_after_broker_acceptance": _crash_recovery(root),
            "ambiguous_response_no_replay": _ambiguous_no_replay(root),
        }
    return {
        "status": "PASS",
        "mode": "mock_qmt_only",
        "real_qmt_contacted": False,
        "server_contacted": False,
        "scenarios": scenarios,
    }


def main() -> None:
    print(json.dumps(run_acceptance(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
