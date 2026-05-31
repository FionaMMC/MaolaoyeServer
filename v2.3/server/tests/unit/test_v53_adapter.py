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


# 约定 B：调仓在「次月第一个交易日」开盘执行，权重用上月最后收盘日的数据算。
# 服务器侧无前向交易日历，所以判断逻辑 = 「target(执行日) 与最近可用收盘日是否跨月」。
# 关键：在真实流程里 target=trade_date 是「下一个交易日」(未来)，它本身的 EOD
# 还没入库，所以 anchor 数据只到上一个交易日 T。下面的 ctx 都不包含 target 当日数据。
def test_is_month_end_true_first_trading_day_of_new_month(tmp_path):
    """target 是新月第一个交易日（最近收盘日落在上月）→ True（触发调仓）"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    april = _dates_in_month(2024, 4)         # 数据只到 4/30
    target_int = 20240506                     # 5 月首个交易日（5/1-5/5 假期，示意）
    ctx = _make_ctx(tmp_path, target_int, anchor_trade_dates=april)
    target = pd.to_datetime(str(target_int), format="%Y%m%d")
    assert V53Adapter()._is_month_end(ctx, target) is True
    _reset_adapter_cache()


def test_is_month_end_true_year_boundary(tmp_path):
    """跨年：12 月最后收盘日 → 1 月首交易日也算调仓日 → True"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    dec = _dates_in_month(2024, 12)          # 数据到 2024-12-31
    target_int = 20250102                     # 2025 年首个交易日
    ctx = _make_ctx(tmp_path, target_int, anchor_trade_dates=dec)
    target = pd.to_datetime(str(target_int), format="%Y%m%d")
    assert V53Adapter()._is_month_end(ctx, target) is True
    _reset_adapter_cache()


def test_is_month_end_false_midmonth(tmp_path):
    """月中：最近收盘日与 target 同月 → False"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    # 4 月数据只到 4/15；target=4/16（同月）
    april_through_15 = [d for d in _dates_in_month(2024, 4) if d < 20240416]
    ctx = _make_ctx(tmp_path, 20240416, anchor_trade_dates=april_through_15)
    target = pd.to_datetime("20240416", format="%Y%m%d")
    assert V53Adapter()._is_month_end(ctx, target) is False
    _reset_adapter_cache()


def test_is_month_end_false_second_trading_day_of_month(tmp_path):
    """新月第二个交易日：最近收盘日已是本月 → False（每月只触发一次）"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    # 4 月数据 + 5 月首个交易日 5/6 都已入库；target=5/7
    days = _dates_in_month(2024, 4) + [20240506]
    ctx = _make_ctx(tmp_path, 20240507, anchor_trade_dates=days)
    target = pd.to_datetime("20240507", format="%Y%m%d")
    assert V53Adapter()._is_month_end(ctx, target) is False
    _reset_adapter_cache()


def test_is_month_end_no_anchor_data(tmp_path):
    """anchor ETF 没有数据 → False（保守不调仓）"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    ctx = _make_ctx(tmp_path, 20240506)  # 无 anchor_trade_dates
    target = pd.to_datetime("20240506", format="%Y%m%d")
    assert V53Adapter()._is_month_end(ctx, target) is False
    _reset_adapter_cache()


# ── run() 短路行为 ────────────────────────────────────────────────────────
def test_run_returns_empty_when_not_rebalance_day(tmp_path):
    """非调仓日（月中）→ run() return []"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    april_through_15 = [d for d in _dates_in_month(2024, 4) if d < 20240416]
    ctx = _make_ctx(tmp_path, 20240416, anchor_trade_dates=april_through_15)
    assert V53Adapter().run(ctx, 20240416) == []
    _reset_adapter_cache()


def test_run_returns_empty_when_bundle_missing(tmp_path, monkeypatch):
    """bundle 文件缺失 (外部数据未上传) → run() 优雅退化 return []，不 crash"""
    _reset_adapter_cache()
    import plugins.v53_adapter as adapter_mod
    from plugins.v53_adapter import V53Adapter
    # 把 _V53_DIR 指向空目录 → bundle 读不到
    monkeypatch.setattr(adapter_mod, "_V53_DIR", tmp_path / "empty_v53_dir")
    april = _dates_in_month(2024, 4)
    # 调仓日（新月首交易日），但资源加载失败应优雅返回空
    ctx = _make_ctx(tmp_path, 20240506, anchor_trade_dates=april)
    assert V53Adapter().run(ctx, 20240506) == []
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


