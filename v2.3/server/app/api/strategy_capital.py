"""Additive capital ownership changes. No broker/order operation is called."""
from fastapi import APIRouter, Depends

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_session_factory
from app.exceptions import APIError, ErrorCode
from app.schemas.common import APIResponse
from app.schemas.strategy_capital import CapitalMovementRequest, CapitalMovementResponseData
from app.services.strategy_capital import StrategyCapitalService
from app.settings import Settings, get_settings

router = APIRouter(prefix="/accounts")


@router.post("/capital-movements", response_model=APIResponse[CapitalMovementResponseData])
def capital_movement(
    req: CapitalMovementRequest,
    auth: AuthContext = Depends(verify_api_key),
    sf=Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
):
    if req.execution_domain != auth.execution_domain or not auth.allows_account(req.account_alias):
        raise APIError(ErrorCode.AUTH_FAILED, "资本变动请求跨域/账户", http_status=403)
    # Initialization and trading-generation switches are not allocation gates.
    # Ownership commands change no broker balance and generate no orders.
    data = StrategyCapitalService(
        sf, commission_rate=settings.stock_commission_rate,
        min_commission=settings.stock_min_commission,
    ).apply(req)
    return APIResponse[CapitalMovementResponseData](code=0, message="ok", data=data)
