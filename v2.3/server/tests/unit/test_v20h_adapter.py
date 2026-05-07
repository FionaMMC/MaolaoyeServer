"""V20H adapter 单元测试 — Phase 14c（实盘）"""
from pathlib import Path

import pandas as pd
import pytest


def test_code_conversion():
    from plugins.v20h_adapter import _v20h_to_qmt_code, _qmt_to_v20h_code
    assert _v20h_to_qmt_code("000012") == "000012.SZ"
    assert _v20h_to_qmt_code("600519") == "600519.SH"
    assert _v20h_to_qmt_code("688981") == "688981.SH"
    assert _qmt_to_v20h_code("000012.SZ") == "000012"
    assert _qmt_to_v20h_code("600519.SH") == "600519"


def test_adapter_class_attrs():
    from plugins.v20h_adapter import V20HAdapter
    assert V20HAdapter.name == "v20h_v1_3"


def test_adapter_handles_missing_external_data(tmp_path, monkeypatch):
    """pred_csi1000 缺失（外部数据未上传）→ 优雅退化，返回空 list，不 crash。"""
    import plugins.v20h_adapter as adapter_mod
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    monkeypatch.setattr(adapter_mod, "_V20H_DIR", tmp_path)
    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None

    store = ParquetStore(root=tmp_path / "parquet")
    ctx = Context(
        instance_id="x", trade_date=20240403,
        virtual_cash=0.0, virtual_positions={},
        parquet_store=store,
    )
    signals = V20HAdapter().run(ctx, 20240403)
    assert signals == []

    # cleanup class cache
    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None


def test_adapter_emits_buy_signals_for_target_positions(tmp_path, monkeypatch):
    """有 pred + v12 数据，且 ctx 有当日行情时，应输出 BUY RawSignals。"""
    import plugins.v20h_adapter as adapter_mod
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    # 1) 准备最小 mock 数据集
    # _load_resources 读取:
    #   _V20H_DIR / "config.yaml"
    #   _V20H_DIR / "data" / "pred_csi1000.parquet"
    #   _V20H_DIR / "data" / "v12_exp_hs300.parquet"
    # 所以把 _V20H_DIR 设为 tmp_path，文件放在 tmp_path / "data"
    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()

    # pred: 1 个日期、20 只股票（strategy.step 要求 len(pred_today) >= 20），prob_top 都正
    n_stocks = 20
    # 6 开头 → SH；其他 → SZ
    codes6 = [f"60{i:04d}" for i in range(n_stocks)]
    closes = [float(10 + i) for i in range(n_stocks)]
    pred = pd.DataFrame({
        "date": [pd.Timestamp("20240403")] * n_stocks,
        "code": codes6,
        "close": closes,
        "prob_top": [0.9 - i * 0.01 for i in range(n_stocks)],
        "excess_ret": [0.01 - i * 0.0005 for i in range(n_stocks)],
    })
    pred.to_parquet(fake_data_dir / "pred_csi1000.parquet")

    # v12 = 0.5（中性）：需要多行才能让 squeeze() 不退化成标量
    v12_dates = pd.date_range("20240401", periods=5, freq="B")
    v12 = pd.DataFrame({"exposure": [0.5] * 5}, index=v12_dates)
    v12.index.name = None
    v12.to_parquet(fake_data_dir / "v12_exp_hs300.parquet")

    # index_csi1000：覆盖 trade_date 20240403
    idx_dates = pd.date_range("20240401", periods=5, freq="B")
    idx_df = pd.DataFrame({
        "open": [5950.0] * 5, "high": [6010.0] * 5,
        "low": [5940.0] * 5, "close": [6005.0] * 5,
        "volume": [0] * 5,
    }, index=idx_dates)
    idx_df.index.name = "date"
    idx_df.to_parquet(fake_data_dir / "index_csi1000.parquet")

    # 2) 把 V20HAdapter 的 _V20H_DIR 临时指向 tmp_path
    monkeypatch.setattr(adapter_mod, "_V20H_DIR", tmp_path)

    # 重置 adapter class 缓存（在 monkeypatch 替换后立即重置）
    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None

    # 写最小 config.yaml（q_warmup_days=1 以绕过 180 天暖机期）
    cfg_yaml = """
capital_init: 10_000_000
cut_pct: 0.10
rebal_freq: 42
weight_cap: 1.5
q10_quantile: 0.10
q20_quantile: 0.20
q40_quantile: 0.40
q_warmup_days: 1
use_vol_target: false
target_vol_ann: 0.15
vol_lookback: 20
stock_cmn_rate: 0.0003
min_stock_cmn: 5.0
stamp_duty: 0.0005
bond_yield: 0.035
fut_cmn_rate: 0.0005
basis_cost: 0.03
fut_margin_ratio: 0.15
roll_cost_bps: 10
lot_size: 100
cash_buffer: 0.02
start_date: "2024-04-03"
"""
    (tmp_path / "config.yaml").write_text(cfg_yaml, encoding="utf-8")

    # 3) Build ctx + 给 20 只股票各推一条当日行情
    store = ParquetStore(root=tmp_path / "parquet")
    for code6, close in zip(codes6, closes):
        symbol_qmt = f"{code6}.SH"  # all start with 6
        store.append("stocks", symbol_qmt, pd.DataFrame([{
            "trade_date": 20240403, "open": close, "high": close,
            "low": close * 0.99, "close": close,
            "volume": 1000, "amount": close * 1000, "suspendFlag": 0,
        }]))
    store.append("indexes", "000852.SH", pd.DataFrame([{
        "trade_date": 20240403, "open": 6000, "high": 6010,
        "low": 5990, "close": 6005,
        "volume": 0, "amount": 0,
    }]))

    ctx = Context(
        instance_id="paper_v20h_v20h_v1_3",
        trade_date=20240403,
        virtual_cash=10_000_000.0,
        virtual_positions={},
        parquet_store=store,
    )

    # 4) 跑 adapter
    adapter = V20HAdapter()
    signals = adapter.run(ctx, 20240403)

    # V20H 第一天 di=0, last_rb_idx=-42 → 0 - (-42) = 42 >= 42，触发 rebalance
    assert len(signals) > 0
    assert all(s.direction == "BUY" for s in signals)
    # 数量必须是 100 整数倍
    assert all(s.quantity % 100 == 0 for s in signals)
    assert all(s.price_offset == 0.005 for s in signals)

    # cleanup class cache
    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None


