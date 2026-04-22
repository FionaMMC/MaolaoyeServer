"""POST /trade-result 推送，重试 3 次。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from src.trade_result.matcher import TradeRecord

logger = logging.getLogger(__name__)

_ENDPOINT = "/trade-result"


@dataclass
class ReportResult:
    ok: bool
    attempts: int
    matched_count: int | None = None
    unmatched_signal_ids: list[str] = field(default_factory=list)
    error: str | None = None


def _record_to_payload(r: TradeRecord) -> dict:
    d = {
        "signal_id": r.signal_id,
        "symbol": r.symbol,
        "direction": r.direction,
        "filled_quantity": int(r.filled_quantity),
        "filled_price": float(r.filled_price),
        "status": r.status,
    }
    if r.filled_time is not None:
        d["filled_time"] = r.filled_time
    return d


def report_trade_result(
    trade_date: str,
    stage: str,
    records: list[TradeRecord],
    http_client: httpx.Client,
    max_retries: int = 3,
    backoff: int = 2,
) -> ReportResult:
    if not records:
        logger.info("无 trade records 可推送（stage=%s）", stage)
        return ReportResult(ok=True, attempts=0)

    payload = {
        "trade_date": trade_date,
        "stage": stage,
        "results": [_record_to_payload(r) for r in records],
    }

    last_err: str | None = None
    for attempt in range(1, max_retries + 1):
        logger.info("POST %s stage=%s 第 %d/%d 次", _ENDPOINT, stage, attempt, max_retries)
        try:
            resp = http_client.post(_ENDPOINT, json=payload)
        except httpx.HTTPError as e:
            last_err = f"network error: {e}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        if 400 <= resp.status_code < 500:
            try:
                body = resp.json()
                last_err = (f"HTTP {resp.status_code} "
                            f"code={body.get('code')} message={body.get('message')}")
            except Exception:
                last_err = f"HTTP {resp.status_code}"
            return ReportResult(ok=False, attempts=attempt, error=last_err)

        if resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        try:
            body = resp.json()
        except Exception as e:
            last_err = f"response not JSON: {e}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        if body.get("code") != 0:
            last_err = f"code={body.get('code')} message={body.get('message')}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        data = body.get("data") or {}
        return ReportResult(
            ok=True, attempts=attempt,
            matched_count=int(data.get("matched_count", 0)),
            unmatched_signal_ids=list(data.get("unmatched_signal_ids") or []),
        )

    return ReportResult(ok=False, attempts=max_retries, error=last_err)
