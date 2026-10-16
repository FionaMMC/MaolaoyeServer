"""从一次获准的 QMT 只读快照创建 Hydra InstanceState。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.exceptions import APIError, ErrorCode
from app.models import InstanceState, Order
from app.services.ledger_transaction import begin_ledger_transaction
from app.services.strategy_capital import StrategyCapitalService
from app.schemas.account_initialization import (
    AccountInitializationRequest,
    AccountInitializationResponseData,
)
from app.services.ownership import (
    OwnershipOverlap,
    validate_no_owned_symbol_overlap,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _position_partitions(req: AccountInitializationRequest) -> tuple[dict[str, int], dict[str, int]]:
    """Return the managed ledger and the remaining physical-account positions."""
    if req.ledger_mode == "attributed":
        managed = {
            code: quantity
            for code, quantity in (req.allocated_positions or {}).items()
            if quantity > 0
        }
        external = {
            code: quantity - managed.get(code, 0)
            for code, quantity in req.qmt_positions.items()
            if quantity - managed.get(code, 0) > 0
        }
        return managed, external
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
    def __init__(self, session_factory, *, clock=None, max_snapshot_age_seconds=300):
        self.session_factory = session_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_snapshot_age_seconds = max_snapshot_age_seconds

    def initialize(
        self, req: AccountInitializationRequest,
    ) -> AccountInitializationResponseData:
        positions, external_positions = _position_partitions(req)
        virtual_cash = (
            float(req.allocated_cash)
            if req.ledger_mode == "attributed"
            else float(req.qmt_cash)
        )
        unallocated_cash = float(req.qmt_cash) - virtual_cash
        payload = req.model_dump(mode="json")
        # Retain proof of identity without persisting the brokerage account ID.
        payload["qmt_account_id"] = hashlib.sha256(req.qmt_account_id.encode()).hexdigest()
        request_sha = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.session_factory() as session:
            begin_ledger_transaction(session, req.execution_domain, req.account_alias)
            existing = session.get(InstanceState, req.instance_id)
            if existing is not None:
                state = dict(existing.strategy_state or {})
                receipt = state.get("opening_receipt")
                if receipt:
                    if receipt["request_sha256"] != request_sha:
                        raise APIError(ErrorCode.BAD_REQUEST, "期初请求与不可变回执不同，拒绝覆盖", http_status=409)
                    return AccountInitializationResponseData(**(receipt["response"] | {
                        "idempotent_replay": True, "current_virtual_cash": float(existing.virtual_cash),
                        "current_positions": dict(existing.virtual_positions or {}),
                    }))
                same = (
                    existing.execution_domain == req.execution_domain
                    and existing.account_alias == req.account_alias
                    and existing.ledger_mode == req.ledger_mode
                    and float(existing.virtual_cash) == virtual_cash
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
                    ledger_mode=req.ledger_mode,
                    virtual_cash=virtual_cash,
                    unallocated_cash=unallocated_cash,
                    positions=positions,
                    evidence_sha256=req.evidence_sha256,
                    external_position_count=len(external_positions),
                    idempotent_replay=True,
                )

            instant = self.clock()
            snapshot_at = datetime.fromisoformat(req.snapshot_time)
            age = (instant - snapshot_at).total_seconds()
            if age < -5 or age > self.max_snapshot_age_seconds:
                raise APIError(ErrorCode.BAD_REQUEST, "期初快照过期或来自未来；请重新采集 QMT 快照", http_status=409)
            open_order = session.execute(select(Order.order_id).where(
                Order.execution_domain == req.execution_domain, Order.qmt_account_alias == req.account_alias,
                Order.status.not_in(("FILLED", "CANCELLED", "REJECTED", "NOT_SUBMITTED", "EXPIRED_BY_POLICY")),
            ).limit(1)).first()
            if open_order:
                raise APIError(ErrorCode.BAD_REQUEST, "账户仍有未终局订单；先核实责任再初始化", http_status=409)
            watermark = StrategyCapitalService._physical_watermark(session, req)
            if watermark and snapshot_at < watermark:
                raise APIError(ErrorCode.BAD_REQUEST, "期初快照早于账户最新事实", http_status=409)
            StrategyCapitalService.check_account_attribution(session, req)
            now = instant.isoformat()
            opening_response = dict(instance_id=req.instance_id, execution_domain=req.execution_domain,
                account_alias=req.account_alias, ledger_mode=req.ledger_mode, virtual_cash=virtual_cash,
                unallocated_cash=unallocated_cash, positions=positions, evidence_sha256=req.evidence_sha256,
                external_position_count=len(external_positions), idempotent_replay=False)
            new_state = InstanceState(
                instance_id=req.instance_id,
                execution_domain=req.execution_domain,
                account_alias=req.account_alias,
                ledger_mode=req.ledger_mode,
                virtual_cash=virtual_cash,
                virtual_positions=positions,
                owned_symbols=req.owned_symbols,
                strategy_state={
                    "opening_receipt": {"request_sha256": request_sha, "response": opening_response,
                                        "created_at": now},
                    "reconciliation_status": "ok",
                    "ledger_mode": req.ledger_mode,
                    "initial_allocated_cash": virtual_cash,
                    "initial_allocated_positions_sha256": _positions_sha256(positions),
                    "initial_physical_cash_snapshot": req.qmt_cash,
                    "initial_unallocated_cash_snapshot": unallocated_cash,
                    "initialization_evidence_sha256": req.evidence_sha256,
                    "initial_snapshot_time": req.snapshot_time,
                    "initial_total_asset": req.qmt_total_asset,
                    "initialized_from_qmt_read_only": True,
                    "external_positions_snapshot": external_positions,
                    "external_positions_sha256": _positions_sha256(external_positions),
                    "external_positions_snapshot_time": req.snapshot_time,
                },
                last_update=now,
            )
            session.add(new_state)
            try:
                # Reject an invalid ownership topology in the creating
                # transaction.  It must never become a latent startup bomb.
                validate_no_owned_symbol_overlap(session)
            except OwnershipOverlap as exc:
                raise APIError(
                    ErrorCode.BAD_REQUEST,
                    f"owned_symbols 与同账户实例冲突: {exc}",
                    http_status=409,
                ) from exc
            if req.ledger_mode == "attributed":
                peers = session.execute(select(InstanceState).where(
                    InstanceState.execution_domain == req.execution_domain,
                    InstanceState.account_alias == req.account_alias,
                )).scalars().all()
                if any(peer.instance_id != req.instance_id and peer.ledger_mode != "attributed" for peer in peers):
                    raise APIError(ErrorCode.BAD_REQUEST, "共享账户已有非 attributed 归属，须先完成其迁移", http_status=409)
                if any(float(peer.virtual_cash) < 0 or (peer.strategy_state or {}).get("accounting_hold") for peer in peers):
                    raise APIError(ErrorCode.BAD_REQUEST, "账户已有账务待核问题，不能新增资产认领", http_status=409)
                ledger_cash = sum(float(peer.virtual_cash) for peer in peers)
                if ledger_cash > req.qmt_cash + 1.0:
                    raise APIError(
                        ErrorCode.BAD_REQUEST,
                        "各策略初始现金分配合计超过 QMT 可用现金",
                        http_status=409,
                    )
                ledger_positions: dict[str, int] = {}
                for peer in peers:
                    for symbol, quantity in (peer.virtual_positions or {}).items():
                        ledger_positions[symbol] = (
                            ledger_positions.get(symbol, 0) + int(quantity)
                        )
                overallocated = {
                    symbol: {
                        "allocated": quantity,
                        "qmt": int(req.qmt_positions.get(symbol, 0)),
                    }
                    for symbol, quantity in ledger_positions.items()
                    if quantity > int(req.qmt_positions.get(symbol, 0))
                }
                if overallocated:
                    raise APIError(
                        ErrorCode.BAD_REQUEST,
                        f"各策略初始持仓分配合计超过 QMT 实际持仓: {overallocated}",
                        http_status=409,
                    )
                unallocated_cash = float(req.qmt_cash) - ledger_cash
                external_positions = {symbol: int(quantity) - ledger_positions.get(symbol, 0)
                    for symbol, quantity in req.qmt_positions.items()
                    if int(quantity) - ledger_positions.get(symbol, 0) > 0}
                opening_response.update(unallocated_cash=unallocated_cash,
                                        external_position_count=len(external_positions))
                data = dict(new_state.strategy_state)
                data.update(initial_unallocated_cash_snapshot=unallocated_cash,
                    external_positions_snapshot=external_positions,
                    external_positions_sha256=_positions_sha256(external_positions),
                    opening_receipt={"request_sha256": request_sha, "response": opening_response, "created_at": now})
                new_state.strategy_state = data
            elif session.scalar(select(InstanceState.instance_id).where(
                    InstanceState.execution_domain == req.execution_domain,
                    InstanceState.account_alias == req.account_alias,
                    InstanceState.instance_id != req.instance_id).limit(1)):
                raise APIError(ErrorCode.BAD_REQUEST, "dedicated 初始化不能认领已有其他策略的账户", http_status=409)
            session.commit()
        return AccountInitializationResponseData(
            instance_id=req.instance_id,
            execution_domain=req.execution_domain,
            account_alias=req.account_alias,
            ledger_mode=req.ledger_mode,
            virtual_cash=virtual_cash,
            unallocated_cash=unallocated_cash,
            positions=positions,
            evidence_sha256=req.evidence_sha256,
            external_position_count=len(external_positions),
            idempotent_replay=False,
        )
