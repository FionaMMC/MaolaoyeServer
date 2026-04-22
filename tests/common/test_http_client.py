"""src.common.http_client 测试"""
from __future__ import annotations

import httpx

from src.common.http_client import new_http_client


def test_client_has_bearer_auth_header():
    client = new_http_client("https://api.example.com", "KEY123", timeout=10)
    assert client.headers["Authorization"] == "Bearer KEY123"
    assert client.headers["Content-Type"] == "application/json"


def test_client_strips_trailing_slash_from_base_url():
    client = new_http_client("https://api.example.com/", "K", timeout=10)
    assert str(client.base_url).rstrip("/") == "https://api.example.com"


def test_client_timeout_is_set():
    client = new_http_client("https://api.example.com", "K", timeout=15)
    assert client.timeout.connect == 15
    assert client.timeout.read == 15


def test_client_sends_auth_on_real_request():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("Authorization")
        captured["path"] = req.url.path
        return httpx.Response(200, json={"code": 0, "message": "ok", "data": {}})

    transport = httpx.MockTransport(handler)
    client = new_http_client("https://api.example.com", "KEY123", timeout=10,
                             transport=transport)
    resp = client.post("/market-data", json={"ok": True})
    assert resp.status_code == 200
    assert captured["auth"] == "Bearer KEY123"
    assert captured["path"] == "/market-data"
