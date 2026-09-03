"""Set Hydra's opening live baseline from one current QMT snapshot.

This is intentionally a small, one-off pre-activation operation. It records
old/new values and existing canary rows, but treats the latest QMT snapshot as
the opening truth instead of reconstructing every cash movement before launch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.db import make_engine, make_session_factory
from app.models import HydraTarget, InstanceState, Order, Trade
from app.settings import get_settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _positions(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RuntimeError("qmt_positions 必须是 JSON object")
    result: dict[str, int] = {}
    for raw_symbol, quantity in value.items():
        symbol = str(raw_symbol)
        if (
            not symbol
            or not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity < 0
        ):
            raise RuntimeError(f"position 非法: {raw_symbol}={quantity}")
        if quantity:
            result[symbol] = quantity
    return dict(sorted(result.items()))


def _validate_snapshot(payload: dict, expected_fingerprint: str) -> None:
    required = {
        "schema_version", "execution_domain", "account_alias", "instance_id",
        "account_fingerprint", "qmt_cash", "qmt_total_asset", "qmt_positions",
        "snapshot_time",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise RuntimeError(f"snapshot 缺字段: {missing}")
    if payload["schema_version"] != 1 or payload["execution_domain"] != "live":
        raise RuntimeError("snapshot 必须是 schema v1 / live")
    if payload["account_fingerprint"] != expected_fingerprint:
        raise RuntimeError("snapshot account fingerprint 与 server 配置不一致")
    cash = float(payload["qmt_cash"])
    total = float(payload["qmt_total_asset"])
    if not all(math.isfinite(value) and value >= 0 for value in (cash, total)):
        raise RuntimeError("QMT cash/total_asset 非法")
    if total <= 0 or cash > total + 1e-6:
        raise RuntimeError("QMT cash 不能大于 total_asset")
    _positions(payload["qmt_positions"])


def rebase(session_factory, payload: dict, evidence_sha256: str, *, apply: bool) -> dict:
    with session_factory() as session:
        state = session.get(InstanceState, str(payload["instance_id"]))
        if state is None:
            raise RuntimeError("Hydra instance 不存在")
        if state.execution_domain != "live" or state.account_alias != payload["account_alias"]:
            raise RuntimeError("snapshot 与 Hydra instance 账户不一致")
        target_count = session.scalar(
            select(func.count()).select_from(HydraTarget).where(
                HydraTarget.execution_domain == "live",
                HydraTarget.account_alias == state.account_alias,
            )
        )
        if target_count:
            raise RuntimeError("正式 Hydra target 已存在，不能再改 opening baseline")

        owned = set(state.owned_symbols or [])
        if not owned:
            raise RuntimeError("Hydra instance 没有 ETF 白名单")
        all_positions = _positions(payload["qmt_positions"])
        managed = {
            symbol: qty for symbol, qty in all_positions.items() if symbol in owned
        }
        external = {
            symbol: qty for symbol, qty in all_positions.items() if symbol not in owned
        }
        canaries = []
        for order in session.execute(
            select(Order).where(
                Order.execution_domain == "live",
                Order.qmt_account_alias == state.account_alias,
                Order.target_id.like("server_canary:%"),
            ).order_by(Order.created_at, Order.order_id)
        ).scalars():
            filled = session.scalar(
                select(func.max(Trade.filled_quantity)).where(
                    Trade.order_id == order.order_id,
                    Trade.execution_domain == "live",
                )
            )
            canaries.append({
                "order_id": order.order_id,
                "valid_date": order.valid_date,
                "status": order.status,
                "max_reported_filled_quantity": int(filled or 0),
            })

        recovery_id = "hbr_" + evidence_sha256
        strategy_state = dict(state.strategy_state or {})
        previous = strategy_state.get("opening_baseline_rebase")
        already_applied = bool(
            previous
            and previous.get("recovery_id") == recovery_id
            and abs(float(state.virtual_cash) - float(payload["qmt_cash"])) <= 1e-6
            and _positions(state.virtual_positions or {}) == managed
        )
        result = {
            "recovery_id": recovery_id,
            "instance_id": state.instance_id,
            "account_alias": state.account_alias,
            "previous_cash": float(state.virtual_cash),
            "previous_positions": _positions(state.virtual_positions or {}),
            "recovered_cash": float(payload["qmt_cash"]),
            "recovered_positions": managed,
            "external_positions": external,
            "observed_canary_orders": canaries,
            "idempotent_replay": already_applied,
        }
        if not apply or already_applied:
            return result

        strategy_state.update({
            "reconciliation_status": "ok",
            "initialization_evidence_sha256": evidence_sha256,
            "initial_snapshot_time": str(payload["snapshot_time"]),
            "initial_total_asset": float(payload["qmt_total_asset"]),
            "external_positions_snapshot": external,
            "external_positions_snapshot_time": str(payload["snapshot_time"]),
            "opening_baseline_rebase": {
                **result,
                "snapshot_time": str(payload["snapshot_time"]),
                "qmt_total_asset": float(payload["qmt_total_asset"]),
                "evidence_sha256": evidence_sha256,
                "recovered_at": _now_iso(),
                "superseded_initialization_evidence_sha256": strategy_state.get(
                    "initialization_evidence_sha256"
                ),
            },
        })
        state.virtual_cash = float(payload["qmt_cash"])
        state.virtual_positions = managed
        state.strategy_state = strategy_state
        state.last_update = _now_iso()
        session.commit()
        return result


def _backup(db_url: str, backup_dir: Path) -> Path:
    url = make_url(db_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        raise RuntimeError("恢复命令只支持具名 SQLite")
    source = Path(url.database).expanduser().resolve()
    backup_dir.mkdir(parents=True, exist_ok=False)
    destination = backup_dir / source.name
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("backup quick_check failed")
    finally:
        dst.close()
        src.close()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    raw = args.snapshot.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("snapshot 必须是 JSON object")
    evidence_sha256 = hashlib.sha256(raw).hexdigest()
    settings = get_settings()
    _validate_snapshot(payload, settings.live_qmt_account_sha256)
    session_factory = make_session_factory(make_engine(settings.db_url))

    preview = rebase(session_factory, payload, evidence_sha256, apply=False)
    if not args.apply:
        print(json.dumps({"status": "DRY_RUN_OK", **preview}, ensure_ascii=False, indent=2))
        return 0
    backup = _backup(settings.db_url, args.backup_dir)
    result = rebase(session_factory, payload, evidence_sha256, apply=True)
    status = "ALREADY_APPLIED" if result["idempotent_replay"] else "APPLIED"
    print(json.dumps({
        "status": status,
        "database_backup": str(backup),
        **result,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
