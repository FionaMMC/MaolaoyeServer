"""V53Strategy: 薄壳 wrapper over vendor.compute_baseline.

负责：
- 接受 close_px (wide DataFrame, columns = internal key) + target_date
- 生成月末 rebalance dates 从 close_px 起点到 target_date
- 调 vendor.compute_baseline
- 取最后一期 asset weights，翻译 internal_key → QMT code
"""
from __future__ import annotations

import pandas as pd

from plugins.v53.code_map import V53_KEY_TO_QMT
from plugins.v53.vendor.reference_config import QUADRANT_MAP
from plugins.v53.vendor.weight_methods import compute_baseline


class V53Strategy:
    """v53 算法薄壳。每次 compute_targets() 都会 recompute 全部历史的权重，
    这是 vendor compute_baseline 的设计 —— 为了 backward-bias fix 必须知道
    历史所有 rebalance 段的累计 quadrant 收益。"""

    def __init__(
        self,
        quadrants: dict[str, list[str]] | None = None,
        method: str = "inv_vol",
    ):
        self.quadrants = quadrants if quadrants is not None else QUADRANT_MAP
        self.method = method

    def compute_targets(
        self,
        close_px: pd.DataFrame,
        target_date: pd.Timestamp,
    ) -> dict[str, float]:
        """计算 target_date 的目标权重 {QMT_code: weight}。

        Args:
            close_px: wide DataFrame indexed by date, columns = internal key
                     (hs300, cyb, bond, ...)。必须含 target_date 及之前的足够历史。
            target_date: 调仓日。

        Returns:
            {QMT_code (510300.SH 等): weight}, sum ≈ 1.0。权重为 0 的 asset 不出现。
            未在 V53_KEY_TO_QMT 里的 internal key 也不出现。
        """
        target_ts = pd.Timestamp(target_date)
        close_to_target = close_px[close_px.index <= target_ts]
        if close_to_target.empty:
            return {}

        rebal_dates = self._monthly_rebal_dates(close_to_target)
        if not rebal_dates:
            return {}

        etf_codes = close_to_target.columns.tolist()
        asset_df, _, _ = compute_baseline(
            close_px=close_to_target,
            rebalance_dates=rebal_dates,
            etf_codes=etf_codes,
            quadrant_map=self.quadrants,
            method=self.method,
        )

        last_w = asset_df.iloc[-1].to_dict()
        return {
            V53_KEY_TO_QMT[k]: float(w)
            for k, w in last_w.items()
            if k in V53_KEY_TO_QMT and w > 0
        }

    @staticmethod
    def _monthly_rebal_dates(close_px: pd.DataFrame) -> list[pd.Timestamp]:
        """从 close_px 每个月的最后一个 trade_date 作为 rebalance anchor。"""
        if close_px.empty:
            return []
        idx = close_px.index
        by_month = pd.Series(idx, index=idx).groupby([idx.year, idx.month])
        return [grp.max() for _, grp in by_month]
