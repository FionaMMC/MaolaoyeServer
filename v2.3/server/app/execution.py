"""Paper/live 执行域：所有会影响订单或账本的数据都必须显式归域。"""
from __future__ import annotations

from typing import Literal, cast

from app.exceptions import APIError, ErrorCode

ExecutionDomain = Literal["paper", "live"]
EXECUTION_DOMAINS: tuple[ExecutionDomain, ...] = ("paper", "live")


def normalize_execution_domain(value: str) -> ExecutionDomain:
    normalized = value.strip().lower()
    if normalized not in EXECUTION_DOMAINS:
        raise APIError(
            ErrorCode.BAD_REQUEST,
            f"execution_domain 必须是 paper/live，收到 {value!r}",
            http_status=422,
        )
    return cast(ExecutionDomain, normalized)