# ── Task 13: helpers (NAV, reference_price, weights → qty) ────────────────
def test_resolve_reference_price_uses_ctx_market_latest(tmp_path):
    """ctx.market 有数据 → 取最近 close (int YYYYMMDD trade_date 转 datetime 后比较 target)"""
    _reset_adapter_cache()
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context
    from plugins.v53_adapter import V53Adapter

    store = ParquetStore(root=tmp_path / "parquet")
    store.append("etfs", "510300.SH", pd.DataFrame({
        "trade_date": [20240401, 20240430],
        "open": [3.0, 3.0], "high": [3.0, 3.0], "low": [3.0, 3.0],
        "close": [3.1, 3.5], "volume": [0, 0],
    }))
    ctx = Context("paper_v53_v53", 20240430, 0.0, {}, store)

    adapter = V53Adapter()
    # 不需要 bundle: 此测试只查 ctx 路径
    price = adapter._resolve_reference_price(ctx, "510300.SH", pd.Timestamp("2024-04-30"))
    assert price == 3.5
    _reset_adapter_cache()


def test_resolve_reference_price_falls_back_to_bundle(tmp_path, monkeypatch):
    """ctx 无数据 → fallback bundle"""
    _reset_adapter_cache()
    import plugins.v53_adapter as adapter_mod
    from plugins.v53_adapter import V53Adapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    # Set class-level bundle directly (avoid file IO)
    bundle = pd.DataFrame({
        "trade_date": pd.to_datetime(["2024-03-31", "2024-04-30"]),
        "code": ["511260.SH", "511260.SH"],
        "close": [105.0, 110.0],
        "open": [105.0, 110.0],
    })
    V53Adapter._etf_close_bundle = bundle

    store = ParquetStore(root=tmp_path / "parquet")  # 无 ETF 数据
    ctx = Context("paper_v53_v53", 20240430, 0.0, {}, store)

    price = V53Adapter()._resolve_reference_price(ctx, "511260.SH", pd.Timestamp("2024-04-30"))
    assert price == 110.0
    _reset_adapter_cache()


def test_resolve_reference_price_returns_none_when_no_data(tmp_path):
    """ctx + bundle 都无 → None"""
    _reset_adapter_cache()
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context
    from plugins.v53_adapter import V53Adapter

    store = ParquetStore(root=tmp_path / "parquet")
    ctx = Context("paper_v53_v53", 20240430, 0.0, {}, store)
    price = V53Adapter()._resolve_reference_price(ctx, "999999.SH", pd.Timestamp("2024-04-30"))
    assert price is None
    _reset_adapter_cache()


def test_compute_nav_cash_plus_positions(tmp_path, monkeypatch):
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter

    adapter = V53Adapter()
    # monkeypatch reference price to constants
    adapter._resolve_reference_price = lambda ctx, code, target: {
        "510300.SH": 3.0, "511260.SH": 110.0,
    }.get(code, 0.0)

    # 简单 ctx 模拟 cash + positions
    class FakeCtx:
        def cash(self): return 1_000_000.0
        def positions(self): return {"510300.SH": 10000, "511260.SH": 5000}

    nav = adapter._compute_nav(FakeCtx(), pd.Timestamp("2024-04-30"))
    # 1M + 10000*3 + 5000*110 = 1M + 30K + 550K = 1,580,000
    assert abs(nav - 1_580_000.0) < 1e-6
    _reset_adapter_cache()


