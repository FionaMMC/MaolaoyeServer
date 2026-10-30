"""Static dashboard pages. They carry no data and load everything with the operator key
the user types in (kept only in the tab's sessionStorage)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
WEB = Path(__file__).resolve().parents[1] / "web"
PAGE = WEB / "oms" / "index.html"
V713_PAGE = WEB / "v713" / "index.html"


def _page(path: Path) -> HTMLResponse:
    # Read per request: a missing page must never prevent the trading API from starting.
    return HTMLResponse(path.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer"})


@router.get("/dashboard/oms", response_class=HTMLResponse, include_in_schema=False)
def oms_dashboard() -> HTMLResponse:
    return _page(PAGE)


@router.get("/dashboard/v713-synthetic", response_class=HTMLResponse, include_in_schema=False)
def v713_synthetic_dashboard() -> HTMLResponse:
    return _page(V713_PAGE)
