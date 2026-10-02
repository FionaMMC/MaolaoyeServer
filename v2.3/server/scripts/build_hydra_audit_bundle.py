"""导出一次 Hydra target 的不可变月末/月初审计包。"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    CashFlowJournal,
    ExecutionQualityObservation,
    HydraExecutionAttempt,
    HydraRebalance,
    HydraTarget,
    InstanceState,
    Order,
    Trade,
)


def _jsonable(row) -> dict:
    return {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns
    }


def _write_json(path: Path, payload) -> str:
    body = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n"
    ).encode()
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise RuntimeError("output-dir 非空，拒绝覆盖已有审计证据")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    sf = sessionmaker(bind=create_engine(args.db_url, future=True), future=True)
    with sf() as session:
        target = session.get(HydraTarget, args.target_id)
        if target is None:
            raise RuntimeError("target_id 不存在")
        rebalances = session.execute(
            select(HydraRebalance).where(HydraRebalance.target_id == args.target_id)
        ).scalars().all()
        rebalance_ids = [row.rebalance_id for row in rebalances]
        attempts = session.execute(
            select(HydraExecutionAttempt).where(
                HydraExecutionAttempt.rebalance_id.in_(rebalance_ids)
            )
        ).scalars().all() if rebalance_ids else []
        orders = session.execute(
            select(Order).where(Order.target_id == args.target_id)
        ).scalars().all()
        order_ids = [row.order_id for row in orders]
        trades = session.execute(
            select(Trade).where(Trade.order_id.in_(order_ids))
        ).scalars().all() if order_ids else []
        quality = session.execute(
            select(ExecutionQualityObservation).where(
                ExecutionQualityObservation.order_id.in_(order_ids)
            )
        ).scalars().all() if order_ids else []
        cash_flows = session.execute(
            select(CashFlowJournal).where(
                CashFlowJournal.execution_domain == target.execution_domain,
                CashFlowJournal.account_alias == target.account_alias,
                CashFlowJournal.event_date >= target.decision_date,
            )
        ).scalars().all()
        state = session.get(InstanceState, args.instance_id)
        if state is None or state.execution_domain != target.execution_domain:
            raise RuntimeError("instance 不存在或与 target 跨域")

    payloads = {
        "target.json": _jsonable(target),
        "rebalances.json": [_jsonable(row) for row in rebalances],
        "attempts.json": [_jsonable(row) for row in attempts],
        "orders.json": [_jsonable(row) for row in orders],
        "trades.json": [_jsonable(row) for row in trades],
        "execution_quality.json": [_jsonable(row) for row in quality],
        "cash_flows.json": [_jsonable(row) for row in cash_flows],
        "month_end_state.json": _jsonable(state),
    }
    hashes = {
        filename: _write_json(args.output_dir / filename, payload)
        for filename, payload in payloads.items()
    }
    manifest = {
        "schema_version": 1,
        "target_id": args.target_id,
        "execution_domain": target.execution_domain,
        "account_alias": target.account_alias,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "files": hashes,
    }
    manifest_sha = _write_json(args.output_dir / "manifest.json", manifest)
    print(json.dumps({**manifest, "manifest_sha256": manifest_sha}, indent=2))


if __name__ == "__main__":
    main()
