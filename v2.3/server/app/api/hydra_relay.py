"""Domain/account-scoped Hydra target 与 residual attempt API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_hydra_relay_service
from app.exceptions import APIError, ErrorCode
from app.schemas.common import APIResponse
from app.schemas.hydra_relay import (
    HydraAttemptCloseRequest,
    HydraAttemptCloseResponseData,
    HydraRelayResponseData,
    HydraRetryRequest,
    HydraTargetRequest,
)
from app.services.hydra_relay import HydraRelayService

router = APIRouter(prefix="/hydra")


def _authorize(auth: AuthContext, execution_domain: str, account_alias: str) -> None:
    if execution_domain != auth.execution_domain:
        raise APIError(
            ErrorCode.AUTH_FAILED,
            "Hydra 请求 execution_domain 与 token 身份不一致",
            http_status=403,
        )
    if not auth.allows_account(account_alias):
        raise APIError(
            ErrorCode.AUTH_FAILED,
            "token 无权访问该 Hydra account_alias",
            http_status=403,
        )


@router.post(
    "/targets/stage",
    response_model=APIResponse[HydraRelayResponseData],
)
async def stage_hydra_target(
    req: HydraTargetRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: HydraRelayService = Depends(get_hydra_relay_service),
):
    _authorize(auth, req.execution_domain, req.account_alias)
    data = service.stage_initial(req)
    return APIResponse[HydraRelayResponseData](code=0, message="ok", data=data)


@router.post(
    "/rebalances/retry",
    response_model=APIResponse[HydraRelayResponseData],
)
async def stage_hydra_retry(
    req: HydraRetryRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: HydraRelayService = Depends(get_hydra_relay_service),
):
    _authorize(auth, req.execution_domain, req.account_alias)
    data = service.stage_retry(req)
    return APIResponse[HydraRelayResponseData](code=0, message="ok", data=data)


@router.post(
    "/attempts/close",
    response_model=APIResponse[HydraAttemptCloseResponseData],
)
async def close_hydra_attempt(
    req: HydraAttemptCloseRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: HydraRelayService = Depends(get_hydra_relay_service),
):
    _authorize(auth, req.execution_domain, req.account_alias)
    data = service.close_attempt(req)
    return APIResponse[HydraAttemptCloseResponseData](code=0, message="ok", data=data)
