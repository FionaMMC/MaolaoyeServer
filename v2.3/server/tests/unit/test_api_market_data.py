"""POST /market-data stub tests"""

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def _payload():
    return {
        "trade_date": "20260430",
        "stocks": [{
            "symbol": "600519.SH",
            "open": 1500.0, "high": 1520.0, "low": 1490.0, "close": 1510.0,
            "volume": 1000, "amount": 1510000.0,
            "is_suspended": False,
        }],
        "indexes": [],
        "etfs": [],
    }


def test_post_market_data_happy_path(client):
    r = client.post("/market-data", headers=_AUTH, json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["received"]["stocks"] == 1


def test_post_market_data_no_auth_returns_401(client):
    r = client.post("/market-data", json=_payload())
    assert r.status_code == 401
    assert r.json()["code"] == 1001


def test_post_market_data_bad_payload_returns_1002(client):
    r = client.post("/market-data", headers=_AUTH, json={"trade_date": "bad"})
    body = r.json()
    assert body["code"] == 1002


def test_post_market_data_empty_arrays_ok(client):
    r = client.post("/market-data", headers=_AUTH,
                    json={"trade_date": "20260430", "stocks": [],
                          "indexes": [], "etfs": []})
    assert r.json()["code"] == 0
