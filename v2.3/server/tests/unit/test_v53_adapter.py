"""V53Adapter 单元测试 — 月末判断 + 资源加载 + 非月末短路。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest


# ── helpers ───────────────────────────────────────────────────────────────
def _make_ctx(tmp_path, trade_date_int: int,
              cash: float = 0.0, positions: dict | None = None,
              anchor_trade_dates: list[int] | None = None):
    """构造 Context。anchor_trade_dates: list of int YYYYMMDD 写入 510300.SH parquet。"""
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    store = ParquetStore(root=tmp_path / "parquet")
    if anchor_trade_dates:
        df = pd.DataFrame({
            "trade_date": anchor_trade_dates,
            "open":  [3.0] * len(anchor_trade_dates),
            "high":  [3.0] * len(anchor_trade_dates),
            "low":   [3.0] * len(anchor_trade_dates),
            "close": [3.0] * len(anchor_trade_dates),
            "volume": [0] * len(anchor_trade_dates),
        })
        store.append("etfs", "510300.SH", df)
    return Context(
        instance_id="paper_v53_v53",
        trade_date=trade_date_int,
        virtual_cash=cash,
        virtual_positions=positions or {},
        parquet_store=store,
    )


def _reset_adapter_cache():
    from plugins.v53_adapter import V53Adapter
    V53Adapter._cfg = None
    V53Adapter._etf_close_bundle = None
    V53Adapter._etf_meta = None


# ── class attribute tests ─────────────────────────────────────────────────
def test_adapter_class_attrs():
    from plugins.v53_adapter import V53Adapter
    assert V53Adapter.name == "v53"
    assert V53Adapter.data_files == ["etf_close.parquet", "etf_meta.parquet"]


# ── 月末判断 ──────────────────────────────────────────────────────────────
def _dates_in_month(year: int, month: int) -> list[int]:
    """生成 (year, month) 全部工作日的 YYYYMMDD int 列表。"""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    rng = pd.bdate_range(f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}")
    return sorted({int(d.strftime("%Y%m%d")) for d in rng})


def test_is_month_end_true(tmp_path):
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    # 2024-04 所有工作日；最后一个是 2024-04-30 (周二)
    days = _dates_in_month(2024, 4)
    last = max(days)  # 20240430
    ctx = _make_ctx(tmp_path, last, anchor_trade_dates=days)
    target = pd.to_datetime(str(last), format="%Y%m%d")
    assert V53Adapter()._is_month_end(ctx, target) is True
    _reset_adapter_cache()


def test_is_month_end_false_midmonth(tmp_path):
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    days = _dates_in_month(2024, 4)
    # ctx.trade_date must be the last day so ctx.market returns ALL April dates;
    # then we ask _is_month_end with a mid-month target — it should return False
    # because 2024-04-15 != max(April biz days) = 2024-04-30.
    ctx = _make_ctx(tmp_path, 20240430, anchor_trade_dates=days)
    target = pd.to_datetime("20240415", format="%Y%m%d")
    assert V53Adapter()._is_month_end(ctx, target) is False
    _reset_adapter_cache()


def test_is_month_end_no_anchor_data(tmp_path):
    """anchor ETF 没有数据 → False（保守不调仓）"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    ctx = _make_ctx(tmp_path, 20240430)  # 无 anchor_trade_dates
    target = pd.to_datetime("20240430", format="%Y%m%d")
    assert V53Adapter()._is_month_end(ctx, target) is False
    _reset_adapter_cache()