def test_weights_to_quantities_basic():
    """1000万 × 0.67 / 110元 = 60909 股 → round(609.09)=609 lots × 100 = 60900"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._resolve_reference_price = lambda ctx, code, target: {
        "511260.SH": 110.0, "510300.SH": 3.0,
    }.get(code, 0.0)
    qty = adapter._weights_to_quantities(
        {"511260.SH": 0.67, "510300.SH": 0.063},
        nav=10_000_000.0, ctx=None, target=pd.Timestamp("2024-04-30"))
    assert qty["511260.SH"] == 60900
    # 1000万 × 0.063 / 3 = 210000 shares = 2100 lots × 100
    assert qty["510300.SH"] == 210000
    _reset_adapter_cache()


def test_weights_to_quantities_zero_weight_skipped():
    """权重太小 round 到 0 lots → skip"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._resolve_reference_price = lambda ctx, code, target: 100.0
    # 1万 × 0.0001 / 100 = 0.01 shares → round(0.0001) = 0 lots → skip
    qty = adapter._weights_to_quantities(
        {"159930.SZ": 0.0001}, nav=10_000.0, ctx=None,
        target=pd.Timestamp("2024-04-30"))
    assert "159930.SZ" not in qty
    _reset_adapter_cache()


def test_weights_to_quantities_missing_price_skipped():
    """没价格 → skip"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._resolve_reference_price = lambda ctx, code, target: None
    qty = adapter._weights_to_quantities(
        {"159930.SZ": 0.1}, nav=10_000_000.0, ctx=None,
        target=pd.Timestamp("2024-04-30"))
    assert qty == {}
    _reset_adapter_cache()


# ── Task 14: risk filters ──────────────────────────────────────────────────
class _FakeBlacklistCtx:
    def __init__(self, bl: set[str], market_data: dict | None = None,
                 cash: float = 10_000_000.0, positions: dict | None = None):
        self._bl = bl
        self._market = market_data or {}
        self._cash = cash
        self._positions = positions or {}

    def risk_blacklist(self) -> set[str]: return set(self._bl)
    def cash(self) -> float: return self._cash
    def positions(self) -> dict: return dict(self._positions)
    def market(self, symbol, *, category=None):
        return self._market.get(symbol, pd.DataFrame())


def test_risk_filter_blacklist_removes_symbol(tmp_path):
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._cfg = {"risk_filters": {}}
    out = adapter._apply_risk_filters(
        ctx=_FakeBlacklistCtx({"512890.SH"}),
        target_qty={"512890.SH": 1000, "510300.SH": 2000},
        target=pd.Timestamp("2024-04-30"),
    )
    assert "512890.SH" not in out
    assert out["510300.SH"] == 2000
    _reset_adapter_cache()


def test_risk_filter_max_single_etf_weight_caps_qty(tmp_path):
    """单 ETF 60% × 1000万 nav / 100 价 = 60000 股，cap 到 50% = 50000 股"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._cfg = {"risk_filters": {"max_single_etf_weight": 0.5}}
    adapter._resolve_reference_price = lambda ctx, code, target: 100.0
    adapter._compute_nav = lambda ctx, target: 10_000_000.0
    out = adapter._apply_risk_filters(
        ctx=_FakeBlacklistCtx(set()),
        target_qty={"511260.SH": 60000},
        target=pd.Timestamp("2024-04-30"),
    )
    assert out["511260.SH"] == 50000
    _reset_adapter_cache()


def test_risk_filter_qdii_premium_above_threshold_skips(tmp_path):
    """QDII close 比 20 日均值高 10% > 5% threshold → skip"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._cfg = {"risk_filters": {"qdii_premium_threshold": 0.05}}
    # 给 513500 21 行数据：前 20 行 close=100，最后一行 close=110 → 溢价 10%
    market_data = {
        "513500.SH": pd.DataFrame({
            "trade_date": list(range(20240401, 20240422)),
            "close": [100.0] * 20 + [110.0],
            "volume": [1_000_000] * 21,
        }),
    }
    out = adapter._apply_risk_filters(
        ctx=_FakeBlacklistCtx(set(), market_data=market_data),
        target_qty={"513500.SH": 1000, "510300.SH": 2000},  # 非QDII不受影响
        target=pd.Timestamp("2024-04-30"),
    )
    assert "513500.SH" not in out
    assert out["510300.SH"] == 2000
    _reset_adapter_cache()


def test_risk_filter_qdii_below_threshold_keeps(tmp_path):
    """QDII close 比 20 日均值高 2% < 5% threshold → 保留"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._cfg = {"risk_filters": {"qdii_premium_threshold": 0.05}}
    market_data = {
        "513100.SH": pd.DataFrame({
            "trade_date": list(range(20240401, 20240422)),
            "close": [100.0] * 20 + [102.0],
            "volume": [1_000_000] * 21,
        }),
    }
    out = adapter._apply_risk_filters(
        ctx=_FakeBlacklistCtx(set(), market_data=market_data),
        target_qty={"513100.SH": 1000},
        target=pd.Timestamp("2024-04-30"),
    )
    assert out["513100.SH"] == 1000
    _reset_adapter_cache()


