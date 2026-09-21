"""Fast enqueue/status endpoints. No plugins, strategy computation, or order GET."""
from typing import Literal
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from app.auth import AuthContext, verify_api_key
from app.dependencies import get_session_factory
from app.exceptions import APIError, ErrorCode
from app.schemas.common import APIResponse
from app.services.pipeline_jobs import PipelineJobService
from app.settings import get_settings

router = APIRouter(prefix="/admin/pipeline-jobs")


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_group: str = Field(min_length=1, max_length=100)
    trade_date: int = Field(ge=20000101, le=99991231)
    source: Literal["manual", "automatic"] = "manual"


def service(sf=Depends(get_session_factory), settings=Depends(get_settings)):
    return PipelineJobService(sf, settings.strategies_file)


def authorize(auth, jobs, group):
    try:
        alias = jobs.account_alias(group)
    except (ValueError, OSError) as exc:
        raise APIError(ErrorCode.BAD_REQUEST, str(exc), http_status=400) from exc
    if auth.execution_domain != "paper" or not auth.allows_account(alias):
        raise APIError(ErrorCode.AUTH_FAILED, "paper account group not allowed", http_status=403)


@router.post("", status_code=202, response_model=APIResponse[dict])
def enqueue(request: RecoveryRequest, auth: AuthContext = Depends(verify_api_key),
            jobs=Depends(service)):
    authorize(auth, jobs, request.account_group)
    try:
        result = jobs.enqueue(request.account_group, request.trade_date,
                              manual=request.source == "manual")
    except ValueError as exc:
        raise APIError(ErrorCode.BAD_REQUEST, str(exc), http_status=400) from exc
    return APIResponse(code=0, message="accepted; worker required", data=result)


@router.get("/{job_id}", response_model=APIResponse[dict])
def status(job_id: str, auth: AuthContext = Depends(verify_api_key), jobs=Depends(service)):
    result = jobs.get(job_id)
    if result is None:
        raise APIError(ErrorCode.BAD_REQUEST, "job not found", http_status=404)
    authorize(auth, jobs, result["account_group"])
    return APIResponse(code=0, message="ok", data=result)
