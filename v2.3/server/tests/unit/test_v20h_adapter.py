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

    # V20H 空仓 (is_first_rebal=True) → adapter 主动触发 rebalance
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


def test_adapter_holds_position_on_non_rebal_day(tmp_path, monkeypatch):
    """有持仓 + di 不是 rebal_freq 倍数 → 不触发主调仓 (signals 为空)。

    回归测试 stateless 调仓 bug:
    旧逻辑下 V20HStrategy 每次构造 last_rb_idx=-42, 任意 di 都触发 rebal,
    导致服务器每天清仓重建。修复后 adapter 把 last_rb_idx 锚定到 di 模 rebal_freq。
    """
    import plugins.v20h_adapter as adapter_mod
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()

    # 6 个 pred 日期 → target_date = 第 6 天 → di=5 (5%42 != 0)
    n_stocks = 20
    codes6 = [f"60{i:04d}" for i in range(n_stocks)]
    closes = [float(10 + i) for i in range(n_stocks)]
    pred_dates = pd.date_range("20240403", periods=6, freq="B")
    target_date_dt = pred_dates[-1]
    target_date_int = int(target_date_dt.strftime("%Y%m%d"))

    pred_rows = []
    for d in pred_dates:
        for code6, close in zip(codes6, closes):
            pred_rows.append({
                "date": d, "code": code6, "close": close,
                "prob_top": 0.9 - codes6.index(code6) * 0.01,
                "excess_ret": 0.01 - codes6.index(code6) * 0.0005,
            })
    pred = pd.DataFrame(pred_rows)
    pred.to_parquet(fake_data_dir / "pred_csi1000.parquet")

    v12 = pd.DataFrame({"exposure": [0.5] * 6}, index=pred_dates)
    v12.index.name = None
    v12.to_parquet(fake_data_dir / "v12_exp_hs300.parquet")

    idx_df = pd.DataFrame({
        "open": [5950.0] * 6, "high": [6010.0] * 6,
        "low": [5940.0] * 6, "close": [6005.0] * 6,
        "volume": [0] * 6,
    }, index=pred_dates)
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
            "trade_date": target_date_int, "open": close, "high": close,
            "low": close * 0.99, "close": close,
            "volume": 1000, "amount": close * 1000, "suspendFlag": 0,
        }]))
    store.append("indexes", "000852.SH", pd.DataFrame([{
        "trade_date": target_date_int, "open": 6000, "high": 6010,
        "low": 5990, "close": 6005, "volume": 0, "amount": 0,
    }]))

    # 给 18 只仓位 (CUT10 后的 V20H 标准持仓数), 每只 1000 股
    # 价差小 (10-27) → 单股权重均匀 → 不触发 cap-weight 1.5×
    virtual_positions = {f"{code6}.SH": 1000 for code6 in codes6[:18]}

    ctx = Context(
        instance_id="paper_v20h_v20h_v1_3",
        trade_date=target_date_int,
        virtual_cash=1_000_000.0,
        virtual_positions=virtual_positions,
        parquet_store=store,
    )

    adapter = V20HAdapter()
    signals = adapter.run(ctx, target_date_int)

    # di=5, 5 % 42 = 5 != 0 → 非 rebal day → 主调仓不应触发
    # (cap-weight 在均匀持仓下也不触发, 故信号应完全为空)
    assert len(signals) == 0, (
        f"非 rebal day 不应产生信号, 但收到 {len(signals)} 个: "
        f"{[(s.direction, s.symbol, s.quantity) for s in signals[:5]]}"
    )

    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None


