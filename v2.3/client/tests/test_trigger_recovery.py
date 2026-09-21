"""Synthetic Windows/client transport tests; no private config or xtquant."""
import importlib.util
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import requests


@pytest.fixture
def module(monkeypatch):
    config = SimpleNamespace(SERVER_BASE_URL="http://test", API_KEY="synthetic",
        setup_logger=lambda _: logging.getLogger("test"), notify_wecom=lambda *a, **k: True)
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setitem(sys.modules, "xtquant", SimpleNamespace(xtdata=SimpleNamespace()))
    spec = importlib.util.spec_from_file_location("trigger_under_test", Path(__file__).parents[1] / "trigger_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "startup_check", lambda *_: None)
    monkeypatch.setattr(sys, "argv", ["trigger", "--date", "20260922"])
    return module


def response(status, data):
    import json
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({"code": 0, "data": data}).encode()
    return result


def test_legacy_notification_never_fetches_orders(module, monkeypatch):
    monkeypatch.setattr(module, "trigger", lambda *a, **k: {
        "trade_date": 20260922, "instances": 1, "signals": 9, "passed": 9, "orders": 9})
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("notification must not claim orders"))
    module.main()


@pytest.mark.parametrize("reason", ["already_fetched", "already_settled", "recovery_batch_published"])
def test_skipped_existing_is_not_data_failure(module, monkeypatch, reason):
    monkeypatch.setattr(module, "trigger", lambda *a, **k: {"skipped": reason})
    messages = []
    monkeypatch.setattr(module, "_wechat_notify", messages.append)
    module.main()
    assert "保留已有批次" in messages[0]
    assert "信号 0" not in messages[0]


def test_business_block_is_failure(module, monkeypatch):
    monkeypatch.setattr(module, "trigger", lambda *a, **k: {"skipped": "strict_rebalance_blocked"})
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 1


def test_recovery_post_timeout_reuses_identical_payload(module, monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs["json"])
        if len(calls) == 1:
            raise requests.Timeout()
        return response(202, {"job_id": "same", "status": "QUEUED"})
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    job = module.trigger_recovery("20260922", "paper_v53", wait_seconds=0)
    assert job["job_id"] == "same" and calls[0] == calls[1]


def test_auth_failure_not_retried(module, monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: (calls.append(1) or response(403, {})))
    with pytest.raises(requests.HTTPError):
        module.trigger_recovery("20260922", "paper_v53", wait_seconds=0)
    assert len(calls) == 1


def test_poll_only_job_endpoint_and_disconnect_keeps_job(module, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: response(202, {"job_id": "same", "status": "QUEUED"}))
    urls = []
    def get(url, **kwargs):
        urls.append(url)
        raise requests.ConnectionError()
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    job = module.trigger_recovery("20260922", "paper_v53")
    assert job["status"] == "QUEUED"
    assert urls == ["http://test/admin/pipeline-jobs/same"]
    assert module.report_recovery(job) == 3


def test_manual_never_calls_live_route(module, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["trigger", "--manual", "--live", "--account-group", "live", "--date", "20260922"])
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 2
