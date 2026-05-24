"""V53Strategy.compute_targets — close_px + target_date → {QMT_code: weight}"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from plugins.v53.strategy import V53Strategy
from plugins.v53.vendor.reference_config import QUADRANT_MAP


INTERNAL_KEYS = [
    "hs300", "cyb", "bond", "gold", "commodity2",
    "commodity3", "crude_oil", "sp500", "nasdaq", "dividend",
]


def _make_close_px(n_days: int = 800, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-01", periods=n_days)
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


def test_compute_targets_returns_qmt_codes():
    """key 是 QMT code (带 .SH/.SZ)，不是 internal key"""
    close_px = _make_close_px()
    strat = V53Strategy()
    weights = strat.compute_targets(close_px, target_date=close_px.index[-1])
    assert isinstance(weights, dict)
    assert all("." in k for k in weights.keys())
    assert "510300.SH" in weights  # hs300
    assert "511260.SH" in weights  # bond


def test_compute_targets_sum_close_to_one():
    close_px = _make_close_px()
    strat = V53Strategy()
    weights = strat.compute_targets(close_px, target_date=close_px.index[-1])
    s = sum(weights.values())
    assert abs(s - 1.0) < 1e-3, f"sum={s}"


def test_bond_high_weight_low_vol():
    """bond 低 vol → 高权重"""
    close_px = _make_close_px()
    strat = V53Strategy()
    weights = strat.compute_targets(close_px, target_date=close_px.index[-1])
    bond_w = weights.get("511260.SH", 0)
    assert bond_w > 0.2, f"bond weight={bond_w}"


def test_dividend_double_counted_via_default_quadrants():
    """默认 QUADRANT_MAP 把 dividend 放在 growth_up + inflation_down"""
    close_px = _make_close_px()
    weights_double = V53Strategy().compute_targets(close_px, target_date=close_px.index[-1])

    single_q = {**QUADRANT_MAP,
                "growth_up": [k for k in QUADRANT_MAP["growth_up"] if k != "dividend"]}
    weights_single = V53Strategy(quadrants=single_q).compute_targets(
        close_px, target_date=close_px.index[-1])

    div_double = weights_double.get("512890.SH", 0)
    div_single = weights_single.get("512890.SH", 0)
    assert div_double > div_single, f"double={div_double} single={div_single}"


def test_target_date_filters_history():
    """target_date 之后的 close 不影响结果"""
    close_px = _make_close_px()
    mid = close_px.index[400]
    strat = V53Strategy()
    w_at_mid = strat.compute_targets(close_px, target_date=mid)
    # 截断到 mid 前的 close_px 应给相同结果
    w_at_mid_truncated = strat.compute_targets(close_px.loc[:mid], target_date=mid)
    for k in w_at_mid:
        assert abs(w_at_mid[k] - w_at_mid_truncated[k]) < 1e-9, f"key={k}"


def test_compute_targets_skips_zero_weight():
    """如果某个 internal key vendor 给了 0 权重，输出 dict 不应该包含它"""
    # 用一个 quadrant_map 完全排除 'crude_oil'，应该不在结果里
    no_crude = {q: [k for k in keys if k != "crude_oil"]
                for q, keys in QUADRANT_MAP.items()}
    close_px = _make_close_px()
    weights = V53Strategy(quadrants=no_crude).compute_targets(
        close_px, target_date=close_px.index[-1])
    assert "159930.SZ" not in weights  # crude_oil 不在
