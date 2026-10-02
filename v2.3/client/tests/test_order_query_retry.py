"""No broker or private config required for transport failure regression."""

import importlib.util
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import requests


def load(monkeypatch):
    config = SimpleNamespace(
        QMT_USERDATA_DIR="unused",
        SERVER_BASE_URL="http://test",
        API_KEY="synthetic",
        PUSH_MODE="server",
        FORCE_TRADE_DATE="20260921",
        setup_logger=lambda _: logging.getLogger("test"),
        notify_wecom=lambda *a, **k: True,
    )
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setitem(
        sys.modules, "xtquant", SimpleNamespace(xtdata=SimpleNamespace())
    )
    spec = importlib.util.spec_from_file_location(
        "query_retry_test", Path(__file__).parents[1] / "order_query.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paper_query_reports_failed_exit_and_does_not_write_orders(monkeypatch):
    module = load(monkeypatch)
    monkeypatch.setattr(module, "startup_check", lambda: None)
    monkeypatch.setattr(module, "_init_server_orders_table", lambda: None)
    monkeypatch.setattr(module, "_fetch_from_server", lambda _: None)
    monkeypatch.setattr(
        module,
        "_write_server_orders",
        lambda *a: pytest.fail("must not write a false empty batch"),
    )
    with pytest.raises(RuntimeError, match="retries exhausted"):
        module.main()


def test_paper_fetch_uses_bounded_retry(monkeypatch):
    module = load(monkeypatch)
    calls = []

    def get(url, **kw):
        calls.append(kw)
        if len(calls) < 3:
            raise requests.Timeout()
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"code":0,"data":{"orders":[]}}'
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests, "get", get)
    from live_client.network_retry import get_with_retry

    monkeypatch.setattr(
        module,
        "get_with_retry",
        lambda *a, **kw: get_with_retry(*a, sleep=lambda _: None, **kw),
    )
    assert module._fetch_from_server("20260921")["code"] == 0
    assert len(calls) == 3
