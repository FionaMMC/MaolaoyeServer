"""V20H adapter 单元测试 — mock ctx + 准备 mini 数据"""
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

# 注意：测试 import adapter 需要 plugins/v20h/data/*.parquet 存在
# 否则跳过外部数据相关的测试


def test_code_conversion():
    from plugins.v20h_adapter import _v20h_to_qmt_code, _qmt_to_v20h_code
    assert _v20h_to_qmt_code("000012") == "000012.SZ"
    assert _v20h_to_qmt_code("600519") == "600519.SH"
    assert _v20h_to_qmt_code("688981") == "688981.SH"
    assert _qmt_to_v20h_code("000012.SZ") == "000012"
    assert _qmt_to_v20h_code("600519.SH") == "600519"


@pytest.mark.skipif(
    not (Path(__file__).parent.parent.parent / "plugins" / "v20h" / "data" /
         "pred_csi1000.parquet").exists(),
    reason="V20H 外部数据未上传，跳过依赖数据的测试"
)
def test_adapter_returns_empty_in_dry_run_mode(tmp_path):
    """有数据时也应该返回空 list（Phase 14a dry-run）。"""
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    store = ParquetStore(root=tmp_path)
    ctx = Context(
        instance_id="real_A_v20h",
        trade_date=20240403,   # pred_csi1000 数据范围内的日期
        virtual_cash=10_000_000.0,
        virtual_positions={},
        parquet_store=store,
    )
    adapter = V20HAdapter()
    signals = adapter.run(ctx, trade_date=20240403)
    # Phase 14a 永远返回空，不下单
    assert signals == []


def test_adapter_handles_missing_external_data():
    """外部数据缺失时应优雅退化，不 crash。"""
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # 临时清空 _cfg 等类属性以触发懒加载（避免被前一个测试缓存）
        V20HAdapter._cfg = None
        V20HAdapter._pred_df = None
        V20HAdapter._v12_series = None

        # mock 外部数据路径不存在的场景：暂时把 adapter 模块的 _V20H_DIR 指到空目录
        import plugins.v20h_adapter as adapter_mod
        original_dir = adapter_mod._V20H_DIR
        adapter_mod._V20H_DIR = Path(tmp)

        try:
            store = ParquetStore(root=Path(tmp))
            ctx = Context(
                instance_id="x", trade_date=20260430,
                virtual_cash=0.0, virtual_positions={},
                parquet_store=store,
            )
            signals = V20HAdapter().run(ctx, 20260430)
            assert signals == []
        finally:
            adapter_mod._V20H_DIR = original_dir
            V20HAdapter._cfg = None
            V20HAdapter._pred_df = None
            V20HAdapter._v12_series = None


def test_adapter_class_attrs():
    from plugins.v20h_adapter import V20HAdapter
    assert V20HAdapter.name == "v20h_v1_3"
