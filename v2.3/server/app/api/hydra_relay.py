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
    HydraExecutionWaitResponseData,
    HydraAdvanceRequest,
    HydraExecutionPublishRequest,
)
from app.services.hydra_relay import HydraRelayService
from app.services.hydra_execution_advance import advance_execution
from app.services.hydra_execution_publish import publish_execution
from app.schemas.hydra_monthly import HydraMonthlySnapshotRequest
from app.services.hydra_monthly import receive_snapshot
from app.settings import get_settings, Settings

router = APIRouter(prefix="/hydra")


@router.post("/research/snapshots", response_model=APIResponse[dict])
def receive_monthly_snapshot(
    req: HydraMonthlySnapshotRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: HydraRelayService = Depends(get_hydra_relay_service),
    settings: Settings = Depends(get_settings),
):
    _authorize(auth, req.execution_domain, req.account_alias)
    try:
        data = receive_snapshot(service, settings, req)
    except (ValueError, OSError) as exc:
        raise APIError(ErrorCode.BAD_REQUEST, str(exc)) from exc
    return APIResponse(code=0, message="ok", data=data)


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
    response_model=APIResponse[HydraRelayResponseData | HydraExecutionWaitResponseData],
)
def stage_hydra_target(
    req: HydraTargetRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: HydraRelayService = Depends(get_hydra_relay_service),
):
    _authorize(auth, req.execution_domain, req.account_alias)
    data = service.stage_initial(req)
    return APIResponse(code=0, message="ok", data=data)


@router.post(
    "/rebalances/retry",
    response_model=APIResponse[HydraRelayResponseData | HydraExecutionWaitResponseData],
)
def stage_hydra_retry(
    req: HydraRetryRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: HydraRelayService = Depends(get_hydra_relay_service),
):
    _authorize(auth, req.execution_domain, req.account_alias)
    data = service.stage_retry(req)
    return APIResponse(code=0, message="ok", data=data)


@router.post("/execution/advance", response_model=APIResponse[dict])
def advance_hydra_execution(
    req: HydraAdvanceRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: HydraRelayService = Depends(get_hydra_relay_service),
):
    _authorize(auth, req.execution_domain, req.account_alias)
    return APIResponse(code=0, message="ok", data=advance_execution(service, req))


@router.post("/execution/data", response_model=APIResponse[dict])
def publish_hydra_execution(
    req: HydraExecutionPublishRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: HydraRelayService = Depends(get_hydra_relay_service),
):
    _authorize(auth, req.execution_domain, req.account_alias)
    try:
        data = publish_execution(service, req)
    except ValueError as exc:
        raise APIError(ErrorCode.BAD_REQUEST, str(exc)) from exc
    return APIResponse(code=0, message="ok", data=data)


@router.post(
    "/attempts/close",
    response_model=APIResponse[HydraAttemptCloseResponseData],
)
def close_hydra_attempt(
    req: HydraAttemptCloseRequest,
    auth: AuthContext = Depends(verify_api_key),
    service: HydraRelayService = Depends(get_hydra_relay_service),
):
    _authorize(auth, req.execution_domain, req.account_alias)
    data = service.close_attempt(req)
    return APIResponse[HydraAttemptCloseResponseData](code=0, message="ok", data=data)
