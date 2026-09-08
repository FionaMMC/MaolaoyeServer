"""QMT cash fact ingestion, with identity checks but no trading/approval switch."""
from fastapi import APIRouter, Depends

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_session_factory
from app.exceptions import APIError, ErrorCode
from app.schemas.account_cash_observation import (
    AccountCashObservationRequest,
    AccountCashObservationResponseData,
)
from app.schemas.common import APIResponse
from app.services.account_cash_observation import AccountCashObservationService

router = APIRouter(prefix="/accounts")


@router.post("/cash-observations", response_model=APIResponse[AccountCashObservationResponseData])
def cash_observation(
    req: AccountCashObservationRequest,
    auth: AuthContext = Depends(verify_api_key),
    sf=Depends(get_session_factory),
):
    if req.execution_domain != auth.execution_domain or not auth.allows_account(req.account_alias):
        raise APIError(ErrorCode.AUTH_FAILED, "现金事实请求跨域/账户", http_status=403)
    data = AccountCashObservationService(sf).record(req)
    return APIResponse[AccountCashObservationResponseData](code=0, message="ok", data=data)
