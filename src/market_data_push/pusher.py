"""行情推送与重试。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ENDPOINT = "/market-data"


@dataclass
class PushResult:
    ok: bool
    attempts: int
    response_data: dict[str, Any] | None
    error: str | None


def push_market_data(
    payload: dict[str, Any],
    http_client: httpx.Client,
    max_retries: int = 3,
    backoff: int = 2,
) -> PushResult:
    """推送行情，网络异常 / 5xx / code!=0 重试，4xx 立即失败。"""
    last_err: str | None = None

    for attempt in range(1, max_retries + 1):
        logger.info("POST %s 第 %d/%d 次尝试", _ENDPOINT, attempt, max_retries)
        try:
            resp = http_client.post(_ENDPOINT, json=payload)
        except httpx.HTTPError as e:
            last_err = f"network error: {e}"
            logger.warning("第 %d 次网络异常: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        if 400 <= resp.status_code < 500:
            try:
                body = resp.json()
                last_err = f"HTTP {resp.status_code} code={body.get('code')} message={body.get('message')}"
            except Exception:
                last_err = f"HTTP {resp.status_code} (body not JSON)"
            logger.error("4xx 不重试：%s", last_err)
            return PushResult(ok=False, attempts=attempt,
                              response_data=None, error=last_err)

        if resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code}"
            logger.warning("第 %d 次 5xx: %s", attempt, last_err)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        # 2xx
        try:
            body = resp.json()
        except Exception as e:
            last_err = f"response not JSON: {e}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        code = body.get("code")
        if code != 0:
            last_err = f"biz code={code} message={body.get('message')}"
            logger.warning("第 %d 次业务失败: %s", attempt, last_err)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        data = body.get("data") or {}
        received = int(data.get("received_count", 0))
        if received <= 0:
            last_err = "received_count=0（服务器未入库任何数据）"
            logger.warning("第 %d 次 received_count=0", attempt)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        logger.info("推送成功：received_count=%d, strategy_triggered=%s",
                    received, data.get("strategy_triggered"))
        return PushResult(ok=True, attempts=attempt,
                          response_data=data, error=None)

    return PushResult(ok=False, attempts=max_retries,
                      response_data=None, error=last_err)