def test_adapter_filters_blacklisted_symbols(tmp_path, monkeypatch):
    """ctx.risk_blacklist 里的 symbol 不应出现在输出 signals 里。"""
    import plugins.v20h_adapter as adapter_mod
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()

    n_stocks = 20
    codes6 = [f"60{i:04d}" for i in range(n_stocks)]
    closes = [float(10 + i) for i in range(n_stocks)]
    pred = pd.DataFrame({
        "date": [pd.Timestamp("20240403")] * n_stocks,
        "code": codes6,
        "close": closes,
        "prob_top": [0.9 - i * 0.01 for i in range(n_stocks)],
        "excess_ret": [0.01 - i * 0.0005 for i in range(n_stocks)],
    })
    pred.to_parquet(fake_data_dir / "pred_csi1000.parquet")

    v12_dates = pd.date_range("20240401", periods=5, freq="B")
    v12 = pd.DataFrame({"exposure": [0.5] * 5}, index=v12_dates)
    v12.index.name = None
    v12.to_parquet(fake_data_dir / "v12_exp_hs300.parquet")

    idx_dates = pd.date_range("20240401", periods=5, freq="B")
    idx_df = pd.DataFrame({
        "open": [5950.0] * 5, "high": [6010.0] * 5,
        "low": [5940.0] * 5, "close": [6005.0] * 5,
        "volume": [0] * 5,
    }, index=idx_dates)
    idx_df.index.name = "date"
    idx_df.to_parquet(fake_data_dir / "index_csi1000.parquet")

    monkeypatch.setattr(adapter_mod, "_V20H_DIR", tmp_path)
    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None

    cfg_yaml = """
capital_init: 10_000_000
cut_pct: 0.10
rebal_freq: 42
weight_cap: 1.5
q10_quantile: 0.10
q20_quantile: 0.20
q40_quantile: 0.40
q_warmup_days: 1
use_vol_target: false
target_vol_ann: 0.15
vol_lookback: 20
stock_cmn_rate: 0.0003
min_stock_cmn: 5.0
stamp_duty: 0.0005
bond_yield: 0.035
fut_cmn_rate: 0.0005
basis_cost: 0.03
fut_margin_ratio: 0.15
roll_cost_bps: 10
lot_size: 100
cash_buffer: 0.02
start_date: "2024-04-03"
"""
    (tmp_path / "config.yaml").write_text(cfg_yaml, encoding="utf-8")

    store = ParquetStore(root=tmp_path / "parquet")
    for code6, close in zip(codes6, closes):
        store.append("stocks", f"{code6}.SH", pd.DataFrame([{
            "trade_date": 20240403, "open": close, "high": close,
            "low": close * 0.99, "close": close,
            "volume": 1000, "amount": close * 1000, "suspendFlag": 0,
        }]))
    store.append("indexes", "000852.SH", pd.DataFrame([{
        "trade_date": 20240403, "open": 6000, "high": 6010,
        "low": 5990, "close": 6005, "volume": 0, "amount": 0,
    }]))

    # 拉黑前 5 只（QMT 格式 .SH）
    blacklisted_qmt = {f"60{i:04d}.SH" for i in range(5)}

    ctx = Context(
        instance_id="paper_v20h_v20h_v1_3",
        trade_date=20240403,
        virtual_cash=10_000_000.0,
        virtual_positions={},
        parquet_store=store,
        risk_blacklist=blacklisted_qmt,
    )

    adapter = V20HAdapter()
    signals = adapter.run(ctx, 20240403)

    output_symbols = {s.symbol for s in signals}
    # 拉黑的 5 只完全不应出现
    assert output_symbols.isdisjoint(blacklisted_qmt), (
        f"黑名单未生效，仍出现: {output_symbols & blacklisted_qmt}"
    )

    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None