def test_adapter_triggers_rebal_on_scheduled_day(tmp_path, monkeypatch):
    """有持仓 + di 是 rebal_freq 倍数 → 主调仓触发 (signals 非空)。"""
    import plugins.v20h_adapter as adapter_mod
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()

    # 让 pred 有 43 天 → target_date 是第 43 天 → di=42 → 42 % 42 == 0 是 rebal day
    n_stocks = 20
    codes6 = [f"60{i:04d}" for i in range(n_stocks)]
    closes = [float(10 + i) for i in range(n_stocks)]
    pred_dates = pd.date_range("20240403", periods=43, freq="B")
    target_date_dt = pred_dates[-1]
    target_date_int = int(target_date_dt.strftime("%Y%m%d"))

    pred_rows = []
    for d in pred_dates:
        for code6, close in zip(codes6, closes):
            pred_rows.append({
                "date": d, "code": code6, "close": close,
                "prob_top": 0.9 - codes6.index(code6) * 0.01,
                "excess_ret": 0.01 - codes6.index(code6) * 0.0005,
            })
    pred = pd.DataFrame(pred_rows)
    pred.to_parquet(fake_data_dir / "pred_csi1000.parquet")

    v12 = pd.DataFrame({"exposure": [0.5] * 43}, index=pred_dates)
    v12.index.name = None
    v12.to_parquet(fake_data_dir / "v12_exp_hs300.parquet")

    idx_df = pd.DataFrame({
        "open": [5950.0] * 43, "high": [6010.0] * 43,
        "low": [5940.0] * 43, "close": [6005.0] * 43,
        "volume": [0] * 43,
    }, index=pred_dates)
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
            "trade_date": target_date_int, "open": close, "high": close,
            "low": close * 0.99, "close": close,
            "volume": 1000, "amount": close * 1000, "suspendFlag": 0,
        }]))
    store.append("indexes", "000852.SH", pd.DataFrame([{
        "trade_date": target_date_int, "open": 6000, "high": 6010,
        "low": 5990, "close": 6005, "volume": 0, "amount": 0,
    }]))

    # 持仓和 V20H 在 di=42 调仓的目标不同 (我们只给 5 只),
    # 调仓必须卖掉它们并买入 CUT10 后的 18 只目标 → 产生信号
    virtual_positions = {f"{code6}.SH": 5000 for code6 in codes6[15:20]}

    ctx = Context(
        instance_id="paper_v20h_v20h_v1_3",
        trade_date=target_date_int,
        virtual_cash=5_000_000.0,
        virtual_positions=virtual_positions,
        parquet_store=store,
    )

    adapter = V20HAdapter()
    signals = adapter.run(ctx, target_date_int)

    # di=42, 42 % 42 == 0 → rebal day → 应产生 SELL (旧持仓) + BUY (新目标)
    assert len(signals) > 0, "rebal 日应产生信号"
    sells = [s for s in signals if s.direction == "SELL"]
    buys = [s for s in signals if s.direction == "BUY"]
    assert len(sells) > 0, "rebal 日应有 SELL 信号 (卖掉非目标股票)"
    assert len(buys) > 0, "rebal 日应有 BUY 信号 (买入新目标)"

    # Bug D: 验证 strategy_state 被写回（adapter 应通过 set_strategy_state 落库）
    next_state = ctx.pop_next_strategy_state()
    assert next_state is not None
    assert "last_rb_idx" in next_state
    assert "equity_history" in next_state
    assert "daily_rets" in next_state
    assert "prev_hedge" in next_state
    # 既然这次触发了 rebal，last_rb_idx 应该被 step() 更新为 di
    assert next_state["last_rb_idx"] == 42

    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None


