"""reporter 测试（httpx.MockTransport）"""
from __future__ import annotations

import httpx
import pytest

from src.common.http_client import new_http_client
from src.trade_result.matcher import TradeRecord
from src.trade_result.reporter import report_trade_result


def _rec(sid="s1", qty=100, price=10.0, status="FILLED",
         ft="2026-04-22T09:25:00+08:00") -> TradeRecord:
    return TradeRecord(
        order_id="o1", signal_id=sid, symbol="600519.SH", direction="BUY",
        submitted_quantity=100, filled_quantity=qty, filled_price=price,
        filled_time=ft, status=status,
    )


def _mk_client(handlers: list, base_url="https://srv") -> httpx.Client:
    it = iter(handlers)

    def h(req):
        return next(it)(req)

    return new_http_client(base_url, "K", timeout=10,
                           transport=httpx.MockTransport(h))


def test_report_happy_path():
    captured = {}

    def handler(req):
        import json
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={
            "code": 0, "data": {"trade_date": "20260422", "stage": "auction",
                                "matched_count": 1, "unmatched_signal_ids": []}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    result = report_trade_result(
        trade_date="20260422", stage="auction",
        records=[_rec()],
        http_client=client, max_retries=3, backoff=0,
    )

    assert result.ok is True
    assert result.attempts == 1
    assert result.matched_count == 1
    assert result.unmatched_signal_ids == []

    assert captured["body"]["trade_date"] == "20260422"
    assert captured["body"]["stage"] == "auction"
    assert len(captured["body"]["results"]) == 1
    assert captured["body"]["results"][0]["signal_id"] == "s1"
    assert captured["body"]["results"][0]["filled_time"] == "2026-04-22T09:25:00+08:00"


def test_report_omits_filled_time_when_none():
    captured = {}

    def handler(req):
        import json
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1, "unmatched_signal_ids": []}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    report_trade_result(
        trade_date="20260422", stage="auction",
        records=[_rec(ft=None, qty=0, price=0.0, status="CANCELLED")],
        http_client=client, max_retries=3, backoff=0,
    )

    r0 = captured["body"]["results"][0]
    assert "filled_time" not in r0 or r0["filled_time"] is None
    assert r0["filled_quantity"] == 0
    assert r0["filled_price"] == 0.0


def test_report_retries_on_5xx(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    client = _mk_client([
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1, "unmatched_signal_ids": []}}),
    ])

    r = report_trade_result("20260422", "auction", [_rec()], client,
                            max_retries=3, backoff=0)
    assert r.ok is True
    assert r.attempts == 3


def test_report_4xx_no_retry(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    client = _mk_client([
        lambda req: httpx.Response(401, json={"code": 1001, "message": "auth"}),
    ])

    r = report_trade_result("20260422", "auction", [_rec()], client,
                            max_retries=3, backoff=0)

    assert r.ok is False
    assert r.attempts == 1


def test_report_exhausts_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    client = _mk_client([
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
    ])

    r = report_trade_result("20260422", "auction", [_rec()], client,
                            max_retries=3, backoff=0)

    assert r.ok is False
    assert r.attempts == 3


def test_report_surfaces_unmatched(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "code": 0, "data": {"matched_count": 1,
                                "unmatched_signal_ids": ["s99"]}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    r = report_trade_result("20260422", "auction", [_rec()], client,
                            max_retries=3, backoff=0)

    assert r.ok is True
    assert r.unmatched_signal_ids == ["s99"]


def test_report_empty_records_does_not_post():
    called = []

    def handler(req):
        called.append(1)
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))

    r = report_trade_result("20260422", "auction", [], client,
                            max_retries=3, backoff=0)

    assert r.ok is True
    assert r.attempts == 0
    assert called == []
