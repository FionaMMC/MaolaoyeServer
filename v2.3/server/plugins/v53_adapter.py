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