# ── run() 短路行为 ────────────────────────────────────────────────────────
def test_run_returns_empty_when_not_month_end(tmp_path):
    """非月末 → run() return []，bundle 不需要加载"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    days = _dates_in_month(2024, 4)
    ctx = _make_ctx(tmp_path, 20240415, anchor_trade_dates=days)
    # bundle 不存在，但因为非月末提前 return []，不会触发 _load_resources 失败
    # ⚠️ 当前 plugins/v53/data/ 有真实 bundle (Task 6 已 copy)，会被加载，但不影响 _is_month_end 之后立即 return
    assert V53Adapter().run(ctx, 20240415) == []
    _reset_adapter_cache()


def test_run_returns_empty_when_bundle_missing(tmp_path, monkeypatch):
    """bundle 文件缺失 (外部数据未上传) → run() 优雅退化 return []，不 crash"""
    _reset_adapter_cache()
    import plugins.v53_adapter as adapter_mod
    from plugins.v53_adapter import V53Adapter
    # 把 _V53_DIR 指向空目录 → bundle 读不到
    monkeypatch.setattr(adapter_mod, "_V53_DIR", tmp_path / "empty_v53_dir")
    days = _dates_in_month(2024, 4)
    ctx = _make_ctx(tmp_path, max(days), anchor_trade_dates=days)
    # 即使是月末，资源加载失败应优雅返回空
    assert V53Adapter().run(ctx, max(days)) == []
    _reset_adapter_cache()


# ── _build_close_matrix ───────────────────────────────────────────────────

def _make_bundle_in_tmp_dir(tmp_path, bundle_end_date: str = "2024-03-31"):
    """生成 mini etf_close + etf_meta bundle 写到 tmp_path/v53dir/data/。
    返回 v53dir 路径（含 config.yaml + data/）。"""
    import shutil
    import yaml
    from plugins.v53.code_map import V53_KEY_TO_QMT, ETF_KEYS

    v53dir = tmp_path / "v53dir"
    (v53dir / "data").mkdir(parents=True)

    # bundle close_px
    dates = pd.bdate_range("2023-01-01", bundle_end_date)
    pieces = []
    for i, key in enumerate(ETF_KEYS):
        qmt = V53_KEY_TO_QMT[key]
        pieces.append(pd.DataFrame({
            "trade_date": dates,
            "code": qmt,
            "close": [10.0 + i + j * 0.001 for j in range(len(dates))],
            "open": [10.0 + i + j * 0.001 for j in range(len(dates))],
        }))
    pd.concat(pieces, ignore_index=True).to_parquet(
        v53dir / "data" / "etf_close.parquet", index=False)

    # bundle meta
    pd.DataFrame([{
        "code": V53_KEY_TO_QMT[k], "name": k, "list_date": pd.Timestamp("2020-01-01").date(),
        "is_qdii": V53_KEY_TO_QMT[k] in {"513500.SH", "513100.SH"},
        "quadrants": ["growth_up"],
    } for k in ETF_KEYS]).to_parquet(v53dir / "data" / "etf_meta.parquet", index=False)

    # config.yaml — copy real one for completeness
    real_cfg = Path("plugins/v53/config.yaml").resolve()
    shutil.copy(real_cfg, v53dir / "config.yaml")
    return v53dir


def test_build_close_matrix_pure_bundle(tmp_path, monkeypatch):
    """bundle 覆盖到 target_date → 不需要 IngestService 增量。返回 wide DataFrame, columns=internal keys"""
    _reset_adapter_cache()
    import plugins.v53_adapter as adapter_mod
    from plugins.v53_adapter import V53Adapter
    from plugins.v53.code_map import ETF_KEYS

    v53dir = _make_bundle_in_tmp_dir(tmp_path, bundle_end_date="2024-04-30")
    monkeypatch.setattr(adapter_mod, "_V53_DIR", v53dir)

    ctx = _make_ctx(tmp_path, 20240430)  # 无 ingest 增量
    adapter = V53Adapter()
    adapter._load_resources()
    close = adapter._build_close_matrix(ctx, pd.Timestamp("2024-04-30"))

    assert isinstance(close, pd.DataFrame)
    # columns 是 internal keys（不是 QMT code）
    assert set(close.columns) == set(ETF_KEYS)
    # index 是 datetime
    assert pd.api.types.is_datetime64_any_dtype(close.index)
    # 不超过 target_date
    assert close.index.max() <= pd.Timestamp("2024-04-30")
    # 至少要有几百行（2023-01 ~ 2024-04）
    assert len(close) >= 300
    _reset_adapter_cache()


def test_build_close_matrix_with_incremental(tmp_path, monkeypatch):
    """bundle 截到 3/31，ingest 提供 4 月增量 → 拼接到 4/30"""
    _reset_adapter_cache()
    import plugins.v53_adapter as adapter_mod
    from plugins.v53_adapter import V53Adapter
    from plugins.v53.code_map import V53_KEY_TO_QMT
    from app.storage.parquet import ParquetStore

    v53dir = _make_bundle_in_tmp_dir(tmp_path, bundle_end_date="2024-03-31")
    monkeypatch.setattr(adapter_mod, "_V53_DIR", v53dir)

    # 给每个 v53 ETF 写 4 月增量到 ctx 的 store
    store = ParquetStore(root=tmp_path / "parquet")
    incr_dates_int = [20240401, 20240402, 20240403, 20240430]
    for i, qmt in enumerate(V53_KEY_TO_QMT.values()):
        store.append("etfs", qmt, pd.DataFrame({
            "trade_date": incr_dates_int,
            "open":  [99.0] * 4, "high": [99.0] * 4, "low": [99.0] * 4,
            "close": [99.0 + i] * 4, "volume": [0] * 4,
        }))

    from app.strategy.context import Context
    ctx = Context(
        instance_id="paper_v53_v53",
        trade_date=20240430,
        virtual_cash=0.0,
        virtual_positions={},
        parquet_store=store,
    )

    adapter = V53Adapter()
    adapter._load_resources()
    close = adapter._build_close_matrix(ctx, pd.Timestamp("2024-04-30"))

    # 4/30 那天的数据应该来自 ingest
    last_row = close.loc[pd.Timestamp("2024-04-30")]
    # close 99.0 + i 对应 internal key 顺序
    assert last_row["hs300"] == 99.0  # i=0
    assert last_row["dividend"] == 99.0 + 9  # i=9
    _reset_adapter_cache()


def test_build_close_matrix_target_filters_future(tmp_path, monkeypatch):
    """bundle 含 target 之后的数据 → 应被过滤掉"""
    _reset_adapter_cache()
    import plugins.v53_adapter as adapter_mod
    from plugins.v53_adapter import V53Adapter

    v53dir = _make_bundle_in_tmp_dir(tmp_path, bundle_end_date="2024-04-30")
    monkeypatch.setattr(adapter_mod, "_V53_DIR", v53dir)

    ctx = _make_ctx(tmp_path, 20240131)
    adapter = V53Adapter()
    adapter._load_resources()
    close = adapter._build_close_matrix(ctx, pd.Timestamp("2024-01-31"))

    assert close.index.max() <= pd.Timestamp("2024-01-31")
    _reset_adapter_cache()
