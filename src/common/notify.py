"""企业微信 webhook 推送。失败静默（仅记日志）。"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def notify_wecom(
    webhook: str,
    message: str,
    level: str = "info",
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """向企业微信机器人 webhook 推送文本消息。

    Args:
        level: "info"（提示）或 "alert"（报警，消息前加 [报警] 前缀）

    Returns: True=推送成功，False=失败。不抛异常。
    """
    content = f"[报警] {message}" if level == "alert" else message
    body = {"msgtype": "text", "text": {"content": content}}

    try:
        with httpx.Client(timeout=10, transport=transport) as client:
            resp = client.post(webhook, json=body)
            if resp.status_code != 200:
                logger.warning("wecom 推送 HTTP %s: %s", resp.status_code, resp.text)
                return False
            data = resp.json()
            if data.get("errcode", 0) != 0:
                logger.warning("wecom 推送失败 errcode=%s errmsg=%s",
                               data.get("errcode"), data.get("errmsg"))
                return False
            return True
    except httpx.HTTPError as e:
        logger.warning("wecom 推送异常: %s", e)
        return False
