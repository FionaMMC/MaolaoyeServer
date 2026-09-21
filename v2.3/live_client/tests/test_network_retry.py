import requests
import pytest

from live_client.network_retry import get_with_retry


def response(status=200):
    result = requests.Response()
    result.status_code = status
    result._content = b'{"code":0,"data":{"orders":[]}}'
    result._content_consumed = True
    return result


def test_timeout_then_success_preserves_request(monkeypatch):
    calls, sleeps = [], []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) < 3:
            raise requests.Timeout()
        return response()

    monkeypatch.setattr(requests, "get", get)
    result = get_with_retry(
        "http://test/orders",
        params={"date": "20260921"},
        timeout=30,
        sleep=sleeps.append,
    )
    assert result.status_code == 200
    assert calls[0] == calls[1] == calls[2]
    assert sleeps == [1, 2]


@pytest.mark.parametrize("status", [401, 403, 409, 500])
def test_permanent_http_errors_not_retried(monkeypatch, status):
    calls = []
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: calls.append(1) or response(status)
    )
    with pytest.raises(requests.HTTPError):
        get_with_retry("http://test/orders", sleep=lambda _: None)
    assert len(calls) == 1


def test_exhaustion_does_not_turn_failure_into_empty_orders(monkeypatch):
    calls = []

    def get(*a, **kw):
        calls.append(1)
        raise requests.ConnectionError()

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(requests.ConnectionError):
        get_with_retry("http://test/orders", sleep=lambda _: None)
    assert len(calls) == 3


def test_503_then_valid_empty_batch_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: calls.append(1) or response(503 if len(calls) == 1 else 200),
    )
    assert (
        get_with_retry("http://test/orders", sleep=lambda _: None).json()["data"][
            "orders"
        ]
        == []
    )
    assert len(calls) == 2
