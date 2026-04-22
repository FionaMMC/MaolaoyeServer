"""pusher 测试（用 httpx.MockTransport 模拟服务器）"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.common.http_client import new_http_client
from src.market_data_push.pusher import push_market_data


def _mk_client(responses: list, base_url: str = "https://srv") -> httpx.Client:
    """responses 是一个 callable 列表，按顺序返回。"""
    it = iter(responses)

    def handler(req: httpx.Request) -> httpx.Response:
        fn = next(it)
        return fn(req)

    return new_http_client(base_url, "KEY", timeout=10, transport=httpx.MockTransport(handler))


def _ok_response(received: int = 100) -> httpx.Response:
    return httpx.Response(200, json={
        "code": 0, "message": "ok",
        "data": {"trade_date": "20260422", "received_count": received,
                 "strategy_triggered": True},
    })


def test_push_happy_path_first_attempt():
    client = _mk_client([lambda req: _ok_response(3)])
    payload = {"trade_date": "20260422", "stocks": [{}, {}, {}]}

    result = push_market_data(payload, client, max_retries=3, backoff=0)

    assert result.ok is True
    assert result.attempts == 1
    assert result.response_data["received_count"] == 3
    assert result.error is None


def test_push_retries_on_5xx(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(500, text="bad gateway"),
        lambda req: httpx.Response(500, text="bad gateway"),
        lambda req: _ok_response(1),
    ])
    payload = {"trade_date": "20260422", "stocks": [{}]}
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data(payload, client, max_retries=3, backoff=0)

    assert result.ok is True
    assert result.attempts == 3


def test_push_does_not_retry_on_4xx(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(401, json={"code": 1001, "message": "auth"}),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is False
    assert result.attempts == 1
    assert "1001" in (result.error or "") or "401" in (result.error or "")


def test_push_retries_on_non_zero_code(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(200, json={"code": 5000, "message": "server err"}),
        lambda req: httpx.Response(200, json={"code": 5000, "message": "server err"}),
        lambda req: _ok_response(1),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is True
    assert result.attempts == 3


def test_push_exhausts_retries_then_fails(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is False
    assert result.attempts == 3
    assert result.error is not None


def test_push_network_exception_is_retried(monkeypatch):
    attempts = []

    def handler(req):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("no route")
        return _ok_response(1)

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is True
    assert result.attempts == 3


def test_push_received_count_zero_is_failure(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"received_count": 0, "trade_date": "20260422"}}),
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"received_count": 0, "trade_date": "20260422"}}),
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"received_count": 0, "trade_date": "20260422"}}),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is False
