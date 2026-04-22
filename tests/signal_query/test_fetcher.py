"""fetcher 测试（用 httpx.MockTransport）"""
from __future__ import annotations

import httpx
import pytest

from src.common.http_client import new_http_client
from src.signal_query.fetcher import FetchResult, fetch_signals


def _signal(sid: str = "sig1", valid: str = "20260422") -> dict:
    return {
        "signal_id": sid,
        "symbol": "600519.SH",
        "direction": "BUY",
        "quantity": 100,
        "order_type": "LIMIT",
        "limit_price": 1540.0,
        "price_offset": 0.005,
        "strategy_id": "s1",
        "signal_time": "2026-04-21T18:30:00+08:00",
        "valid_date": valid,
    }


def _mk_client(handlers: list) -> httpx.Client:
    it = iter(handlers)

    def h(req: httpx.Request) -> httpx.Response:
        return next(it)(req)

    return new_http_client("https://srv", "K", timeout=10,
                           transport=httpx.MockTransport(h))


def test_fetch_has_signals():
    client = _mk_client([
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"date": "20260422", "signals": [_signal()]}}),
    ])

    r = fetch_signals("20260422", client, wait_secs=0)

    assert isinstance(r, FetchResult)
    assert r.state == "HAS_SIGNALS"
    assert len(r.signals) == 1
    assert r.signals[0]["signal_id"] == "sig1"


def test_fetch_empty_signals_returns_no_signals():
    client = _mk_client([
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"date": "20260422", "signals": []}}),
    ])

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "NO_SIGNALS"
    assert r.signals == []


def test_fetch_3002_then_ok(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(200, json={"code": 3002, "message": "pending"}),
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"signals": [_signal()]}}),
    ])

    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    r = fetch_signals("20260422", client, wait_secs=1800)

    assert r.state == "HAS_SIGNALS"
    assert sleep_calls == [1800]


def test_fetch_3002_twice_returns_still_pending(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(200, json={"code": 3002, "message": "pending"}),
        lambda req: httpx.Response(200, json={"code": 3002, "message": "pending"}),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "STILL_PENDING"
    assert r.error is not None and "3002" in r.error


def test_fetch_other_error_returns_error():
    client = _mk_client([
        lambda req: httpx.Response(200, json={"code": 1001, "message": "auth"}),
    ])

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "ERROR"
    assert "1001" in (r.error or "")


def test_fetch_http_500_returns_error():
    client = _mk_client([
        lambda req: httpx.Response(500, text="boom"),
    ])

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "ERROR"
    assert "500" in (r.error or "")


def test_fetch_network_error_returns_error():
    def handler(req):
        raise httpx.ConnectError("no route")

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    r = fetch_signals("20260422", client, wait_secs=0)

    assert r.state == "ERROR"


def test_fetch_uses_query_param_date():
    captured = {}

    def handler(req):
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"code": 0, "data": {"signals": []}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    fetch_signals("20260422", client, wait_secs=0)

    assert "date=20260422" in captured["url"]
    assert "/signals" in captured["url"]