def test_adapter_respects_persisted_last_rb_idx(tmp_path, monkeypatch):
    """有持久化 last_rb_idx 且未到 42 天 → 不应触发 rebal。"""
    import plugins.v20h_adapter as adapter_mod
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()

    n_stocks = 20
    codes6 = [f"60{i:04d}" for i in range(n_stocks)]
    closes = [float(10 + i) for i in range(n_stocks)]
    pred_dates = pd.date_range("20240403", periods=43, freq="B")
    target_date_dt = pred_dates[20]  # di=20, 离 last_rb_idx=10 还差 10 天
    target_date_int = int(target_date_dt.strftime("%Y%m%d"))

    pred_rows = []
    for d in pred_dates:
        for code6, close in zip(codes6, closes):
            pred_rows.append({
                "date": d, "code": code6, "close": close,
                "prob_top": 0.9 - codes6.index(code6) * 0.01,
                "excess_ret": 0.01 - codes6.index(code6) * 0.0005,
            })
    pd.DataFrame(pred_rows).to_parquet(fake_data_dir / "pred_csi1000.parquet")

    v12 = pd.DataFrame({"exposure": [0.5] * 43}, index=pred_dates)
    v12.to_parquet(fake_data_dir / "v12_exp_hs300.parquet")

    idx_df = pd.DataFrame({
        "open": [5950.0] * 43, "high": [6010.0] * 43,
        "low": [5940.0] * 43, "close": [6005.0] * 43, "volume": [0] * 43,
    }, index=pred_dates)
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
            "trade_date": target_date_int, "open": close, "high": close,
            "low": close * 0.99, "close": close,
            "volume": 1000, "amount": close * 1000, "suspendFlag": 0,
        }]))
    store.append("indexes", "000852.SH", pd.DataFrame([{
        "trade_date": target_date_int, "open": 6000, "high": 6010,
        "low": 5990, "close": 6005, "volume": 0, "amount": 0,
    }]))

    # 持仓正好等于 V20H target（避免有 cap_weight 触发，让"无信号"假设成立）
    # 这里我们模拟"已经 rebal 过、当前完美对齐"的情形
    target_codes = codes6[:18]  # cut 10% 后剩 18 个
    virtual_positions = {f"{c}.SH": 100 for c in target_codes}

    ctx = Context(
        instance_id="paper_v20h_v20h_v1_3",
        trade_date=target_date_int,
        virtual_cash=5_000_000.0,
        virtual_positions=virtual_positions,
        parquet_store=store,
        strategy_state={
            "last_rb_idx": 10,  # 上次 rebal 在 di=10, 离今天 (di=20) 仅 10 天
            "equity_history": [10_000_000.0],
            "daily_rets": [],
            "prev_hedge": 0.0,
        },
    )

    adapter = V20HAdapter()
    signals = adapter.run(ctx, target_date_int)

    # 不到 42 天，主调仓不应触发；signals 应该为空或只有少量 cap_weight 微调
    main_rebal_signals = [s for s in signals if s.direction == "BUY"]
    # cap_weight 只产生 SELL，不会产生 BUY；如果有 BUY 说明触发了主调仓
    assert len(main_rebal_signals) == 0, (
        f"未到 42 天，主调仓不应触发；实际有 {len(main_rebal_signals)} 个 BUY 信号"
    )

    # 验证 next state 写回了正确的 last_rb_idx（未变）
    next_state = ctx.pop_next_strategy_state()
    assert next_state["last_rb_idx"] == 10  # 未触发 rebal，保持不变

    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None


def test_adapter_uses_real_close_not_pred_close_as_reference_price(tmp_path, monkeypatch):
    """RawSignal.reference_price 应该用 ctx.market 里最近一日真实收盘，**不是** pred close。

    Regression: 5/21 实盘场景——pred lag 16 天，pred close 比实际跌了 24%。
    用 pred close 当限价基准导致 4 个 SELL 单注定卖不出。
    fix 后用 ctx.market 真实价，limit 跟当下市场对齐。
    """
    import plugins.v20h_adapter as adapter_mod
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()

    n_stocks = 20
    codes6 = [f"60{i:04d}" for i in range(n_stocks)]
    # 关键：pred close 故意设为 100（"stale"），store 里真实 close 设为 50（"real"）
    pred_close = 100.0
    real_close = 50.0   # 模拟 pred lag 期间市场跌了 50%

    pred_dates = pd.date_range("20240403", periods=43, freq="B")
    target_date_dt = pred_dates[-1]
    target_date_int = int(target_date_dt.strftime("%Y%m%d"))

    pred_rows = []
    for d in pred_dates:
        for code6 in codes6:
            pred_rows.append({
                "date": d, "code": code6, "close": pred_close,
                "prob_top": 0.9 - codes6.index(code6) * 0.01,
                "excess_ret": 0.01 - codes6.index(code6) * 0.0005,
            })
    pd.DataFrame(pred_rows).to_parquet(fake_data_dir / "pred_csi1000.parquet")

    v12 = pd.DataFrame({"exposure": [0.5] * 43}, index=pred_dates)
    v12.to_parquet(fake_data_dir / "v12_exp_hs300.parquet")

    idx_df = pd.DataFrame({
        "open": [5950.0] * 43, "high": [6010.0] * 43,
        "low": [5940.0] * 43, "close": [6005.0] * 43, "volume": [0] * 43,
    }, index=pred_dates)
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

    # 关键：store 里的 close = real_close = 50（和 pred 100 不同）
    store = ParquetStore(root=tmp_path / "parquet")
    for code6 in codes6:
        store.append("stocks", f"{code6}.SH", pd.DataFrame([{
            "trade_date": target_date_int, "open": real_close, "high": real_close,
            "low": real_close * 0.99, "close": real_close,
            "volume": 1000, "amount": real_close * 1000, "suspendFlag": 0,
        }]))
    store.append("indexes", "000852.SH", pd.DataFrame([{
        "trade_date": target_date_int, "open": 6000, "high": 6010,
        "low": 5990, "close": 6005, "volume": 0, "amount": 0,
    }]))

    # 持仓里有 5 只 V20H 不要的票（rebal 日会全卖）
    virtual_positions = {f"{code6}.SH": 5000 for code6 in codes6[15:20]}

    ctx = Context(
        instance_id="paper_v20h_v20h_v1_3",
        trade_date=target_date_int,
        virtual_cash=5_000_000.0,
        virtual_positions=virtual_positions,
        parquet_store=store,
    )

    adapter = V20HAdapter()
    signals = adapter.run(ctx, target_date_int)

    assert len(signals) > 0, "应产生信号"
    # 所有信号的 reference_price 应该 = real_close (50)，不是 pred_close (100)
    for sig in signals:
        assert sig.reference_price == real_close, (
            f"{sig.symbol} {sig.direction}: reference_price={sig.reference_price}，"
            f"应该是 real_close={real_close} 而不是 pred_close={pred_close}"
        )

    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None


