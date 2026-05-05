"""V20H 完整管线 e2e: 推行情 → 上传外部数据 → run-pipeline → GET /orders 看到订单。"""
from pathlib import Path

import io
import pandas as pd
import pytest

_AUTH = {"Authorization": "Bearer TEST_KEY"}


@pytest.fixture
def v20h_plugin_in_test_dir(settings_for_test):
    """在 settings 的 plugins_dir 下放一个最小 V20H 风格 plugin。"""
    plugins = settings_for_test.plugins_dir
    plugins.mkdir(parents=True, exist_ok=True)

    plugin_code = '''
from pathlib import Path
from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context

class FakeV20H(Strategy):
    name = "fake_v20h"
    data_dir = Path(__file__).parent / "_fake_v20h_data"
    data_files = ["pred.parquet"]
    def run(self, ctx, trade_date):
        pred_path = self.data_dir / "pred.parquet"
        if not pred_path.exists():
            return []
        df = ctx.market("600519.SH", fields=["close"])
        if df.empty:
            return []
        close = float(df["close"].iloc[-1])
        return [RawSignal(
            symbol="600519.SH", direction="BUY", quantity=100,
            reference_price=close, price_offset=0.005,
        )]
'''
    (plugins / "fake_v20h_adapter.py").write_text(plugin_code, encoding="utf-8")

    # strategies.yaml 配一个对应实例
    strategies_yaml = settings_for_test.strategies_file
    strategies_yaml.write_text(
        """
account_groups:
  - group_id: paper_test
    qmt_account_id: ""
    strategies:
      - strategy_id: fake_v20h
        virtual_initial_cash: 1000000
""",
        encoding="utf-8",
    )

    # 重置缓存，确保 registry 重新扫描
    from app.dependencies import _strategy_registry
    _strategy_registry.cache_clear()

    yield

    # 清理：避免影响其他测试
    from app.dependencies import _strategy_registry
    _strategy_registry.cache_clear()


def test_full_pipeline_with_data_upload(client, settings_for_test,
                                         v20h_plugin_in_test_dir):
    """1. 推行情 → 2. 上传外部数据 → 3. run-pipeline → 4. GET /orders 应非空。"""
    # 1. 推行情
    r = client.post("/market-data", headers=_AUTH, json={
        "trade_date": "20240403",
        "stocks": [{
            "symbol": "600519.SH", "open": 1490, "high": 1510,
            "low": 1485, "close": 1500,
            "volume": 1000, "amount": 1500000, "is_suspended": False,
        }],
        "indexes": [], "etfs": [],
    })
    assert r.json()["code"] == 0

    # 2. 上传外部数据
    df = pd.DataFrame([{"date": "20240403", "value": 0.5}])
    buf = io.BytesIO()
    df.to_parquet(buf, engine="pyarrow", index=False)

    r = client.post(
        "/admin/upload-data?strategy=fake_v20h&filename=pred.parquet",
        headers=_AUTH,
        files={"file": ("pred.parquet", buf.getvalue(), "application/octet-stream")},
    )
    assert r.json()["code"] == 0

    # 3. 触发 pipeline
    r = client.post("/admin/run-pipeline?trade_date=20240403", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    summary = body["data"]
    assert summary["instances"] == 1
    assert summary["signals"] >= 1
    assert summary["passed"] >= 1
    assert summary["orders"] >= 1

    # 4. GET /orders 拿到订单
    r = client.get("/orders?date=20240403", headers=_AUTH)
    body = r.json()
    assert body["code"] == 0
    orders = body["data"]["orders"]
    assert len(orders) == 1
    assert orders[0]["symbol"] == "600519.SH"
    assert orders[0]["direction"] == "BUY"
    assert orders[0]["quantity"] == 100