def test_risk_filter_liquidity_skips_when_volume_low(tmp_path):
    """qty=1000, liquidity_multiplier=100 → 需要 ≥100,000 股。
    volume 单位是手(×100股)：vol=500手=50,000股 < 100,000 → skip"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._cfg = {"risk_filters": {"liquidity_multiplier": 100}}
    market_data = {
        "159985.SZ": pd.DataFrame({
            "trade_date": [20240429, 20240430],
            "close": [3.0, 3.0],
            "volume": [400, 500],  # 手 → 50,000 股
        }),
    }
    out = adapter._apply_risk_filters(
        ctx=_FakeBlacklistCtx(set(), market_data=market_data),
        target_qty={"159985.SZ": 1000},  # 需要 100k 股，实际 50k 股
        target=pd.Timestamp("2024-04-30"),
    )
    assert "159985.SZ" not in out
    _reset_adapter_cache()


def test_risk_filter_liquidity_treats_volume_as_lots(tmp_path):
    """volume 单位是手(1手=100股)。vol=50,000手=5,000,000股，qty=1000，
    liq_mul=100 → 需要 100,000 股 → 5M 远超 → 不该 skip。
    (旧 bug: 把 vol 当股 → 50000 < 100000 → 误 skip 掉本来很活跃的 ETF)"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._cfg = {"risk_filters": {"liquidity_multiplier": 100}}
    market_data = {
        "159985.SZ": pd.DataFrame({
            "trade_date": [20240429, 20240430],
            "close": [3.0, 3.0],
            "volume": [40_000, 50_000],  # 手 → 5,000,000 股
        }),
    }
    out = adapter._apply_risk_filters(
        ctx=_FakeBlacklistCtx(set(), market_data=market_data),
        target_qty={"159985.SZ": 1000},  # 需要 100k 股，实际 5M 股 → 充裕
        target=pd.Timestamp("2024-04-30"),
    )
    assert "159985.SZ" in out
    _reset_adapter_cache()


def test_risk_filter_disabled_when_thresholds_none(tmp_path):
    """所有阈值 None / 0 → 全部 target_qty 保留 (blacklist 仍生效)"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._cfg = {"risk_filters": {
        "qdii_premium_threshold": None,
        "liquidity_multiplier": 0,
        "max_single_etf_weight": None,
    }}
    out = adapter._apply_risk_filters(
        ctx=_FakeBlacklistCtx(set()),
        target_qty={"511260.SH": 60000, "513500.SH": 5000},
        target=pd.Timestamp("2024-04-30"),
    )
    assert out == {"511260.SH": 60000, "513500.SH": 5000}
    _reset_adapter_cache()


# ── Task 15: diff + emit + complete run() ─────────────────────────────────
def test_diff_and_emit_only_buys_when_no_current(tmp_path):
    """current 空 → 全 BUY"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._resolve_reference_price = lambda ctx, code, target: 100.0
    sigs = adapter._diff_and_emit(
        ctx=None,
        current={},
        target={"510300.SH": 1000, "511260.SH": 2000},
        target_ts=pd.Timestamp("2024-04-30"),
    )
    assert len(sigs) == 2
    assert all(s.direction == "BUY" for s in sigs)
    qty_by_code = {s.symbol: s.quantity for s in sigs}
    assert qty_by_code == {"510300.SH": 1000, "511260.SH": 2000}
    _reset_adapter_cache()


