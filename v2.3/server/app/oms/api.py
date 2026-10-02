"""/oms/live: the only API through which the Windows agent drives live execution."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.auth import OPERATOR_CLIENT_ID, AuthContext, verify_api_key
from app.dependencies import get_oms_cycle_service, get_oms_manual_service
from app.exceptions import APIError, ErrorCode
from app.oms.cycles import CycleService
from app.oms.manual import AckIn, DividendIn, ManualCancelIn, ManualOrderIn, ManualService
from app.oms.schemas import EventsIn, PlanOut, SnapshotIn
from app.schemas.common import APIResponse
from app.settings import Settings, get_settings

router = APIRouter(prefix="/oms/live")
CHINA = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _authorize(auth: AuthContext, account_alias: str) -> None:
    """The Windows agent: live client token only, never the trigger or the dashboard operator."""
    if (auth.execution_domain != "live" or auth.client_id in ("live-trigger", OPERATOR_CLIENT_ID)
            or not auth.allows_account(account_alias)):
        raise APIError(ErrorCode.AUTH_FAILED, "OMS 仅限 live 客户端访问其授权账户", http_status=403)


def _authorize_operator(auth: AuthContext, account_alias: str) -> None:
    if auth.client_id != OPERATOR_CLIENT_ID or auth.execution_domain != "live" or not auth.allows_account(account_alias):
        raise APIError(ErrorCode.AUTH_FAILED, "仅看板运维密钥可执行人工操作", http_status=403)


def _bad_request(exc: ValueError) -> APIError:
    return APIError(ErrorCode.BAD_REQUEST, str(exc), http_status=400)


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


# ── dashboard operator ─────────────────────────────────────────────────────
@router.get("/overview", response_model=APIResponse[dict])
def get_overview(account_alias: str, auth: AuthContext = Depends(verify_api_key),
                 service: ManualService = Depends(get_oms_manual_service)):
    _authorize_operator(auth, account_alias)
    try:
        return APIResponse[dict](code=0, message="ok", data=service.overview(account_alias))
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/manual/orders", response_model=APIResponse[dict])
def post_manual_order(body: ManualOrderIn, auth: AuthContext = Depends(verify_api_key),
                      service: ManualService = Depends(get_oms_manual_service)):
    _authorize_operator(auth, body.account_alias)
    try:
        return APIResponse[dict](code=0, message="ok", data=service.create_order(body, datetime.now(timezone.utc)))
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/manual/cancels", response_model=APIResponse[dict])
def post_manual_cancel(body: ManualCancelIn, auth: AuthContext = Depends(verify_api_key),
                       service: ManualService = Depends(get_oms_manual_service)):
    _authorize_operator(auth, body.account_alias)
    return APIResponse[dict](code=0, message="ok", data=service.create_cancel(body, datetime.now(timezone.utc)))


@router.post("/dividends/preview", response_model=APIResponse[dict])
def post_dividend_preview(body: DividendIn, auth: AuthContext = Depends(verify_api_key),
                          service: ManualService = Depends(get_oms_manual_service)):
    _authorize_operator(auth, body.account_alias)
    try:
        return APIResponse[dict](code=0, message="ok",
                                 data=service.dividend(body, datetime.now(timezone.utc), apply=False))
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/dividends", response_model=APIResponse[dict])
def post_dividend(body: DividendIn, auth: AuthContext = Depends(verify_api_key),
                  service: ManualService = Depends(get_oms_manual_service)):
    _authorize_operator(auth, body.account_alias)
    try:
        return APIResponse[dict](code=0, message="ok",
                                 data=service.dividend(body, datetime.now(timezone.utc), apply=True))
    except ValueError as exc:
        raise _bad_request(exc) from exc


# ── agent side of manual instructions ──────────────────────────────────────
@router.get("/manual/pending", response_model=APIResponse[dict])
def get_manual_pending(account_alias: str, trade_date: str | None = Query(default=None, pattern=r"^\d{8}$"),
                       auth: AuthContext = Depends(verify_api_key),
                       service: ManualService = Depends(get_oms_manual_service)):
    _authorize(auth, account_alias)
    day = trade_date or datetime.now(timezone.utc).astimezone(CHINA).strftime("%Y%m%d")
    return APIResponse[dict](code=0, message="ok", data=service.pending(account_alias, day))


@router.post("/manual/ack", response_model=APIResponse[dict])
def post_manual_ack(body: AckIn, auth: AuthContext = Depends(verify_api_key),
                    service: ManualService = Depends(get_oms_manual_service)):
    _authorize(auth, body.account_alias)
    return APIResponse[dict](code=0, message="ok", data=service.ack(body, datetime.now(timezone.utc)))
