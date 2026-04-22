"""GET /signals 三态处理"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

FetchState = Literal["HAS_SIGNALS", "NO_SIGNALS", "STILL_PENDING", "ERROR"]


@dataclass
class FetchResult:
    state: FetchState
    signals: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _parse_response(resp: httpx.Response) -> tuple[str, list[dict], str | None]:
    if resp.status_code >= 500:
        return ("ERROR", [], f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception as e:
        return ("ERROR", [], f"response not JSON: {e}")

    code = body.get("code")
    if code == 3002:
        return ("PENDING", [], f"code=3002 {body.get('message')}")
    if code != 0:
        return ("ERROR", [], f"code={code} message={body.get('message')}")

    signals = (body.get("data") or {}).get("signals") or []
    if not signals:
        return ("NO_SIGNALS", [], None)
    return ("HAS_SIGNALS", signals, None)


def fetch_signals(
    date: str,
    http_client: httpx.Client,
    wait_secs: int = 1800,
) -> FetchResult:
    """查询 /signals?date=X。PENDING 时等待 wait_secs 后重试一次。"""
    try:
        resp = http_client.get("/signals", params={"date": date})
    except httpx.HTTPError as e:
        return FetchResult(state="ERROR", error=f"network error: {e}")

    state, signals, err = _parse_response(resp)
    if state in ("HAS_SIGNALS", "NO_SIGNALS", "ERROR"):
        logger.info("fetch_signals 首次 state=%s", state)
        return FetchResult(state=state, signals=signals, error=err)

    logger.info("服务器策略未完成（3002），等待 %ds 后重试一次", wait_secs)
    time.sleep(wait_secs)

    try:
        resp2 = http_client.get("/signals", params={"date": date})
    except httpx.HTTPError as e:
        return FetchResult(state="ERROR", error=f"retry network error: {e}")

    state2, signals2, err2 = _parse_response(resp2)
    if state2 == "PENDING":
        return FetchResult(state="STILL_PENDING",
                           error=f"重试后仍 3002: {err2}")
    if state2 == "ERROR":
        return FetchResult(state="ERROR", error=f"重试后错误: {err2}")
    return FetchResult(state=state2, signals=signals2, error=err2)
