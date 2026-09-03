"""Hydra live client CLI：query → preflight → offline submit → settle。"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from live_client.config import LiveClientConfig
from live_client.core import (
    validate_account_capacity,
    validate_frozen_batch,
    validate_order_batch,
)
from live_client.gateway import MockQMTGateway, XtQMTGateway, live_order_remark
from live_client.http_client import LiveServerClient
from live_client.state import LiveStateStore


def _logger(cfg: LiveClientConfig) -> logging.Logger:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("hydra.live")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [HYDRA-LIVE] %(message)s"
    )
    handler = TimedRotatingFileHandler(
        cfg.log_dir / "hydra-live.log", when="midnight", backupCount=365,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    return logger


def _gateway(cfg: LiveClientConfig, mock_state: Path | None):
    if cfg.mode == "mock_qmt":
        if mock_state is None:
            raise ValueError("mock_qmt 模式必须提供 --mock-state")
        return MockQMTGateway(mock_state, cfg.account_id)
    return XtQMTGateway(cfg)


def query(cfg: LiveClientConfig, trade_date: str) -> dict:
    server = LiveServerClient(
        cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
    )
    orders = server.fetch_orders(trade_date)
    if not orders:
        return {"trade_date": trade_date, "orders": 0, "status": "NO_ORDERS"}
    batch = validate_order_batch(orders, trade_date, cfg)
    installed = LiveStateStore(cfg.state_db).save_batch(batch)
    return {
        "trade_date": trade_date,
        "orders": len(batch.orders),
        "batch_sha256": batch.batch_sha256,
        "status": "FETCHED" if installed else "ALREADY_FETCHED",
    }


def _load_frozen_batch(
    cfg: LiveClientConfig, trade_date: str, state: LiveStateStore,
):
    return validate_frozen_batch(state.load_batch(trade_date), trade_date, cfg)


def preflight(
    cfg: LiveClientConfig, trade_date: str, mock_state: Path | None,
) -> dict:
    """Required online reconciliation, completed outside the submit window."""
    state = LiveStateStore(cfg.state_db)
    batch = _load_frozen_batch(cfg, trade_date, state)
    gateway = _gateway(cfg, mock_state)
    gateway.connect()
    try:
        snapshot = gateway.account_snapshot()
        if snapshot.account_id != cfg.account_id:
            raise RuntimeError("QMT account_id 二次校验失败")
        server = LiveServerClient(
            cfg.server_base_url, cfg.api_key,
            execution_domain=cfg.execution_domain,
        )
        reconciliation = _require_server_reconciled(server, cfg, snapshot)
        risk_snapshot = validate_account_capacity(
            batch,
            snapshot.available_cash,
            snapshot.sellable_positions,
            cfg,
            snapshot.total_asset,
        )
    finally:
        gateway.close()
    receipt = {
        "status": "PASSED",
        "trade_date": trade_date,
        "batch_sha256": batch.batch_sha256,
        "account_alias": cfg.account_alias,
        "account_fingerprint": cfg.expected_account_sha256,
        "reconciliation": reconciliation,
        "risk": risk_snapshot,
    }
    receipt_sha256 = state.record_preflight(batch.batch_sha256, receipt)
    return {
        "trade_date": trade_date,
        "batch_sha256": batch.batch_sha256,
        "preflight_receipt_sha256": receipt_sha256,
        "reconciliation": reconciliation,
        "risk": risk_snapshot,
        "status": "READY_FOR_OFFLINE_SUBMIT",
    }


def submit(cfg: LiveClientConfig, trade_date: str, mock_state: Path | None) -> dict:
    cfg.require_submission_enabled()
    state = LiveStateStore(cfg.state_db)
    # The batch was independently hashed and frozen during query.  Re-validate
    # the local bytes, but make no server call in the trading-critical path.
    frozen_batch = _load_frozen_batch(cfg, trade_date, state)
    preflight_receipt = state.latest_preflight(frozen_batch.batch_sha256)
    expected_receipt = {
        "status": "PASSED",
        "trade_date": trade_date,
        "batch_sha256": frozen_batch.batch_sha256,
        "account_alias": cfg.account_alias,
        "account_fingerprint": cfg.expected_account_sha256,
    }
    if preflight_receipt is None or any(
        preflight_receipt["payload"].get(key) != value
        for key, value in expected_receipt.items()
    ):
        raise RuntimeError(
            "本地没有该冻结批次已通过的在线 preflight 回执，拒绝离线下单"
        )
    gateway = _gateway(cfg, mock_state)
    gateway.connect()
    try:
        snapshot = gateway.account_snapshot()
        if snapshot.account_id != cfg.account_id:
            raise RuntimeError("QMT account_id 二次校验失败")
        existing_rows = {
            row["order_id"]: row
            for row in state.submissions_for_date(trade_date)
        }
        if existing_rows:
            if state.risk_check(frozen_batch.batch_sha256) is None:
                raise RuntimeError("本地已有提交意图但缺少初始风控快照")
            remaining_orders = tuple(
                order for order in frozen_batch.orders
                if existing_rows.get(order["order_id"], {}).get("submit_status")
                in {None, "PREPARED"}
            )
            capacity_batch = replace(frozen_batch, orders=remaining_orders)
        else:
            capacity_batch = frozen_batch
        risk_snapshot = validate_account_capacity(
            capacity_batch,
            snapshot.available_cash,
            snapshot.sellable_positions,
            cfg,
            snapshot.total_asset,
        )
        if not existing_rows:
            state.record_risk_check(frozen_batch.batch_sha256, risk_snapshot)
        submitted = rejected = submitted_now = 0
        attempted_now = recovered = already_recorded = 0
        for order in sorted(
            frozen_batch.orders,
            key=lambda item: (item["direction"] != "SELL", item["symbol"]),
        ):
            remark = live_order_remark(order)
            local = state.prepare_submission(
                order["order_id"], frozen_batch.batch_sha256, remark,
            )
            if local["submit_status"] in {"SUBMITTED", "REJECTED"}:
                already_recorded += 1
                if local["submit_status"] == "SUBMITTED":
                    submitted += 1
                else:
                    rejected += 1
                continue

            existing = gateway.find_existing_submission(order)
            if existing is not None:
                state.complete_submission(
                    order["order_id"], existing.local_order_id, existing.status,
                    existing.detail, existing.execution_meta,
                )
                recovered += 1
                submitted += 1
                continue

            if local["submit_status"] == "SUBMITTING_UNKNOWN":
                raise RuntimeError(
                    f"order_id {order['order_id']} 曾进入 QMT 调用但未找到确定回报；"
                    "为防重复下单，禁止自动重试，请按 remark 核对券商委托"
                )
            if local["submit_status"] != "PREPARED":
                raise RuntimeError(
                    f"order_id 本地提交状态非法: {order['order_id']} "
                    f"{local['submit_status']}"
                )
            if not state.claim_submission(order["order_id"]):
                raise RuntimeError(
                    f"order_id {order['order_id']} 已被另一进程认领，拒绝并发下单"
                )
            result = gateway.submit(order)
            state.complete_submission(
                order["order_id"],
                result.local_order_id,
                result.status,
                result.detail,
                result.execution_meta,
            )
            attempted_now += 1
            if result.status == "SUBMITTED":
                submitted += 1
                submitted_now += 1
            else:
                rejected += 1
        return {
            "trade_date": trade_date,
            "batch_sha256": frozen_batch.batch_sha256,
            "submitted": submitted,
            "rejected": rejected,
            "submitted_now": submitted_now,
            "attempted_now": attempted_now,
            "recovered": recovered,
            "already_recorded": already_recorded,
        }
    finally:
        gateway.close()


def settle(cfg: LiveClientConfig, trade_date: str, mock_state: Path | None) -> dict:
    state = LiveStateStore(cfg.state_db)
    submissions = state.submissions_for_date(trade_date)
    submitted = [row for row in submissions if row["submit_status"] == "SUBMITTED"]
    rejected = [row for row in submissions if row["submit_status"] == "REJECTED"]
    unresolved = [
        row for row in submissions
        if row["submit_status"] in {"PREPARED", "SUBMITTING_UNKNOWN"}
    ]
    if unresolved:
        raise RuntimeError("本地仍有提交状态不确定的订单，拒绝结算")
    if not submitted and not rejected:
        raise RuntimeError("本地没有可结算的订单")
    results = []
    if submitted:
        gateway = _gateway(cfg, mock_state)
        gateway.connect()
        try:
            results.extend(gateway.settlement_results(submitted))
        finally:
            gateway.close()
    results.extend({
        "order_id": row["order_id"],
        "filled_quantity": 0,
        "filled_price": 0.0,
        "status": "REJECTED",
        "symbol": row["symbol"],
        "direction": row["direction"],
        **dict(row.get("execution_meta") or {}),
    } for row in rejected)
    data = LiveServerClient(
        cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
    ).push_trade_results(
        trade_date, results,
    )
    return {"trade_date": trade_date, "results": len(results), "server": data}


def initialize_account(
    cfg: LiveClientConfig, evidence_sha256: str, mock_state: Path | None,
) -> dict:
    gateway = _gateway(cfg, mock_state)
    gateway.connect()
    try:
        snapshot = gateway.account_snapshot()
    finally:
        gateway.close()
    return LiveServerClient(
        cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
    ).initialize_account({
        "execution_domain": cfg.execution_domain,
        "account_alias": cfg.account_alias,
        "qmt_account_id": snapshot.account_id,
        "instance_id": cfg.instance_id,
        "qmt_cash": snapshot.available_cash,
        "qmt_total_asset": snapshot.total_asset,
        "qmt_positions": snapshot.positions,
        "owned_symbols": sorted(cfg.allowed_symbols),
        "snapshot_time": datetime.now().astimezone().isoformat(),
        "evidence_sha256": evidence_sha256,
    })


def journal_cash_flow(
    cfg: LiveClientConfig,
    *,
    event_date: str,
    event_type: str,
    amount: float,
    source: str,
    source_event_id: str,
    evidence_sha256: str,
    description: str | None,
) -> dict:
    return LiveServerClient(
        cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
    ).post_cash_flow({
        "execution_domain": cfg.execution_domain,
        "account_alias": cfg.account_alias,
        "instance_id": cfg.instance_id,
        "event_date": event_date,
        "event_type": event_type,
        "amount": amount,
        "currency": "CNY",
        "source": source,
        "source_event_id": source_event_id,
        "evidence_sha256": evidence_sha256,
        "description": description,
    })


def _require_server_reconciled(server, cfg, snapshot) -> dict:
    reconciliation = server.reconcile({
        "execution_domain": cfg.execution_domain,
        "account_alias": cfg.account_alias,
        "instance_id": cfg.instance_id,
        "qmt_account_id": snapshot.account_id,
        "qmt_cash": snapshot.available_cash,
        "qmt_positions": snapshot.positions,
        "snapshot_time": datetime.now().astimezone().isoformat(),
        "dry_run": True,
        "force": False,
    })
    if (
        reconciliation["n_mismatched"]
        or reconciliation["n_server_only"]
        or reconciliation["n_qmt_only"]
    ):
        raise RuntimeError("QMT positions 与 server ledger 不一致")
    if abs(float(reconciliation["cash_diff"])) > 1.0:
        raise RuntimeError("QMT cash 与 server ledger 不一致")
    return reconciliation


def reconcile_and_close(
    cfg: LiveClientConfig,
    attempt_id: str,
    evidence_sha256: str,
    mock_state: Path | None,
) -> dict:
    gateway = _gateway(cfg, mock_state)
    gateway.connect()
    try:
        snapshot = gateway.account_snapshot()
    finally:
        gateway.close()
    server = LiveServerClient(
        cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
    )
    reconciliation = _require_server_reconciled(server, cfg, snapshot)
    closed = server.close_attempt({
        "execution_domain": "live",
        "account_alias": cfg.account_alias,
        "attempt_id": attempt_id,
        "actual_cash": snapshot.available_cash,
        "actual_positions": snapshot.positions,
        "reconciliation_evidence_sha256": evidence_sha256,
    })
    return {"reconciliation": reconciliation, "attempt": closed}


def doctor(cfg: LiveClientConfig) -> dict:
    """Validate private configuration and migrate state without external I/O."""
    LiveStateStore(cfg.state_db)
    return {
        "status": "LOCAL_CONFIG_OK",
        "mode": cfg.mode,
        "execution_domain": cfg.execution_domain,
        "account_alias": cfg.account_alias,
        "instance_id": cfg.instance_id,
        "task_prefix": cfg.task_prefix,
        "risk_mode": cfg.risk_mode,
        "trading_enabled": cfg.trading_enabled,
        "transport": cfg.server_base_url.split(":", 1)[0],
        "state_schema": "ok",
        "server_contacted": False,
        "qmt_contacted": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydra independent live client")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("query", "preflight", "submit", "settle"):
        command = sub.add_parser(name)
        command.add_argument("--date", required=True)
        if name in {"preflight", "submit", "settle"}:
            command.add_argument("--mock-state", type=Path)
    sub.add_parser("doctor")
    initialize = sub.add_parser("initialize-account")
    initialize.add_argument("--evidence-sha256", required=True)
    initialize.add_argument("--mock-state", type=Path)
    close = sub.add_parser("reconcile-close")
    close.add_argument("--attempt-id", required=True)
    close.add_argument("--evidence-sha256", required=True)
    close.add_argument("--mock-state", type=Path)
    cash_flow = sub.add_parser("cash-flow")
    cash_flow.add_argument("--date", required=True)
    cash_flow.add_argument(
        "--type", required=True,
        choices=("DIVIDEND", "DEPOSIT", "WITHDRAWAL", "OTHER"),
    )
    cash_flow.add_argument("--amount", required=True, type=float)
    cash_flow.add_argument("--source", required=True)
    cash_flow.add_argument("--source-event-id", required=True)
    cash_flow.add_argument("--evidence-sha256", required=True)
    cash_flow.add_argument("--description")
    args = parser.parse_args()
    cfg = LiveClientConfig.from_env()
    log = _logger(cfg)
    try:
        if args.command == "doctor":
            result = doctor(cfg)
        elif args.command == "query":
            result = query(cfg, args.date)
        elif args.command == "preflight":
            result = preflight(cfg, args.date, args.mock_state)
        elif args.command == "submit":
            result = submit(cfg, args.date, args.mock_state)
        elif args.command == "settle":
            result = settle(cfg, args.date, args.mock_state)
        elif args.command == "initialize-account":
            result = initialize_account(cfg, args.evidence_sha256, args.mock_state)
        elif args.command == "cash-flow":
            result = journal_cash_flow(
                cfg,
                event_date=args.date,
                event_type=args.type,
                amount=args.amount,
                source=args.source,
                source_event_id=args.source_event_id,
                evidence_sha256=args.evidence_sha256,
                description=args.description,
            )
        else:
            result = reconcile_and_close(
                cfg, args.attempt_id, args.evidence_sha256, args.mock_state,
            )
        log.info(json.dumps(result, ensure_ascii=False, sort_keys=True))
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except Exception:
        log.exception(
            "FAIL_CLOSED command=%s date=%s",
            args.command,
            getattr(args, "date", None),
        )
        raise


if __name__ == "__main__":
    main()