def test_adapter_falls_back_to_pred_close_when_no_real_market_data(tmp_path, monkeypatch):
    """ctx.market 无数据时，reference_price 回退到 pred close，并 emit warning log。"""
    import plugins.v20h_adapter as adapter_mod
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()

    n_stocks = 20
    codes6 = [f"60{i:04d}" for i in range(n_stocks)]
    pred_close = 100.0   # 只有 pred close

    pred_dates = pd.date_range("20240403", periods=43, freq="B")
    target_date_dt = pred_dates[-1]
    target_date_int = int(target_date_dt.strftime("%Y%m%d"))

    pred_rows = [
        {"date": d, "code": code6, "close": pred_close,
         "prob_top": 0.9 - codes6.index(code6) * 0.01,
         "excess_ret": 0.01 - codes6.index(code6) * 0.0005}
        for d in pred_dates for code6 in codes6
    ]
    pd.DataFrame(pred_rows).to_parquet(fake_data_dir / "pred_csi1000.parquet")

    v12 = pd.DataFrame({"exposure": [0.5] * 43}, index=pred_dates)
    v12.to_parquet(fake_data_dir / "v12_exp_hs300.parquet")

    idx_df = pd.DataFrame({
        "open": [5950.0] * 43, "high": [6010.0] * 43,
        "low": [5940.0] * 43, "close": [6005.0] * 43, "volume": [0] * 43,
    }, index=pred_dates)
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

    # store **完全为空** —— 模拟 ctx.market(symbol) 都返回 empty DataFrame
    store = ParquetStore(root=tmp_path / "parquet")
    # 必须有 index 才能让 adapter 走到信号生成（否则空 prices_today 早退出）
    store.append("indexes", "000852.SH", pd.DataFrame([{
        "trade_date": target_date_int, "open": 6000, "high": 6010,
        "low": 5990, "close": 6005, "volume": 0, "amount": 0,
    }]))

    virtual_positions = {f"{code6}.SH": 5000 for code6 in codes6[15:20]}

    ctx = Context(
        instance_id="paper_v20h_v20h_v1_3",
        trade_date=target_date_int,
        virtual_cash=5_000_000.0,
        virtual_positions=virtual_positions,
        parquet_store=store,
    )

    adapter = V20HAdapter()
    signals = adapter.run(ctx, target_date_int)

    # 没真实价 → fallback 到 pred close
    assert len(signals) > 0
    for sig in signals:
        assert sig.reference_price == pred_close, (
            f"{sig.symbol}: 应回退到 pred_close={pred_close}，实际 {sig.reference_price}"
        )

    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None


