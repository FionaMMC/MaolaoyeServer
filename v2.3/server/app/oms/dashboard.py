"""Dashboard page for manual instructions and dividend registration.

The page is static and carries no data; it loads everything from /oms/live/overview
with the operator key the user types in (kept only in the tab's sessionStorage).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
PAGE = Path(__file__).resolve().parents[1] / "web" / "oms" / "index.html"


@router.get("/dashboard/oms", response_class=HTMLResponse, include_in_schema=False)
def oms_dashboard() -> HTMLResponse:
    # Read per request: a missing page must never prevent the trading API from starting.
    return HTMLResponse(PAGE.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer"})
