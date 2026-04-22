"""src.common.notify 测试"""
from __future__ import annotations

import httpx

from src.common.notify import notify_wecom


def test_notify_success_returns_true():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    transport = httpx.MockTransport(handler)

    ok = notify_wecom(
        webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xx",
        message="行情推送成功",
        level="info",
        transport=transport,
    )

    assert ok is True
    assert captured["body"]["msgtype"] == "text"
    assert captured["body"]["text"]["content"] == "行情推送成功"


def test_notify_alert_level_prefixes_message():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={"errcode": 0})

    transport = httpx.MockTransport(handler)
    notify_wecom("https://x", "行情推送失败", level="alert", transport=transport)
    assert captured["body"]["text"]["content"].startswith("[报警]")


def test_notify_http_error_returns_false_no_raise():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    transport = httpx.MockTransport(handler)

    ok = notify_wecom("https://x", "hi", level="info", transport=transport)
    assert ok is False


def test_notify_network_exception_returns_false_no_raise():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    transport = httpx.MockTransport(handler)

    ok = notify_wecom("https://x", "hi", level="info", transport=transport)
    assert ok is False