def test_adapter_kcb_200_share_minimum_rule(tmp_path, monkeypatch):
    """科创板 (688/689) 单笔委托 < 200 股账户级会废单。
    Regression: 5/26 + 5/27 实证 10 单 100 股 688x SELL 全部 CANCELLED。
    """
    import plugins.v20h_adapter as adapter_mod
    from plugins.v20h_adapter import V20HAdapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()

    # 准备 pred：包含 1 个普通票 + 3 个科创板票
    codes6 = ["600001", "688001", "688002", "688003"]
    closes = [10.0, 100.0, 150.0, 200.0]
    pred_dates = pd.date_range("20240403", periods=43, freq="B")
    target_date_dt = pred_dates[-1]
    target_date_int = int(target_date_dt.strftime("%Y%m%d"))
    pred_rows = [
        {"date": d, "code": c, "close": closes[i],
         "prob_top": 0.9 - i * 0.01, "excess_ret": 0.01}
        for d in pred_dates for i, c in enumerate(codes6)
    ]
    pd.DataFrame(pred_rows).to_parquet(fake_data_dir / "pred_csi1000.parquet")
    v12 = pd.DataFrame({"exposure": [0.5] * 43}, index=pred_dates)
    v12.to_parquet(fake_data_dir / "v12_exp_hs300.parquet")
    idx_df = pd.DataFrame({"open": [6000]*43, "high": [6010]*43, "low": [5990]*43,
                           "close": [6005]*43, "volume": [0]*43}, index=pred_dates)
    idx_df.to_parquet(fake_data_dir / "index_csi1000.parquet")

    monkeypatch.setattr(adapter_mod, "_V20H_DIR", tmp_path)
    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None

    cfg_yaml = """
capital_init: 10_000_000
cut_pct: 0.0
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
        suffix = ".SH" if code6.startswith("6") else ".SZ"
        store.append("stocks", f"{code6}{suffix}", pd.DataFrame([{
            "trade_date": target_date_int, "open": close, "high": close,
            "low": close*0.99, "close": close, "volume": 1000,
            "amount": close*1000, "suspendFlag": 0,
        }]))
    store.append("indexes", "000852.SH", pd.DataFrame([{
        "trade_date": target_date_int, "open": 6000, "high": 6010,
        "low": 5990, "close": 6005, "volume": 0, "amount": 0,
    }]))

    # 持仓:
    # - 600001.SH: 500 股（非科创板，无 200 限制）
    # - 688001.SH: 100 股（科创板，持仓 < 200，cap_weight 想 trim 也跳过）
    # - 688002.SH: 300 股（科创板，持仓 ≥ 200，cap_weight 100 应 round up 到 200）
    # - 688003.SH: 1000 股（科创板，正常 trim）
    virtual_positions = {
        "600001.SH": 500,
        "688001.SH": 100,
        "688002.SH": 300,
        "688003.SH": 1000,
    }
    ctx = Context(
        instance_id="paper_v20h_v20h_v1_3",
        trade_date=target_date_int,
        virtual_cash=5_000_000.0,
        virtual_positions=virtual_positions,
        parquet_store=store,
    )

    # 让 strategy 想各 trim 100 股（直接用 monkeypatch override 不太方便，
    # 这里直接构造 step() 输出后 emit 阶段的行为：手动调用 _emit）
    # 简化做法：直接验证 adapter.run 不发出 100 股的 688x SELL
    adapter = V20HAdapter()
    signals = adapter.run(ctx, target_date_int)

    # 找所有科创板 SELL 信号
    kcb_sells = [s for s in signals
                 if (s.symbol.startswith("688") or s.symbol.startswith("689"))
                 and s.direction == "SELL"]
    for s in kcb_sells:
        assert s.quantity >= 200, (
            f"科创板 SELL {s.symbol} qty={s.quantity} 违反 200 股下限"
        )

    V20HAdapter._cfg = None
    V20HAdapter._pred_df = None
    V20HAdapter._v12_series = None
    V20HAdapter._index_close = None
