"""通用响应包装：{code, message, data}。"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

DataT = TypeVar("DataT")


class APIResponse(BaseModel, Generic[DataT]):
    """所有端点统一响应结构。"""
    code: int
    message: str = "ok"
    data: DataT | None = None