def test_diff_and_emit_sells_first_then_buys(tmp_path):
    """diff: 卖 510300 / 买 511260 → SELL 在前 BUY 在后"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._resolve_reference_price = lambda ctx, code, target: 100.0
    sigs = adapter._diff_and_emit(
        ctx=None,
        current={"510300.SH": 3000, "511260.SH": 1000},
        target={"510300.SH": 1000, "511260.SH": 5000},
        target_ts=pd.Timestamp("2024-04-30"),
    )
    # 顺序：SELL 510300 (qty 2000) 在 BUY 511260 (qty 4000) 之前
    assert len(sigs) == 2
    assert sigs[0].direction == "SELL" and sigs[0].symbol == "510300.SH" and sigs[0].quantity == 2000
    assert sigs[1].direction == "BUY" and sigs[1].symbol == "511260.SH" and sigs[1].quantity == 4000
    _reset_adapter_cache()


def test_diff_and_emit_no_change_emits_nothing(tmp_path):
    """current == target → 0 signals"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._resolve_reference_price = lambda ctx, code, target: 100.0
    sigs = adapter._diff_and_emit(
        ctx=None,
        current={"510300.SH": 1000, "511260.SH": 2000},
        target={"510300.SH": 1000, "511260.SH": 2000},
        target_ts=pd.Timestamp("2024-04-30"),
    )
    assert sigs == []
    _reset_adapter_cache()


def test_diff_and_emit_close_position(tmp_path):
    """target 不包含某 code → SELL all current"""
    _reset_adapter_cache()
    from plugins.v53_adapter import V53Adapter
    adapter = V53Adapter()
    adapter._resolve_reference_price = lambda ctx, code, target: 100.0
    sigs = adapter._diff_and_emit(
        ctx=None,
        current={"512890.SH": 5000, "510300.SH": 1000},
        target={"510300.SH": 1000},  # 完全 close 512890
        target_ts=pd.Timestamp("2024-04-30"),
    )
    assert len(sigs) == 1
    assert sigs[0].direction == "SELL"
    assert sigs[0].symbol == "512890.SH"
    assert sigs[0].quantity == 5000
    _reset_adapter_cache()


def test_full_run_dry_run_returns_empty_but_logs(tmp_path, monkeypatch, caplog):
    """调仓日（新月首交易日）+ dry_run=true → log 写出目标 + return []，orders 表无新条目"""
    _reset_adapter_cache()
    import logging
    import plugins.v53_adapter as adapter_mod
    from plugins.v53_adapter import V53Adapter
    from app.storage.parquet import ParquetStore
    from app.strategy.context import Context

    # 自带 mini bundle（覆盖到 2024-04-30），避免依赖 gitignore 的真实 bundle
    v53dir = _make_bundle_in_tmp_dir(tmp_path, bundle_end_date="2024-04-30")
    monkeypatch.setattr(adapter_mod, "_V53_DIR", v53dir)

    # 显式强制 dry_run: true 测试 dry-run 路径（不依赖部署配置——生产已切 false）
    cfg_path = v53dir / "config.yaml"
    cfg_path.write_text(
        cfg_path.read_text().replace("dry_run: false", "dry_run: true"))

    # anchor ETF 510300.SH 数据只到 4/30；target=5/6（新月首交易日）→ 月末判断 True
    days_int = [int(d.strftime("%Y%m%d"))
                for d in pd.bdate_range("2024-04-01", "2024-04-30")]
    store = ParquetStore(root=tmp_path / "parquet")
    store.append("etfs", "510300.SH", pd.DataFrame({
        "trade_date": days_int,
        "open": [3.0] * len(days_int), "high": [3.0] * len(days_int),
        "low": [3.0] * len(days_int), "close": [3.0] * len(days_int),
        "volume": [1_000_000] * len(days_int),
    }))

    target_int = 20240506
    ctx = Context(
        instance_id="paper_v53_v53",
        trade_date=target_int,
        virtual_cash=10_000_000.0,
        virtual_positions={},
        parquet_store=store,
    )

    caplog.set_level(logging.INFO, logger="plugins.v53_adapter")
    signals = V53Adapter().run(ctx, target_int)

    # dry_run=true → return []
    assert signals == []
    # 但应有 DRY-RUN log
    log_msgs = [r.getMessage() for r in caplog.records]
    assert any("DRY-RUN" in m for m in log_msgs), f"no DRY-RUN log; logs={log_msgs}"
    _reset_adapter_cache()
