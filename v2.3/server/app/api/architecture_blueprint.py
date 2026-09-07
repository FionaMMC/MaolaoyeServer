"""Business-facing architecture explorer; simulations never call trading services.

The public material contains illustrative amounts only. Optional shared notes reuse
the existing review store in a separate session and existing admin authentication.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import sessionmaker

from app.auth import verify_api_key
from app.dependencies import get_session_factory
from app.exceptions import APIError, ErrorCode
from app.schemas.architecture_review import (
    ArchitectureReviewCommentCreate,
    ArchitectureReviewCommentItem,
    ArchitectureReviewDecisionItem,
    ArchitectureReviewDecisionUpsert,
    ArchitectureReviewSessionData,
)
from app.schemas.common import APIResponse
from app.services.architecture_review import ArchitectureReviewService

router = APIRouter()
ASSET_ROOT = Path(__file__).resolve().parents[1] / "web" / "blueprint"
SESSION_ID = "three-books-blueprint-20260907-v1"
ASSETS = {
    "blueprint.css": "text/css",
    "blueprint.js": "text/javascript",
    "model.js": "text/javascript",
}


@lru_cache(maxsize=1)
def blueprint_catalog() -> dict:
    return json.loads((ASSET_ROOT / "catalog.json").read_text(encoding="utf-8"))


def blueprint_item_ids() -> set[str]:
    catalog = blueprint_catalog()
    return {
        item["id"]
        for group in ("stages", "gates", "revisions")
        for item in catalog[group]
    }


def get_blueprint_review_service(
    sf: sessionmaker = Depends(get_session_factory),
) -> ArchitectureReviewService:
    return ArchitectureReviewService(
        sf, session_id=SESSION_ID, allowed_item_ids=blueprint_item_ids(),
    )


@router.get("/dashboard/blueprint", response_class=HTMLResponse, include_in_schema=False)
def blueprint_page():
    # Read only when the page is requested: a missing UI asset must not prevent
    # the trading API process from starting. No database or QMT dependency here.
    catalog = json.dumps(blueprint_catalog(), ensure_ascii=False).replace("<", "\\u003c")
    page = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        page.replace("__BLUEPRINT_CATALOG__", catalog),
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/dashboard/blueprint/assets/{asset_name}", include_in_schema=False)
def blueprint_asset(asset_name: str):
    if asset_name not in ASSETS:
        raise APIError(ErrorCode.BAD_REQUEST, "未知页面资源", http_status=404)
    return FileResponse(
        ASSET_ROOT / asset_name, media_type=ASSETS[asset_name],
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@router.get(
    "/admin/architecture-blueprint/session",
    response_model=APIResponse[ArchitectureReviewSessionData],
    dependencies=[Depends(verify_api_key)],
)
def blueprint_session(
    service: ArchitectureReviewService = Depends(get_blueprint_review_service),
):
    return APIResponse(code=0, message="ok", data=service.snapshot())


@router.post(
    "/admin/architecture-blueprint/comments",
    response_model=APIResponse[ArchitectureReviewCommentItem],
    dependencies=[Depends(verify_api_key)],
)
def blueprint_comment(
    payload: ArchitectureReviewCommentCreate,
    service: ArchitectureReviewService = Depends(get_blueprint_review_service),
):
    try:
        result = service.add_comment(payload)
    except ValueError as exc:
        raise APIError(ErrorCode.BAD_REQUEST, str(exc), http_status=404) from exc
    return APIResponse(code=0, message="ok", data=result)


@router.put(
    "/admin/architecture-blueprint/decisions/{item_id}",
    response_model=APIResponse[ArchitectureReviewDecisionItem],
    dependencies=[Depends(verify_api_key)],
)
def blueprint_decision(
    item_id: str,
    payload: ArchitectureReviewDecisionUpsert,
    service: ArchitectureReviewService = Depends(get_blueprint_review_service),
):
    try:
        result = service.upsert_decision(item_id, payload)
    except ValueError as exc:
        raise APIError(ErrorCode.BAD_REQUEST, str(exc), http_status=404) from exc
    return APIResponse(code=0, message="ok", data=result)
