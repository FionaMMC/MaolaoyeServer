"""POST /trade-result stub tests"""

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def _payload():
    return {
        "trade_date": "20260430",
        "results": [
            {"order_id": "o1", "filled_quantity": 100, "filled_price": 10.5,
             "filled_time": "2026-04-30T09:25:00+08:00", "status": "FILLED"},
        ],
    }


def test_post_trade_result_happy_path(client):
    r = client.post("/trade-result", headers=_AUTH, json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["trade_date"] == "20260430"


def test_post_trade_result_no_auth_returns_401(client):
    r = client.post("/trade-result", json=_payload())
    assert r.status_code == 401


def test_post_trade_result_bad_status_returns_1002(client):
    bad = _payload()
    bad["results"][0]["status"] = "WHATEVER"
    r = client.post("/trade-result", headers=_AUTH, json=bad)
    assert r.json()["code"] == 1002


def test_post_trade_result_empty_results_ok(client):
    r = client.post("/trade-result", headers=_AUTH,
                    json={"trade_date": "20260430", "results": []})
    assert r.json()["code"] == 0
