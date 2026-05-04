"""POST /market-data tests"""
import pandas as pd

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


def test_post_market_data_empty_arrays_returns_2002(client):
    r = client.post("/market-data", headers=_AUTH,
                    json={"trade_date": "20260430", "stocks": [],
                          "indexes": [], "etfs": []})
    body = r.json()
    assert body["code"] == 2002


def test_post_market_data_writes_parquet(client, settings_for_test):
    """e2e: 端到端发请求 → 检查 parquet 文件实际写出。"""
    r = client.post("/market-data", headers=_AUTH, json={
        "trade_date": "20260430",
        "stocks": [{
            "symbol": "600519.SH",
            "open": 1500.0, "high": 1520.0, "low": 1490.0, "close": 1510.0,
            "volume": 1234, "amount": 1865000.0,
            "is_suspended": False,
        }],
        "indexes": [{
            "symbol": "000300.SH",
            "open": 3800.0, "high": 3850.0, "low": 3790.0, "close": 3820.0,
        }],
        "etfs": [],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["received"] == {"stocks": 1, "indexes": 1, "etfs": 0}

    p_stock = settings_for_test.parquet_root / "market" / "daily" / "stocks" / "600519.SH.parquet"
    p_index = settings_for_test.parquet_root / "market" / "daily" / "indexes" / "000300.SH.parquet"
    assert p_stock.exists()
    assert p_index.exists()
    df = pd.read_parquet(p_stock)
    assert df["close"].iloc[0] == 1510.0
    assert df["suspendFlag"].iloc[0] == 0


def test_post_market_data_duplicate_returns_2001(client):
    payload = {
        "trade_date": "20260430",
        "stocks": [{
            "symbol": "A.SH", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
            "volume": 1, "amount": 1.0, "is_suspended": False,
        }],
        "indexes": [], "etfs": [],
    }
    r1 = client.post("/market-data", headers=_AUTH, json=payload)
    assert r1.json()["code"] == 0
    r2 = client.post("/market-data", headers=_AUTH, json=payload)
    body = r2.json()
    assert body["code"] == 2001
