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
