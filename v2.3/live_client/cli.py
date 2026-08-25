"""Hydra live client CLI：query → submit → settle。"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from live_client.config import LiveClientConfig
from live_client.core import validate_account_capacity, validate_order_batch
from live_client.gateway import MockQMTGateway, XtQMTGateway
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
    server = LiveServerClient(cfg.server_base_url, cfg.api_key)
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


def submit(cfg: LiveClientConfig, trade_date: str, mock_state: Path | None) -> dict:
    cfg.require_submission_enabled()
    server = LiveServerClient(cfg.server_base_url, cfg.api_key)
    # 网络失败、非 0 响应或空批次都会抛出；绝不沿用本地旧快照继续。
    fresh_orders = server.fetch_orders(trade_date)
    fresh_batch = validate_order_batch(fresh_orders, trade_date, cfg)
    state = LiveStateStore(cfg.state_db)
    state.assert_same_batch(fresh_batch)
    gateway = _gateway(cfg, mock_state)
    gateway.connect()
    try:
        snapshot = gateway.account_snapshot()
        if snapshot.account_id != cfg.account_id:
            raise RuntimeError("QMT account_id 二次校验失败")
        _require_server_reconciled(server, cfg, snapshot)
        risk_snapshot = validate_account_capacity(
            fresh_batch,
            snapshot.available_cash,
            snapshot.sellable_positions,
            cfg,
            snapshot.total_asset,
        )
        state.record_risk_check(fresh_batch.batch_sha256, risk_snapshot)
        submitted = rejected = 0
        for order in sorted(
            fresh_batch.orders,
            key=lambda item: (item["direction"] != "SELL", item["symbol"]),
        ):
            result = gateway.submit(order)
            state.record_submission(
                order["order_id"],
                fresh_batch.batch_sha256,
                result.local_order_id,
                result.status,
                result.detail,
                result.execution_meta,
            )
            if result.status == "SUBMITTED":
                submitted += 1
            else:
                rejected += 1
        return {
            "trade_date": trade_date,
            "batch_sha256": fresh_batch.batch_sha256,
            "submitted": submitted,
            "rejected": rejected,
        }
    finally:
        gateway.close()


def settle(cfg: LiveClientConfig, trade_date: str, mock_state: Path | None) -> dict:
    state = LiveStateStore(cfg.state_db)
    submissions = state.submissions_for_date(trade_date)
    submitted = [row for row in submissions if row["submit_status"] == "SUBMITTED"]
    if not submitted:
        raise RuntimeError("本地没有可结算的已提交订单")
    gateway = _gateway(cfg, mock_state)
    gateway.connect()
    try:
        results = gateway.settlement_results(submitted)
    finally:
        gateway.close()
    data = LiveServerClient(cfg.server_base_url, cfg.api_key).push_trade_results(
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
    return LiveServerClient(cfg.server_base_url, cfg.api_key).initialize_account({
        "execution_domain": "live",
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
    return LiveServerClient(cfg.server_base_url, cfg.api_key).post_cash_flow({
        "execution_domain": "live",
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
        "execution_domain": "live",
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
    server = LiveServerClient(cfg.server_base_url, cfg.api_key)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydra independent live client")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("query", "submit", "settle"):
        command = sub.add_parser(name)
        command.add_argument("--date", required=True)
        if name in {"submit", "settle"}:
            command.add_argument("--mock-state", type=Path)
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
        if args.command == "query":
            result = query(cfg, args.date)
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
