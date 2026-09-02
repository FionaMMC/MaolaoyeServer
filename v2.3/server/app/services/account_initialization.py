"""从一次获准的 QMT 只读快照创建 Hydra InstanceState。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.exceptions import APIError, ErrorCode
from app.models import InstanceState
from app.schemas.account_initialization import (
    AccountInitializationRequest,
    AccountInitializationResponseData,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _position_partitions(req: AccountInitializationRequest) -> tuple[dict[str, int], dict[str, int]]:
    """Keep external physical-account positions auditable but out of Hydra."""
    owned = set(req.owned_symbols)
    managed: dict[str, int] = {}
    external: dict[str, int] = {}
    for code, quantity in req.qmt_positions.items():
        if quantity <= 0:
            continue
        (managed if code in owned else external)[code] = quantity
    return managed, external


def _positions_sha256(positions: dict[str, int]) -> str:
    body = json.dumps(positions, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


class AccountInitializationService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def initialize(
        self, req: AccountInitializationRequest,
    ) -> AccountInitializationResponseData:
        positions, external_positions = _position_partitions(req)
        with self.session_factory() as session:
            existing = session.get(InstanceState, req.instance_id)
            if existing is not None:
                state = dict(existing.strategy_state or {})
                same = (
                    existing.execution_domain == req.execution_domain
                    and existing.account_alias == req.account_alias
                    and float(existing.virtual_cash) == req.qmt_cash
                    and dict(existing.virtual_positions or {}) == positions
                    and existing.owned_symbols == req.owned_symbols
                    and state.get("initialization_evidence_sha256") == req.evidence_sha256
                    and dict(state.get("external_positions_snapshot") or {}) == external_positions
                )
                if not same:
                    raise APIError(
                        ErrorCode.BAD_REQUEST,
                        "instance 已初始化且内容不同，拒绝覆盖；请走正式对账/现金流流程",
                        http_status=409,
                    )
                return AccountInitializationResponseData(
                    instance_id=req.instance_id,
                    execution_domain=req.execution_domain,
                    account_alias=req.account_alias,
                    virtual_cash=req.qmt_cash,
                    positions=positions,
                    evidence_sha256=req.evidence_sha256,
                    external_position_count=len(external_positions),
                    idempotent_replay=True,
                )

            now = _now_iso()
            session.add(InstanceState(
                instance_id=req.instance_id,
                execution_domain=req.execution_domain,
                account_alias=req.account_alias,
                virtual_cash=req.qmt_cash,
                virtual_positions=positions,
                owned_symbols=req.owned_symbols,
                strategy_state={
                    "reconciliation_status": "ok",
                    "initialization_evidence_sha256": req.evidence_sha256,
                    "initial_snapshot_time": req.snapshot_time,
                    "initial_total_asset": req.qmt_total_asset,
                    "initialized_from_qmt_read_only": True,
                    "external_positions_snapshot": external_positions,
                    "external_positions_sha256": _positions_sha256(external_positions),
                    "external_positions_snapshot_time": req.snapshot_time,
                },
                last_update=now,
            ))
            session.commit()
        return AccountInitializationResponseData(
            instance_id=req.instance_id,
            execution_domain=req.execution_domain,
            account_alias=req.account_alias,
            virtual_cash=req.qmt_cash,
            positions=positions,
            evidence_sha256=req.evidence_sha256,
            external_position_count=len(external_positions),
            idempotent_replay=False,
        )
