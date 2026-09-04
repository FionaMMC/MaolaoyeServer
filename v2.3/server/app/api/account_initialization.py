"""POST /accounts/initialize-from-qmt — 单次只读快照初始化。"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_account_initialization_service, get_session_factory
from app.exceptions import APIError, ErrorCode
from app.models import CashFlowJournal, InstanceState
from app.schemas.account_initialization import (
    AccountInitializationRequest,
    AccountInitializationResponseData,
    StrategyLedgerResponseData,
)
from app.schemas.common import APIResponse
from app.services.account_initialization import AccountInitializationService
from app.settings import Settings, get_settings

router = APIRouter(prefix="/accounts")


@router.get(
    "/strategy-ledger",
    response_model=APIResponse[StrategyLedgerResponseData],
)
async def strategy_ledger(
    instance_id: str = Query(min_length=1, max_length=200),
    account_alias: str = Query(min_length=1, max_length=100),
    auth: AuthContext = Depends(verify_api_key),
    sf=Depends(get_session_factory),
):
    """Return only one authorized strategy's attributed ledger; never QMT secrets."""
    if not auth.allows_account(account_alias):
        raise APIError(ErrorCode.AUTH_FAILED, "无权读取该策略账本", http_status=403)
    with sf() as session:
        state = session.get(InstanceState, instance_id)
        if (
            state is None
            or state.execution_domain != auth.execution_domain
            or state.account_alias != account_alias
        ):
            raise APIError(ErrorCode.BAD_REQUEST, "策略账本不存在或跨域", http_status=404)
        flows = session.execute(select(CashFlowJournal).where(
            CashFlowJournal.execution_domain == auth.execution_domain,
            CashFlowJournal.account_alias == account_alias,
            CashFlowJournal.instance_id == instance_id,
            CashFlowJournal.status == "APPLIED",
        )).scalars().all()
        totals: dict[str, float] = {}
        for row in flows:
            totals[row.event_type] = totals.get(row.event_type, 0.0) + float(row.amount)
        strategy_state = dict(state.strategy_state or {})
        data = StrategyLedgerResponseData(
            instance_id=state.instance_id,
            execution_domain=state.execution_domain,
            account_alias=state.account_alias,
            ledger_mode=state.ledger_mode,
            virtual_cash=float(state.virtual_cash),
            positions={
                symbol: int(quantity)
                for symbol, quantity in (state.virtual_positions or {}).items()
                if int(quantity) > 0
            },
            owned_symbols=state.owned_symbols,
            initial_allocated_cash=strategy_state.get("initial_allocated_cash"),
            cash_flow_totals=totals,
            last_update=state.last_update,
        )
    return APIResponse[StrategyLedgerResponseData](code=0, message="ok", data=data)


@router.post(
    "/initialize-from-qmt",
    response_model=APIResponse[AccountInitializationResponseData],
)
async def initialize_from_qmt(
    req: AccountInitializationRequest,
    auth: AuthContext = Depends(verify_api_key),
    settings: Settings = Depends(get_settings),
    service: AccountInitializationService = Depends(get_account_initialization_service),
):
    if req.execution_domain != auth.execution_domain or not auth.allows_account(
        req.account_alias
    ):
        raise APIError(ErrorCode.AUTH_FAILED, "QMT 初始化请求跨域/账户", http_status=403)
    if req.execution_domain == "live":
        if not settings.live_account_initialization_enabled:
            raise APIError(
                ErrorCode.STRATEGY_PENDING,
                "live 账户初始化闸门关闭",
                http_status=423,
            )
        fingerprint = hashlib.sha256(req.qmt_account_id.encode()).hexdigest()
        if (
            not settings.live_qmt_account_sha256
            or fingerprint != settings.live_qmt_account_sha256
        ):
            raise APIError(
                ErrorCode.AUTH_FAILED,
                "QMT account fingerprint 不匹配",
                http_status=403,
            )
    data = service.initialize(req)
    return APIResponse[AccountInitializationResponseData](code=0, message="ok", data=data)
