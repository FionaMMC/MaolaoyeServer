"""POST /admin/run-pipeline 测试"""

_AUTH = {"Authorization": "Bearer TEST_KEY"}


def test_admin_run_pipeline_no_yaml(client, settings_for_test):
    """无 strategies.yaml 时管线应正常退出。"""
    r = client.post("/admin/run-pipeline?trade_date=20260430", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["instances"] == 0


def test_admin_run_pipeline_no_auth_returns_401(client):
    r = client.post("/admin/run-pipeline?trade_date=20260430")
    assert r.status_code == 401


def test_admin_run_pipeline_bad_date(client):
    r = client.post("/admin/run-pipeline?trade_date=abc", headers=_AUTH)
    assert r.json()["code"] == 1002


def test_upload_data_endpoint_happy_path(client, settings_for_test, tmp_path):
    """e2e: 上传 → status 能看到。"""
    import io
    import pandas as pd

    # 先用一个真实存在的 strategy_name (默认插件自带 buy_on_dip_example，但它 data_dir=None)
    # 改用上面 registry 的处理方式 — 这里我们 mock 一个能接收数据的 plugin

    # 因为实际 plugins 加载依赖 settings.plugins_dir，client fixture 已经把它指到 tmp，
    # 所以暂时不会有真 plugin 注册。我们插一个 fake plugin 文件进去：
    plugin_file = settings_for_test.plugins_dir / "_test_data_plugin.py"
    plugin_file.write_text('''
from pathlib import Path
from app.strategy.base import Strategy

class TestUploadPlugin(Strategy):
    name = "test_upload_plugin"
    data_dir = Path(__file__).parent / "_test_data"
    data_files = ["sample.parquet"]
    def run(self, ctx, trade_date):
        return []
''')

    # 重置 registry 缓存
    from app.dependencies import _strategy_registry
    _strategy_registry.cache_clear()

    df = pd.DataFrame([{"x": 1, "y": 2}])
    buf = io.BytesIO()
    df.to_parquet(buf, engine="pyarrow", index=False)
    buf.seek(0)

    r = client.post(
        "/admin/upload-data?strategy=test_upload_plugin&filename=sample.parquet",
        headers=_AUTH,
        files={"file": ("sample.parquet", buf.getvalue(), "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["bytes"] > 0


def test_upload_data_unknown_strategy(client, settings_for_test):
    import io
    r = client.post(
        "/admin/upload-data?strategy=ghost&filename=x.parquet",
        headers=_AUTH,
        files={"file": ("x.parquet", b"data", "application/octet-stream")},
    )
    body = r.json()
    assert body["code"] == 1002
    assert "未注册" in body["message"]


def test_upload_data_no_auth(client):
    import io
    r = client.post(
        "/admin/upload-data?strategy=any&filename=x.parquet",
        files={"file": ("x.parquet", b"data", "application/octet-stream")},
    )
    assert r.status_code == 401


def test_data_status_endpoint(client, settings_for_test):
    plugin_file = settings_for_test.plugins_dir / "_test_status_plugin.py"
    plugin_file.write_text('''
from pathlib import Path
from app.strategy.base import Strategy

class StatusPlugin(Strategy):
    name = "status_plugin"
    data_dir = Path(__file__).parent / "_status_data"
    data_files = ["a.parquet", "b.parquet"]
    def run(self, ctx, trade_date):
        return []
''')

    from app.dependencies import _strategy_registry
    _strategy_registry.cache_clear()

    r = client.get(
        "/admin/data-status?strategy=status_plugin",
        headers=_AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    files = body["data"]["files"]
    assert {f["filename"] for f in files} == {"a.parquet", "b.parquet"}
    assert all(f["exists"] is False for f in files)   # 啥都没上传过
