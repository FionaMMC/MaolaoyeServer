"""业务错误码 + APIError 基类。"""
from __future__ import annotations

from enum import IntEnum


class ErrorCode(IntEnum):
    """与 API 文档错误码定义一致。"""
    OK                = 0
    AUTH_FAILED       = 1001
    BAD_REQUEST       = 1002
    DUPLICATE_DATE    = 2001
    EMPTY_DATA        = 2002
    NON_TRADING_DAY   = 3001
    STRATEGY_PENDING  = 3002
    NO_ORDERS_MATCHED = 4001
    INTERNAL_ERROR    = 5000


class APIError(Exception):
    """业务异常。FastAPI exception handler 会包装为标准响应。"""

    def __init__(self, code: ErrorCode, message: str, http_status: int = 200):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)
