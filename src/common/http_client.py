"""共享 HTTP Client：httpx 同步 Client，带 Bearer 鉴权。"""
from __future__ import annotations

import httpx


def new_http_client(
    base_url: str,
    api_key: str,
    timeout: int,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """构造预置鉴权与超时的 httpx Client。

    Args:
        transport: 测试时传入 httpx.MockTransport；生产环境传 None。
    """
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
        transport=transport,
    )
