"""Reviewed shared-account accounting operations (no broker calls)."""
from fastapi import APIRouter, Depends
from app.auth import AuthContext, OPERATOR_CLIENT_ID, verify_api_key
from app.dependencies import get_session_factory
from app.exceptions import APIError, ErrorCode
from app.schemas.accounting_operations import (FeeAdjustmentRequest, CapitalTransferRequest,
    CorporateActionIssueRequest, CorporateActionResolutionRequest)
from app.schemas.common import APIResponse
from app.services.accounting_operations import AccountingOperationsService

router = APIRouter(prefix="/accounts")


def _authorize(auth, req):
    if (auth.execution_domain != req.execution_domain or not auth.allows_account(req.account_alias)
            or (req.execution_domain == "live" and auth.client_id != OPERATOR_CLIENT_ID)):
        raise APIError(ErrorCode.AUTH_FAILED, "accounting commands require the account operator", http_status=403)


@router.post("/fee-adjustments", response_model=APIResponse[dict])
def fees(req: FeeAdjustmentRequest, auth: AuthContext = Depends(verify_api_key), sf=Depends(get_session_factory)):
    _authorize(auth, req)
    return APIResponse[dict](code=0, message="ok", data=AccountingOperationsService(sf).adjust_fees(req))


@router.post("/capital-transfers", response_model=APIResponse[dict])
def transfers(req: CapitalTransferRequest, auth: AuthContext = Depends(verify_api_key), sf=Depends(get_session_factory)):
    _authorize(auth, req)
    return APIResponse[dict](code=0, message="ok", data=AccountingOperationsService(sf).transfer_capital(req))


@router.post("/corporate-action-issues", response_model=APIResponse[dict])
def issues(req: CorporateActionIssueRequest, auth: AuthContext = Depends(verify_api_key), sf=Depends(get_session_factory)):
    _authorize(auth, req)
    return APIResponse[dict](code=0, message="ok", data=AccountingOperationsService(sf).register_issue(req))


@router.post("/corporate-action-resolutions", response_model=APIResponse[dict])
def resolutions(req: CorporateActionResolutionRequest, auth: AuthContext = Depends(verify_api_key), sf=Depends(get_session_factory)):
    _authorize(auth, req)
    return APIResponse[dict](code=0, message="ok", data=AccountingOperationsService(sf).resolve_issue(req))
