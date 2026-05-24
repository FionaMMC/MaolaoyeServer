"""Vendor compute_baseline sanity tests.

不验证算法精度（那是同事的责任），只验证 vendor 集成的"调得通 + 返回结构正确"。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from plugins.v53.vendor.weight_methods import compute_baseline
from plugins.v53.vendor.reference_config import (
    ETF_CODES, QUADRANT_MAP, RISK_PARITY_WINDOW, MIN_HISTORY_DAYS,
)


INTERNAL_KEYS = list(ETF_CODES.keys())


def _make_fake_close_px(n_days: int = 800, seed: int = 0) -> pd.DataFrame:
    """生成 10 个 internal key 的伪 close 序列。
    需要 ≥ RISK_PARITY_WINDOW (252) + 一些 warmup 来跑 inv_vol。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-01", periods=n_days)
    # 不同 vol 让 inv_vol 有区分（bond 最低，cyb/crude_oil 最高）
    vol_by_key = {
        "hs300": 0.012, "cyb": 0.020, "bond": 0.003, "gold": 0.010,
        "commodity2": 0.014, "commodity3": 0.012, "crude_oil": 0.022,
        "sp500": 0.011, "nasdaq": 0.013, "dividend": 0.009,
    }
    data = {
        k: 100 * np.exp(np.cumsum(rng.normal(0.0003, vol_by_key[k], n_days)))
        for k in INTERNAL_KEYS
    }
    return pd.DataFrame(data, index=dates)


def _monthly_rebal_dates(close_px: pd.DataFrame, start_offset: int = 280) -> list:
    """从 start_offset 开始，每月最后一个 trade_date 作为 rebal anchor。"""
    sub = close_px.iloc[start_offset:]
    by_month = sub.groupby([sub.index.year, sub.index.month])
    return [grp.index.max() for _, grp in by_month]


def test_vendor_constants_loaded():
    assert RISK_PARITY_WINDOW == 252
    assert MIN_HISTORY_DAYS == 126
    assert set(QUADRANT_MAP.keys()) == {
        "growth_up", "growth_down", "inflation_up", "inflation_down",
    }
    assert len(INTERNAL_KEYS) == 10
    assert "dividend" in INTERNAL_KEYS  # v53 加的红利低波


def test_compute_baseline_returns_tuple_of_three():
    close_px = _make_fake_close_px()
    rebal = _monthly_rebal_dates(close_px)
    assert len(rebal) >= 3, "需要至少 3 个月才能看到 layer-2 inv_vol 效果"

    result = compute_baseline(
        close_px=close_px,
        rebalance_dates=rebal,
        etf_codes=INTERNAL_KEYS,
        quadrant_map=QUADRANT_MAP,
        method="inv_vol",
    )
    assert isinstance(result, tuple)
    assert len(result) == 3
    asset_df, quadrant_df, qaw_dfs = result
    assert isinstance(asset_df, pd.DataFrame)
    assert isinstance(quadrant_df, pd.DataFrame)
    assert isinstance(qaw_dfs, dict)


def test_asset_weights_sum_to_one_on_last_rebal():
    close_px = _make_fake_close_px()
    rebal = _monthly_rebal_dates(close_px)

    asset_df, _, _ = compute_baseline(
        close_px=close_px,
        rebalance_dates=rebal,
        etf_codes=INTERNAL_KEYS,
        quadrant_map=QUADRANT_MAP,
        method="inv_vol",
    )
    last = asset_df.iloc[-1]
    s = last.sum()
    assert abs(s - 1.0) < 1e-3, f"asset weights sum={s}, expected ≈ 1"


def test_bond_gets_high_weight_low_vol():
    """bond 是最低 vol，inv_vol 应该给它很高的权重（>20%）。"""
    close_px = _make_fake_close_px()
    rebal = _monthly_rebal_dates(close_px)

    asset_df, _, _ = compute_baseline(
        close_px=close_px,
        rebalance_dates=rebal,
        etf_codes=INTERNAL_KEYS,
        quadrant_map=QUADRANT_MAP,
        method="inv_vol",
    )
    last = asset_df.iloc[-1]
    bond_w = float(last.get("bond", 0))
    assert bond_w > 0.2, f"bond weight={bond_w}, 期望 > 0.2"


def test_cyb_and_crude_oil_get_low_weight_high_vol():
    """cyb 和 crude_oil 是最高 vol，inv_vol 应该给它们很低权重（<10%）。"""
    close_px = _make_fake_close_px()
    rebal = _monthly_rebal_dates(close_px)

    asset_df, _, _ = compute_baseline(
        close_px=close_px,
        rebalance_dates=rebal,
        etf_codes=INTERNAL_KEYS,
        quadrant_map=QUADRANT_MAP,
        method="inv_vol",
    )
    last = asset_df.iloc[-1]
    cyb_w = float(last.get("cyb", 0))
    crude_w = float(last.get("crude_oil", 0))
    assert cyb_w < 0.1, f"cyb weight={cyb_w}, 期望 < 0.1"
    assert crude_w < 0.1, f"crude_oil weight={crude_w}, 期望 < 0.1"


def test_dividend_double_counted_in_two_quadrants():
    """v53 设计意图：dividend 在 growth_up + inflation_down 两象限重复计入。
    去掉 growth_up 里的 dividend 后，权重应该下降。"""
    close_px = _make_fake_close_px()
    rebal = _monthly_rebal_dates(close_px)

    # 原版（双象限）
    asset_df_double, _, _ = compute_baseline(
        close_px=close_px, rebalance_dates=rebal,
        etf_codes=INTERNAL_KEYS, quadrant_map=QUADRANT_MAP, method="inv_vol",
    )
    div_w_double = float(asset_df_double.iloc[-1].get("dividend", 0))

    # 单象限版（只放 inflation_down）
    single_quad = {**QUADRANT_MAP,
                   "growth_up": [k for k in QUADRANT_MAP["growth_up"] if k != "dividend"]}
    asset_df_single, _, _ = compute_baseline(
        close_px=close_px, rebalance_dates=rebal,
        etf_codes=INTERNAL_KEYS, quadrant_map=single_quad, method="inv_vol",
    )
    div_w_single = float(asset_df_single.iloc[-1].get("dividend", 0))

    assert div_w_double > div_w_single, (
        f"双象限 dividend weight={div_w_double} 应 > 单象限 {div_w_single}"
    )
