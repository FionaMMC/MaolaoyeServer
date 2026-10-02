"""/oms/live: the only API through which the Windows agent drives live execution."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_oms_cycle_service
from app.exceptions import APIError, ErrorCode
from app.oms.cycles import CycleService
from app.oms.schemas import EventsIn, PlanOut, SnapshotIn
from app.schemas.common import APIResponse
from app.settings import Settings, get_settings

router = APIRouter(prefix="/oms/live")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _authorize(auth: AuthContext, account_alias: str) -> None:
    if auth.execution_domain != "live" or auth.client_id == "live-trigger" or not auth.allows_account(account_alias):
        raise APIError(ErrorCode.AUTH_FAILED, "OMS 仅限 live 客户端访问其授权账户", http_status=403)


@router.post("/snapshot", response_model=APIResponse[dict])
def post_snapshot(snapshot: SnapshotIn, auth: AuthContext = Depends(verify_api_key),
                  service: CycleService = Depends(get_oms_cycle_service)):
    # Accepted with the flag off as well, so the read-only drill reconciles real data.
    _authorize(auth, snapshot.account_alias)
    return APIResponse[dict](code=0, message="ok", data=service.ingest_snapshot(snapshot, _now()))


@router.get("/plan", response_model=APIResponse[PlanOut])
def get_plan(account_alias: str, trade_date: str = Query(pattern=r"^\d{8}$"),
             phase: Literal["SELL", "BUY"] = Query(), auth: AuthContext = Depends(verify_api_key),
             settings: Settings = Depends(get_settings), service: CycleService = Depends(get_oms_cycle_service)):
    _authorize(auth, account_alias)
    try:
        plan = service.plan_for(account_alias, trade_date, phase, executable_flag=settings.oms_live_enabled)
    except LookupError as exc:
        raise APIError(ErrorCode.NO_ORDERS_MATCHED, str(exc), http_status=404) from exc
    return APIResponse[PlanOut](code=0, message="ok", data=plan)


@router.post("/events", response_model=APIResponse[dict])
def post_events(body: EventsIn, auth: AuthContext = Depends(verify_api_key),
                service: CycleService = Depends(get_oms_cycle_service)):
    _authorize(auth, body.account_alias)
    return APIResponse[dict](code=0, message="ok", data=service.ledger.apply_events(body.account_alias, body.events,
                                                                                     _now()))


@router.get("/status", response_model=APIResponse[dict])
def get_status(account_alias: str, auth: AuthContext = Depends(verify_api_key),
               service: CycleService = Depends(get_oms_cycle_service)):
    _authorize(auth, account_alias)
    return APIResponse[dict](code=0, message="ok", data=service.status(account_alias))
