"""Hydra live client CLI：query → preflight → offline submit → cancel/settle。"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from live_client.config import LiveClientConfig
from live_client.core import (
    validate_account_capacity,
    validate_frozen_batch,
    validate_order_batch,
)
from live_client.gateway import MockQMTGateway, XtQMTGateway, live_order_remark
from live_client.execution_queue import account_submission_lock, cash_readiness
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


def _strategy_capacity(
    cfg: LiveClientConfig, snapshot, reconciliation: dict, gateway,
) -> tuple[float, dict[str, int], float]:
    """Return cash, sellable positions and NAV owned by this strategy only."""
    if cfg.ledger_mode == "dedicated":
        return (
            float(snapshot.available_cash),
            dict(snapshot.sellable_positions),
            float(snapshot.total_asset),
        )
    managed_cash = float(reconciliation["managed_cash"])
    managed_positions = {
        symbol: int(quantity)
        for symbol, quantity in reconciliation["managed_positions"].items()
        if int(quantity) > 0
    }
    managed_sellable = {
        symbol: min(quantity, int(snapshot.sellable_positions.get(symbol, 0)))
        for symbol, quantity in managed_positions.items()
    }
    position_value = 0.0
    for symbol, quantity in managed_positions.items():
        physical_quantity = int(snapshot.positions.get(symbol, 0))
        physical_value = snapshot.position_market_values.get(symbol)
        if physical_quantity > 0 and physical_value is not None:
            unit_price = float(physical_value) / physical_quantity
        else:
            unit_price = float(gateway.market_quote(symbol).last_price)
        position_value += quantity * unit_price
    return managed_cash, managed_sellable, managed_cash + position_value


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
        capacity_cash, capacity_positions, capacity_nav = _strategy_capacity(
            cfg, snapshot, reconciliation, gateway,
        )
        risk_snapshot = validate_account_capacity(
            batch,
            capacity_cash,
            capacity_positions,
            cfg,
            capacity_nav,
        )
        risk_snapshot["capacity_scope"] = cfg.ledger_mode
        risk_snapshot["physical_qmt_available_cash"] = snapshot.available_cash
        risk_snapshot["physical_qmt_total_asset"] = snapshot.total_asset
        if cfg.ledger_mode == "attributed":
            risk_snapshot["managed_sellable_positions"] = capacity_positions
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
    with account_submission_lock(cfg.userdata_dir, cfg.expected_account_sha256):
        return _submit_locked(cfg, trade_date, mock_state)


def _submit_locked(cfg: LiveClientConfig, trade_date: str, mock_state: Path | None) -> dict:
    state = LiveStateStore(cfg.state_db)
    if state.workflow_receipt("close-window-intent", trade_date) is not None:
        return {"status": "EXECUTION_WINDOW_CLOSED", "trade_date": trade_date}
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
        initial_risk = state.risk_check(frozen_batch.batch_sha256)
        cost_policy = (initial_risk or {}).get("execution_cost_policy") or (
            preflight_receipt["payload"].get("risk", {}).get("execution_cost_policy")
        ) or {
            "execution_cost_reserve_bps": cfg.execution_cost_reserve_bps,
            "execution_min_commission": cfg.execution_min_commission,
        }
        execution_cfg = replace(
            cfg,
            execution_cost_reserve_bps=float(cost_policy["execution_cost_reserve_bps"]),
            execution_min_commission=float(cost_policy["execution_min_commission"]),
        )
        if existing_rows:
            if initial_risk is None:
                raise RuntimeError("本地已有提交意图但缺少初始风控快照")
            remaining_orders = tuple(
                order for order in frozen_batch.orders
                if existing_rows.get(order["order_id"], {}).get("submit_status")
                in {None, "PREPARED", "DEFERRED_CASH"}
            )
            capacity_batch = replace(frozen_batch, orders=remaining_orders)
        else:
            capacity_batch = frozen_batch
        if cfg.ledger_mode == "attributed":
            frozen_reconciliation = preflight_receipt["payload"].get(
                "reconciliation", {}
            )
            frozen_risk = preflight_receipt["payload"].get("risk", {})
            if frozen_reconciliation.get("reconciliation_scope") != "portfolio_attributed":
                raise RuntimeError("preflight 回执不是 attributed 策略额度")
            # Always re-check the complete frozen batch against the strategy's
            # immutable prior-evening allocation.  This remains valid after a
            # partial local submission and never borrows another strategy's cash.
            strategy_risk = validate_account_capacity(
                frozen_batch,
                float(frozen_reconciliation["managed_cash"]),
                {
                    symbol: int(quantity)
                    for symbol, quantity in frozen_risk[
                        "managed_sellable_positions"
                    ].items()
                },
                execution_cfg,
                float(frozen_risk["qmt_total_asset"]),
                enforce_projected_cash=False,
            )
            physical_risk = validate_account_capacity(
                capacity_batch,
                snapshot.available_cash,
                snapshot.sellable_positions,
                execution_cfg,
                snapshot.total_asset,
                enforce_projected_cash=False,
            )
            risk_snapshot = {
                "capacity_scope": "attributed",
                "strategy": strategy_risk,
                "physical_remaining": physical_risk,
            }
        else:
            risk_snapshot = validate_account_capacity(
                capacity_batch,
                snapshot.available_cash,
                snapshot.sellable_positions,
                execution_cfg,
                snapshot.total_asset,
                enforce_projected_cash=False,
            )
        if initial_risk is None:
            risk_snapshot["execution_cost_policy"] = cost_policy
            risk_snapshot["execution_queue_initial_cash"] = (
                float(frozen_reconciliation["managed_cash"])
                if cfg.ledger_mode == "attributed" else snapshot.available_cash
            )
            state.record_risk_check(frozen_batch.batch_sha256, risk_snapshot)
            initial_risk = risk_snapshot
        initial_owned_cash = initial_risk.get("execution_queue_initial_cash")
        if initial_owned_cash is None:
            # Compatibility with pre-queue evidence, never with fresh total
            # account cash after part of a batch has already been submitted.
            initial_owned_cash = (
                float(frozen_reconciliation["managed_cash"])
                if cfg.ledger_mode == "attributed"
                else initial_risk["qmt_available_cash"]
            )
        submitted = rejected = submitted_now = 0
        attempted_now = recovered = already_recorded = 0
        deferred = []
        physical_queue_cash = None
        observed_sell_proceeds = 0.0
        for order in sorted(
            frozen_batch.orders,
            key=lambda item: (item["direction"] != "SELL", item["symbol"]),
        ):
            remark = live_order_remark(order)
            local = state.prepare_submission(
                order["order_id"], frozen_batch.batch_sha256, remark,
            )
            if local["submit_status"] in {"SUBMITTED", "REJECTED", "NOT_SUBMITTED"}:
                already_recorded += 1
                if local["submit_status"] == "SUBMITTED":
                    submitted += 1
                elif local["submit_status"] == "REJECTED":
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
            if local["submit_status"] not in {"PREPARED", "DEFERRED_CASH"}:
                raise RuntimeError(
                    f"order_id 本地提交状态非法: {order['order_id']} "
                    f"{local['submit_status']}"
                )
            if order["direction"] == "BUY":
                current_rows = state.submissions_for_date(trade_date)
                sell_rows = [row for row in current_rows if (
                    row["direction"] == "SELL" and row["submit_status"] == "SUBMITTED"
                )]
                confirmed_fills = (
                    gateway.confirmed_sell_fills(sell_rows) if sell_rows else {}
                )
                state.observe_sell_fills(confirmed_fills)
                current = gateway.account_snapshot()
                if current.account_id != cfg.account_id:
                    raise RuntimeError("QMT account_id 二次校验失败")
                readiness = cash_readiness(
                    order, initial_owned_cash=initial_owned_cash,
                    qmt_available_cash=current.available_cash,
                    submissions=current_rows, confirmed_sell_fills=confirmed_fills,
                    reserve_bps=execution_cfg.execution_cost_reserve_bps,
                    min_commission=execution_cfg.execution_min_commission,
                )
                if physical_queue_cash is None:
                    physical_queue_cash = current.available_cash
                else:
                    physical_queue_cash += max(
                        0.0,
                        readiness["confirmed_sell_proceeds"] - observed_sell_proceeds,
                    )
                observed_sell_proceeds = readiness["confirmed_sell_proceeds"]
                # A lagging QMT asset snapshot must not make the same physical
                # cash available twice within this queue pass. New external
                # inflows/unfreezes can be picked up on the next explicit pass.
                readiness = cash_readiness(
                    order, initial_owned_cash=initial_owned_cash,
                    qmt_available_cash=max(0.0, min(
                        current.available_cash, physical_queue_cash,
                    )),
                    submissions=current_rows, confirmed_sell_fills=confirmed_fills,
                    reserve_bps=execution_cfg.execution_cost_reserve_bps,
                    min_commission=execution_cfg.execution_min_commission,
                )
                readiness["physical_qmt_available_cash"] = current.available_cash
                if not readiness["ready"]:
                    state.defer_for_cash(order["order_id"], readiness)
                    deferred.append({"order_id": order["order_id"], **readiness})
                    continue
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
                if order["direction"] == "BUY":
                    physical_queue_cash -= readiness["required_cash"]
            else:
                rejected += 1
        result = {
            "trade_date": trade_date,
            "batch_sha256": frozen_batch.batch_sha256,
            "submitted": submitted,
            "rejected": rejected,
            "submitted_now": submitted_now,
            "attempted_now": attempted_now,
            "recovered": recovered,
            "already_recorded": already_recorded,
        }
        if deferred:
            result.update(status="WAITING_FOR_CASH", deferred_cash=deferred)
        return result
    finally:
        gateway.close()


def settle(
    cfg: LiveClientConfig, trade_date: str, mock_state: Path | None,
    *, close_deferred: bool = False,
) -> dict:
    state = LiveStateStore(cfg.state_db)
    if close_deferred:
        # settle-close is the explicit end of this execution workflow. Unlike a
        # broker rejection, this proves these orders never entered order_stock.
        with account_submission_lock(cfg.userdata_dir, cfg.expected_account_sha256):
            state.close_deferred(trade_date)
    submissions = state.submissions_for_date(trade_date)
    submitted = [row for row in submissions if row["submit_status"] == "SUBMITTED"]
    rejected = [row for row in submissions if row["submit_status"] == "REJECTED"]
    not_submitted = [row for row in submissions if row["submit_status"] == "NOT_SUBMITTED"]
    unresolved = [
        row for row in submissions
        if row["submit_status"] in {"PREPARED", "SUBMITTING_UNKNOWN"}
    ]
    if not submissions:
        raise RuntimeError("本地没有可结算的订单")
    results = []
    pending = [row["order_id"] for row in unresolved]
    pending.extend(row["order_id"] for row in submissions if row["submit_status"] == "DEFERRED_CASH")
    issues = [{"order_id": row["order_id"], "reason": "本地提交状态待核实"} for row in unresolved]
    if submitted:
        gateway = _gateway(cfg, mock_state)
        try:
            gateway.connect()
            # Legacy/mock adapters retain the list contract. The real adapter
            # now reports independently validated open-order fills as well.
            observation_method = getattr(gateway, "settlement_observations", None)
            if observation_method is None:
                results.extend(gateway.settlement_results(submitted))
            else:
                observations = observation_method(submitted)
                results.extend(observations["results"])
                pending.extend(observations["pending_order_ids"])
                issues.extend(observations["issues"])
        except RuntimeError as exc:
            pending.extend(row["order_id"] for row in submitted)
            issues.append({"reason": str(exc)})
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
    results.extend({
        "order_id": row["order_id"],
        "filled_quantity": 0,
        "filled_price": 0.0,
        "status": "NOT_SUBMITTED",
        "symbol": row["symbol"],
        "direction": row["direction"],
        "not_submitted_reason": "INSUFFICIENT_CASH",
    } for row in not_submitted)
    data = None
    if results:
        data = LiveServerClient(
            cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
        ).push_trade_results(trade_date, results)
        # An accepted HTTP response with unmatched facts is not a completed
        # settlement; keep the missing ownership link visible to the caller.
        pending.extend(data.get("unmatched_order_ids", []))
        pending.extend(data.get("rejected_observations", {}).keys())
    return {
        "trade_date": trade_date, "results": len(results), "server": data,
        "pending_order_ids": sorted(set(pending)), "issues": issues,
        "status": "FACTS_RECORDED_PENDING" if pending else "FACTS_RECORDED",
    }


def _write_cancel_evidence(
    cfg: LiveClientConfig, trade_date: str, payload: dict,
) -> tuple[str, Path]:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode()).hexdigest()
    evidence_dir = cfg.log_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"cancel-open-{trade_date}-{digest}.json"
    if path.exists():
        if path.read_text(encoding="utf-8") != body:
            raise RuntimeError("同名 cancel-open evidence 内容不一致")
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
    return digest, path


def cancel_open_orders(
    cfg: LiveClientConfig, trade_date: str, mock_state: Path | None,
) -> dict:
    """Request EOD cancellation for this frozen Hydra batch and nothing else."""
    if cfg.mode == "live":
        now = datetime.now().astimezone()
        if trade_date != now.strftime("%Y%m%d"):
            raise RuntimeError("cancel-open 只能处理 QMT 当日委托")
        if (now.hour, now.minute) >= (14, 57):
            raise RuntimeError("cancel-open 已错过 14:57 交易所撤单截止时间")
    state = LiveStateStore(cfg.state_db)
    if not state.has_batch(trade_date):
        return {"status": "NO_ORDERS", "trade_date": trade_date}
    batch = _load_frozen_batch(cfg, trade_date, state)
    submissions = state.submissions_for_date(trade_date)
    unresolved = [
        row for row in submissions
        if row["submit_status"] in {"PREPARED", "SUBMITTING_UNKNOWN"}
    ]
    if unresolved:
        raise RuntimeError("本地仍有提交状态不确定的订单，禁止自动撤单")
    submitted = [
        row for row in submissions if row["submit_status"] == "SUBMITTED"
    ]
    if submitted:
        gateway = _gateway(cfg, mock_state)
        gateway.connect()
        try:
            results = gateway.cancel_open_orders(submitted)
        finally:
            gateway.close()
    else:
        results = []
    counts = {
        action: sum(row["action"] == action for row in results)
        for action in ("REQUESTED", "ALREADY_PENDING", "TERMINAL", "FAILED")
    }
    if counts["FAILED"]:
        status = "CANCEL_INCOMPLETE"
    elif counts["REQUESTED"] or counts["ALREADY_PENDING"]:
        status = "CANCEL_REQUESTED"
    else:
        status = "NO_ACTIVE_ORDERS"
    receipt = {
        "status": status,
        "trade_date": trade_date,
        "batch_sha256": batch.batch_sha256,
        "target_id": batch.orders[0]["target_id"],
        "rebalance_id": batch.rebalance_id,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "counts": counts,
        "results": results,
    }
    receipt_sha256, evidence_path = _write_cancel_evidence(
        cfg, trade_date, receipt,
    )
    return {
        **receipt,
        "cancel_receipt_sha256": receipt_sha256,
        "evidence_path": str(evidence_path),
    }


def _batch_attempt_id(batch) -> str:
    attempt_ids = {str(order.get("attempt_id") or "") for order in batch.orders}
    if len(attempt_ids) != 1 or "" in attempt_ids:
        raise RuntimeError("冻结批次缺少唯一 Hydra attempt_id")
    return next(iter(attempt_ids))


def close_execution_window(
    cfg: LiveClientConfig, trade_date: str, execution_deadline_at: str,
) -> dict:
    """End the local execution window without requiring a broker response.

    This is a workflow fact, never a claim that open broker orders are cancelled
    or that their funds/positions are available to another attempt.
    """
    deadline = datetime.fromisoformat(execution_deadline_at)
    if deadline.utcoffset() is None:
        raise ValueError("execution-deadline-at 必须带时区")
    if deadline.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d") != trade_date:
        raise ValueError("execution-deadline-at 必须属于该订单的中国交易日")
    if deadline > datetime.now(timezone.utc):
        raise ValueError("尚未到 execution-deadline-at，不能提前关闭执行窗口")
    state = LiveStateStore(cfg.state_db)
    batch = validate_frozen_batch(
        state.load_batch(trade_date), trade_date, cfg,
        enforce_execution_policy=False,
    )
    attempt_id = _batch_attempt_id(batch)
    intent = {
        "trade_date": trade_date,
        "batch_sha256": batch.batch_sha256,
        "attempt_id": attempt_id,
        "target_id": batch.orders[0]["target_id"],
        "rebalance_id": batch.rebalance_id,
        "account_alias": cfg.account_alias,
        "account_fingerprint": cfg.expected_account_sha256,
        "execution_domain": cfg.execution_domain,
        "close_mode": "execution_deadline",
        "execution_deadline_at": deadline.isoformat(),
    }
    with account_submission_lock(cfg.userdata_dir, cfg.expected_account_sha256):
        # If HTTP fails, the local window stays closed. Repeating this command
        # resends the same idempotent intent, not any broker order.
        intent_sha256 = state.record_workflow_receipt(
            "close-window-intent", trade_date, intent,
        )
        state.close_deferred(trade_date)
        existing = state.workflow_receipt("close-window", trade_date)
        if existing is not None:
            return existing["payload"]
    closed = LiveServerClient(
        cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
    ).close_attempt({
        "execution_domain": cfg.execution_domain,
        "account_alias": cfg.account_alias,
        "attempt_id": attempt_id,
        "close_mode": "execution_deadline",
        "execution_deadline_at": deadline.isoformat(),
        "reconciliation_evidence_sha256": intent_sha256,
    })
    if any(closed.get(key) != intent[key] for key in (
        "attempt_id", "target_id", "rebalance_id", "execution_domain",
    )):
        raise RuntimeError("Close-window 回执与本地冻结批次不一致")
    if closed.get("status") not in {
        "CLOSED_PENDING_BROKER", "CLOSED_PENDING_RECONCILIATION", "COMPLETE", "RESIDUAL",
    }:
        raise RuntimeError("Close-window 回执状态非法")
    receipt = {
        **intent,
        "workflow_intent_sha256": intent_sha256,
        "status": closed["status"],
        "close": closed,
    }
    state.record_workflow_receipt("close-window", trade_date, receipt)
    return receipt


def _write_reconciliation_evidence(
    cfg: LiveClientConfig, trade_date: str, attempt_id: str, snapshot,
) -> tuple[str, Path]:
    payload = {
        "account_alias": cfg.account_alias,
        "account_fingerprint": cfg.expected_account_sha256,
        "attempt_id": attempt_id,
        "available_cash": snapshot.available_cash,
        "positions": dict(sorted(snapshot.positions.items())),
        "total_asset": snapshot.total_asset,
        "trade_date": trade_date,
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode()).hexdigest()
    evidence_dir = cfg.log_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"reconcile-{trade_date}-{attempt_id}-{digest}.json"
    if path.exists():
        if path.read_text(encoding="utf-8") != body:
            raise RuntimeError("同名 reconciliation evidence 内容不一致")
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
    return digest, path


def settle_and_close(
    cfg: LiveClientConfig, trade_date: str, mock_state: Path | None,
) -> dict:
    """Push terminal order state, reconcile QMT, then close the Hydra attempt."""
    state = LiveStateStore(cfg.state_db)
    if not state.has_batch(trade_date):
        return {"status": "NO_ORDERS", "trade_date": trade_date}
    settlement = settle(cfg, trade_date, mock_state, close_deferred=True)
    if settlement["pending_order_ids"]:
        return {"status": "WAITING_FOR_BROKER", "settlement": settlement}
    batch = _load_frozen_batch(cfg, trade_date, state)
    attempt_id = _batch_attempt_id(batch)
    gateway = _gateway(cfg, mock_state)
    gateway.connect()
    try:
        snapshot = gateway.account_snapshot()
    finally:
        gateway.close()
    evidence_sha256, evidence_path = _write_reconciliation_evidence(
        cfg, trade_date, attempt_id, snapshot,
    )
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
    batch_target_id = batch.orders[0]["target_id"]
    if (
        closed.get("target_id") != batch_target_id
        or closed.get("rebalance_id") != batch.rebalance_id
    ):
        raise RuntimeError("Hydra close 回执与本地冻结批次 target/rebalance 不一致")
    receipt = {
        "trade_date": trade_date,
        "attempt_id": attempt_id,
        "target_id": closed["target_id"],
        "rebalance_id": closed["rebalance_id"],
        "status": closed["status"],
        "residual_after": closed.get("residual_after") or {},
        "evidence_sha256": evidence_sha256,
        "evidence_path": str(evidence_path),
    }
    receipt_sha256 = state.record_workflow_receipt("close", trade_date, receipt)
    return {
        "status": "ATTEMPT_CLOSED",
        "settlement": settlement,
        "reconciliation": reconciliation,
        "close": receipt,
        "close_receipt_sha256": receipt_sha256,
    }


def stage_residual_retry(
    cfg: LiveClientConfig, source_trade_date: str, next_trade_date: str,
    mock_state: Path | None,
) -> dict:
    """Stage only a residual explicitly returned by the Hydra close endpoint."""
    state = LiveStateStore(cfg.state_db)
    previous_retry = state.workflow_receipt("retry", source_trade_date)
    if previous_retry is not None:
        return {"status": "ALREADY_STAGED", **previous_retry["payload"]}
    close_receipt = state.workflow_receipt("close", source_trade_date)
    if close_receipt is None:
        if not state.has_batch(source_trade_date):
            return {"status": "NO_ATTEMPT", "trade_date": source_trade_date}
        raise RuntimeError("没有日终 Hydra close 回执，禁止生成补单")
    closed = close_receipt["payload"]
    if closed["status"] == "COMPLETE":
        return {"status": "NO_RESIDUAL", "trade_date": source_trade_date}
    if closed["status"] != "RESIDUAL" or not closed.get("residual_after"):
        raise RuntimeError("Hydra close 回执没有有效 RESIDUAL")
    batch = _load_frozen_batch(cfg, source_trade_date, state)
    batch_target_id = batch.orders[0]["target_id"]
    retry_binding = (
        cfg.retry_execution_raw_sha256,
        cfg.retry_target_id,
        cfg.retry_rebalance_id,
    )
    if not all(retry_binding):
        raise RuntimeError(
            "缺少完整 retry 绑定：execution hash、target_id 与 rebalance_id"
        )
    expected_binding = {
        "target_id": cfg.retry_target_id,
        "rebalance_id": cfg.retry_rebalance_id,
    }
    actual_binding = {
        "target_id": closed.get("target_id"),
        "rebalance_id": closed.get("rebalance_id"),
    }
    if actual_binding != expected_binding:
        raise RuntimeError("Hydra close 回执与已批准 retry target/rebalance 不一致")
    if (
        batch_target_id != cfg.retry_target_id
        or batch.rebalance_id != cfg.retry_rebalance_id
    ):
        raise RuntimeError("本地冻结批次与已批准 retry target/rebalance 不一致")

    gateway = _gateway(cfg, mock_state)
    gateway.connect()
    try:
        snapshot = gateway.account_snapshot()
    finally:
        gateway.close()
    evidence_sha256, evidence_path = _write_reconciliation_evidence(
        cfg, source_trade_date, closed["attempt_id"], snapshot,
    )
    server = LiveServerClient(
        cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
    )
    reconciliation = _require_server_reconciled(server, cfg, snapshot)
    staged = server.stage_retry({
        "execution_domain": "live",
        "account_alias": cfg.account_alias,
        "rebalance_id": closed["rebalance_id"],
        "trade_date": next_trade_date,
        "execution_raw_sha256": cfg.retry_execution_raw_sha256,
        "actual_cash": snapshot.available_cash,
        "actual_positions": snapshot.positions,
        "reconciliation_evidence_sha256": evidence_sha256,
    })
    if (
        staged.get("target_id") != cfg.retry_target_id
        or staged.get("rebalance_id") != cfg.retry_rebalance_id
    ):
        raise RuntimeError("Hydra retry 回执与已批准 target/rebalance 不一致")
    receipt = {
        "source_trade_date": source_trade_date,
        "next_trade_date": next_trade_date,
        "target_id": staged["target_id"],
        "attempt_id": staged["attempt_id"],
        "rebalance_id": staged["rebalance_id"],
        "batch_sha256": staged["batch_sha256"],
        "order_count": staged["order_count"],
        "evidence_sha256": evidence_sha256,
        "evidence_path": str(evidence_path),
    }
    receipt_sha256 = state.record_workflow_receipt(
        "retry", source_trade_date, receipt,
    )
    return {
        "status": "RETRY_STAGED",
        "reconciliation": reconciliation,
        "retry": receipt,
        "retry_receipt_sha256": receipt_sha256,
    }


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
        "ledger_mode": cfg.ledger_mode,
        "allocated_cash": cfg.initial_allocated_cash,
        "allocated_positions": (
            cfg.initial_allocated_positions
            if cfg.ledger_mode == "attributed" else None
        ),
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
    mock_state: Path | None = None,
    transition_to_attributed: bool = False,
) -> dict:
    qmt_cash = None
    snapshot_time = None
    if event_type.startswith("CAPITAL_"):
        gateway = _gateway(cfg, mock_state)
        gateway.connect()
        try:
            snapshot = gateway.account_snapshot()
            if snapshot.account_id != cfg.account_id:
                raise RuntimeError("QMT account_id 二次校验失败")
            qmt_cash = snapshot.available_cash
            snapshot_time = datetime.now().astimezone().isoformat()
        finally:
            gateway.close()
    return LiveServerClient(
        cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
    ).post_cash_flow({
        "execution_domain": cfg.execution_domain,
        "account_alias": cfg.account_alias,
        "instance_id": cfg.instance_id,
        "event_date": event_date,
        "event_type": event_type,
        "amount": amount,
        "qmt_cash": qmt_cash,
        "snapshot_time": snapshot_time,
        "transition_to_attributed": transition_to_attributed,
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
    if cfg.ledger_mode == "attributed":
        if reconciliation.get("reconciliation_scope") != "portfolio_attributed":
            raise RuntimeError("server 未按 attributed portfolio 口径对账")
        portfolio = reconciliation.get("portfolio") or {}
        if portfolio.get("n_mismatched") or not portfolio.get("cash_ok"):
            raise RuntimeError("QMT 全账户与策略子账本合计不一致")
        if reconciliation.get("managed_cash") is None or not isinstance(
            reconciliation.get("managed_positions"), dict
        ):
            raise RuntimeError("server 未返回 Hydra 策略可用额度")
    else:
        if reconciliation.get("reconciliation_scope", "instance") != "instance":
            raise RuntimeError("server 账本模式与 dedicated client 不一致")
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
        "ledger_mode": cfg.ledger_mode,
        "task_prefix": cfg.task_prefix,
        "risk_mode": cfg.risk_mode,
        "trading_enabled": cfg.trading_enabled,
        "transport": cfg.server_base_url.split(":", 1)[0],
        "state_schema": "ok",
        "server_contacted": False,
        "qmt_contacted": False,
    }


def show_strategy_ledger(cfg: LiveClientConfig) -> dict:
    return LiveServerClient(
        cfg.server_base_url, cfg.api_key, execution_domain=cfg.execution_domain,
    ).strategy_ledger(cfg.instance_id, cfg.account_alias)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydra independent live client")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "query", "preflight", "submit", "cancel-open", "settle", "settle-close",
    ):
        command = sub.add_parser(name)
        command.add_argument("--date", required=True)
        if name in {
            "preflight", "submit", "cancel-open", "settle", "settle-close",
        }:
            command.add_argument("--mock-state", type=Path)
    retry = sub.add_parser("retry")
    retry.add_argument("--date", required=True)
    retry.add_argument("--next-date", required=True)
    retry.add_argument("--mock-state", type=Path)
    close_window = sub.add_parser("close-window")
    close_window.add_argument("--date", required=True)
    close_window.add_argument("--execution-deadline-at", required=True)
    sub.add_parser("doctor")
    sub.add_parser("ledger")
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
        choices=(
            "DIVIDEND", "DEPOSIT", "WITHDRAWAL", "CAPITAL_ALLOCATION",
            "CAPITAL_DEALLOCATION", "OTHER",
        ),
    )
    cash_flow.add_argument("--amount", required=True, type=float)
    cash_flow.add_argument("--source", required=True)
    cash_flow.add_argument("--source-event-id", required=True)
    cash_flow.add_argument("--evidence-sha256", required=True)
    cash_flow.add_argument("--description")
    cash_flow.add_argument("--mock-state", type=Path)
    cash_flow.add_argument("--transition-to-attributed", action="store_true")
    args = parser.parse_args()
    cfg = LiveClientConfig.from_env()
    log = _logger(cfg)
    try:
        if args.command == "doctor":
            result = doctor(cfg)
        elif args.command == "ledger":
            result = show_strategy_ledger(cfg)
        elif args.command == "query":
            result = query(cfg, args.date)
        elif args.command == "preflight":
            result = preflight(cfg, args.date, args.mock_state)
        elif args.command == "submit":
            result = submit(cfg, args.date, args.mock_state)
        elif args.command == "cancel-open":
            result = cancel_open_orders(cfg, args.date, args.mock_state)
        elif args.command == "settle":
            result = settle(cfg, args.date, args.mock_state)
        elif args.command == "settle-close":
            result = settle_and_close(cfg, args.date, args.mock_state)
        elif args.command == "retry":
            result = stage_residual_retry(
                cfg, args.date, args.next_date, args.mock_state,
            )
        elif args.command == "close-window":
            result = close_execution_window(cfg, args.date, args.execution_deadline_at)
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
                mock_state=args.mock_state,
                transition_to_attributed=args.transition_to_attributed,
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
