"""V53 适配器 — 月末双层 inv_vol 全天候调仓 (10 ETF)。

Phase M0: dry_run=true 模式，月末会 log 目标权重 + diff，但 return []
Phase M1: dry_run=false，输出真实 RawSignal[]

Task 11 实现：骨架 + 月末判断 + 资源加载 + dry_run 短路。
Task 12-15 会填实 _build_returns_matrix / weight→qty / 风控 / diff.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

import pandas as pd
import yaml

from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_V53_DIR = _HERE / "v53"


class V53Adapter(Strategy):
    """V53 v1.0 适配器 — Phase M0 dry_run。"""

    name = "v53"
    data_dir: ClassVar[Path | None] = _V53_DIR / "data"
    data_files: ClassVar[list[str]] = ["etf_close.parquet", "etf_meta.parquet"]

    # class-level 缓存（首次 _load_resources 后保留）
    _cfg: ClassVar[dict | None] = None
    _etf_close_bundle: ClassVar[pd.DataFrame | None] = None
    _etf_meta: ClassVar[pd.DataFrame | None] = None

    def _load_resources(self) -> None:
        """懒加载 config + bundled data。各自独立加载。"""
        if type(self)._cfg is None:
            with (_V53_DIR / "config.yaml").open() as f:
                type(self)._cfg = yaml.safe_load(f)
        if type(self)._etf_close_bundle is None:
            df = pd.read_parquet(_V53_DIR / "data" / "etf_close.parquet")
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            type(self)._etf_close_bundle = df
        if type(self)._etf_meta is None:
            type(self)._etf_meta = pd.read_parquet(_V53_DIR / "data" / "etf_meta.parquet")

    def _is_month_end(self, ctx: Context, target: pd.Timestamp) -> bool:
        """从 anchor ETF 的 trade_date 序列推：target 是其当月最大 trade_date 吗？

        ctx.market 返回 trade_date 是 int YYYYMMDD，需要转 datetime 才能比 year/month。
        """
        anchor = (type(self)._cfg or {}).get("month_end_anchor_etf", "510300.SH")
        try:
            df = ctx.market(anchor, category="etfs")
        except Exception:
            return False
        if df is None or df.empty:
            return False
        # trade_date 是 int YYYYMMDD
        td = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
        same_month = td[(td.dt.year == target.year) & (td.dt.month == target.month)]
        if same_month.empty:
            return False
        return target == same_month.max()

    def _build_close_matrix(
        self, ctx: Context, target: pd.Timestamp,
    ) -> pd.DataFrame:
        """拼 bundle (datetime trade_date) + IngestService (int YYYYMMDD trade_date) 增量。

        Args:
            ctx: 当前调用上下文
            target: 调仓目标日期 (pd.Timestamp)

        Returns:
            close_px wide DataFrame:
              - index = datetime, ≤ target
              - columns = v53 internal keys (hs300, cyb, ...) — vendor 直接消费
              - values = close
            如果某个 ETF 完全无数据，该列将不出现。
        """
        from plugins.v53.code_map import ETF_KEYS, QMT_TO_V53_KEY, V53_KEY_TO_QMT

        # 1. bundle (long format, QMT code, datetime trade_date)
        bundle = type(self)._etf_close_bundle
        if bundle is None:
            return pd.DataFrame()
        bundle = bundle[bundle["trade_date"] <= target][["trade_date", "code", "close"]]
        bundle_end = bundle["trade_date"].max() if not bundle.empty else pd.Timestamp("1900-01-01")

        # 2. IngestService 增量 (trade_date 是 int YYYYMMDD)
        incr_pieces = []
        for qmt_code in V53_KEY_TO_QMT.values():
            try:
                df = ctx.market(qmt_code, category="etfs")
            except Exception:
                continue
            if df is None or df.empty:
                continue
            df = df.copy()
            # int YYYYMMDD → datetime
            df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
            # 只取 bundle_end 之后到 target 之间的部分（严格大于 bundle_end 避免重复）
            df = df[(df["trade_date"] > bundle_end) & (df["trade_date"] <= target)]
            if df.empty:
                continue
            df["code"] = qmt_code
            incr_pieces.append(df[["trade_date", "code", "close"]])

        combined = (
            pd.concat([bundle, *incr_pieces], ignore_index=True)
            if incr_pieces else bundle
        )
        if combined.empty:
            return pd.DataFrame()

        # 3. long → wide; 列名 QMT code → internal key
        wide = combined.pivot_table(
            index="trade_date", columns="code", values="close", aggfunc="last")
        wide = wide.sort_index()
        wide = wide.rename(columns=QMT_TO_V53_KEY)

        # 4. 只保留 v53 关心的 10 个 internal key 顺序
        keep = [k for k in ETF_KEYS if k in wide.columns]
        return wide[keep]

    def _resolve_reference_price(
        self, ctx: Context, qmt_code: str, target: pd.Timestamp,
    ) -> float | None:
        """优先 ctx.market 最近真实 close (≤ target)，回退 bundle close."""
        # 1. ctx.market path (trade_date 是 int YYYYMMDD)
        try:
            df = ctx.market(qmt_code, category="etfs")
        except Exception:
            df = None
        if df is not None and not df.empty:
            td = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d")
            df2 = df.assign(_dt=td)
            df2 = df2[df2["_dt"] <= target].sort_values("_dt")
            if not df2.empty:
                price = float(df2.iloc[-1]["close"])
                if price > 0:
                    return price

        # 2. bundle fallback
        bundle = type(self)._etf_close_bundle
        if bundle is None:
            return None
        sub = bundle[(bundle["code"] == qmt_code) & (bundle["trade_date"] <= target)]
        if sub.empty:
            return None
        price = float(sub.sort_values("trade_date").iloc[-1]["close"])
        return price if price > 0 else None

    def _compute_nav(self, ctx: Context, target: pd.Timestamp) -> float:
        """NAV = cash + Σ(qty × ref_price)。"""
        nav = float(ctx.cash())
        for code, qty in ctx.positions().items():
            price = self._resolve_reference_price(ctx, code, target)
            if price is None:
                continue
            nav += float(qty) * float(price)
        return nav

    def _weights_to_quantities(
        self, weights: dict[str, float], nav: float,
        ctx: Context, target: pd.Timestamp,
    ) -> dict[str, int]:
        """权重 dict → 100 股整 数量 dict。权重 0 / 无价格 / qty<100 都 skip。"""
        out: dict[str, int] = {}
        for qmt_code, w in weights.items():
            if w <= 0 or nav <= 0:
                continue
            price = self._resolve_reference_price(ctx, qmt_code, target)
            if price is None or price <= 0:
                continue
            lots = round(nav * w / price / 100)
            if lots > 0:
                out[qmt_code] = int(lots) * 100
        return out

    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        """Phase M0 dry_run 骨架。Task 12-15 会填实完整调仓 pipeline。"""
        target = pd.to_datetime(str(trade_date), format="%Y%m%d")

        # 1. 加载 config + bundle（失败则优雅退化）
        try:
            self._load_resources()
        except Exception as e:
            logger.warning("V53 资源加载失败 (bundle 可能未上传): %s", e)
            return []

        # 2. 月末判断（非月末直接 return []，不做任何重计算）
        if not self._is_month_end(ctx, target):
            return []

        # 3. Task 12-15 将在此处填实：拼数据 → 算权重 → diff → emit RawSignal[]
        # 当前阶段只 log 月末识别，不发信号
        logger.info(
            "V53[%s] month-end detected on %s, awaiting Task 12-15 full pipeline",
            ctx.instance_id, trade_date,
        )
        return []
