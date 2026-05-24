# VENDORED from magicboom1/permenant_portfolio master @ e55e0b1fadac4b10e030dd78a4c9435620dc9046
# DO NOT EDIT — sync only via Mac local refresh_v53_bundle.sh (vendor copy mode).
# See: docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md §4 (vendor)

"""
v48/weight_methods.py — 两层风险平价权重计算（修复版）

相对 v41/weight_methods.py 的核心修复:
  _compute_quadrant_returns 改为逐段累计（per-segment），
  避免用单一截面权重计算整段历史象限收益的 backward bias。

Meican review 指出的问题:
  原版: prev_q_weights（单一截面）应用到整段 daily_ret，再切 252 天
  修复: 每段调仓期间使用该期间活跃的权重，累计得到真实路径收益
"""
import numpy as np
import pandas as pd

from .reference_config import QUADRANT_MAP, RISK_PARITY_WINDOW, MIN_HISTORY_DAYS
from .risk_parity import solve_risk_parity as _solve_risk_parity_v4
from .erc_solver import erc_weights_robust


def _solve_risk_parity(returns, method="inv_vol"):
    if method == "erc":
        return erc_weights_robust(returns)
    return _solve_risk_parity_v4(returns, method=method)


def _is_month_end(dates, i):
    if i >= len(dates) - 1:
        return True
    return dates[i].month != dates[i + 1].month


def _intra_quadrant_step(ret_window, quadrant_map, close_px, method, date_loc):
    """象限内风险平价。同 v41 实现。"""
    q_weights_current = {}
    qaw_store = {q: [] for q in quadrant_map}

    for q_name, q_assets in quadrant_map.items():
        valid_assets = [a for a in q_assets if a in close_px.columns]
        if not valid_assets:
            q_weights_current[q_name] = {}
            continue

        tradable = []
        fallback = []
        for a in valid_assets:
            col_ret = ret_window[a].dropna()
            if len(col_ret) >= MIN_HISTORY_DAYS:
                tradable.append(a)
            elif len(col_ret) >= 20:
                fallback.append(a)

        if not tradable:
            if fallback:
                eq_w = 1.0 / len(fallback)
                w_dict = dict.fromkeys(fallback, eq_w)
                q_weights_current[q_name] = w_dict
            else:
                q_weights_current[q_name] = {}
            continue

        q_w = _solve_risk_parity(ret_window[tradable], method=method)
        w_dict = dict(zip(tradable, q_w))
        q_weights_current[q_name] = w_dict
        qaw_store[q_name].append({"date": close_px.index[date_loc], **w_dict})

    return q_weights_current, qaw_store


def _compute_q_returns_segment(daily_ret, q_weights, quadrant_map,
                                seg_start, seg_end):
    """
    计算一段时期 [seg_start, seg_end] 的象限组合日收益率。
    使用该段时期活跃的权重（来自上一次调仓）。

    这是修复 backward bias 的核心：
    原版用单个截面权重计算整段历史，修复后按段累计。
    """
    segment_ret = daily_ret.loc[seg_start:seg_end]
    q_returns = {}
    for q_name in quadrant_map:
        pw = q_weights.get(q_name)
        if pw is None or len(pw) == 0:
            continue
        q_ret = pd.Series(0.0, index=segment_ret.index)
        for asset, w in pw.items():
            if asset in segment_ret.columns and w > 0:
                q_ret += w * segment_ret[asset].fillna(0.0)
        q_returns[q_name] = q_ret
    return pd.DataFrame(q_returns)


def _inter_quadrant_rp(q_ret_window, quadrant_names, method):
    """象限间风险平价。同 v41 实现，增加空数据时的等权回退。"""
    valid_q, fallback_q = [], []
    for qn in quadrant_names:
        if qn in q_ret_window.columns:
            col = q_ret_window[qn].dropna()
            if len(col) >= MIN_HISTORY_DAYS:
                valid_q.append(qn)
            elif len(col) >= 20:
                fallback_q.append(qn)

    # 空数据 → 等权回退（v41 原版返回全 0，导致首期权重丢失）
    if not valid_q and not fallback_q:
        return dict.fromkeys(quadrant_names, 1.0 / len(quadrant_names))

    if len(valid_q) >= 2:
        q_w = _solve_risk_parity(q_ret_window[valid_q], method=method)
        return dict(zip(valid_q, q_w))
    elif len(valid_q) == 1:
        qw = dict.fromkeys(quadrant_names, 0.0)
        qw[valid_q[0]] = 1.0
        return qw
    elif fallback_q:
        eq = 1.0 / len(fallback_q)
        qw = dict.fromkeys(quadrant_names, 0.0)
        for qn in fallback_q:
            qw[qn] = eq
        return qw
    else:
        return dict.fromkeys(quadrant_names, 0.0)


def _map_to_assets(qw_dict, q_weights_current, all_assets):
    """还原到底层资产权重。同 v41。"""
    asset_w = dict.fromkeys(all_assets, 0.0)
    for q_name, q_weight in qw_dict.items():
        if q_weight > 0:
            q_aw = q_weights_current.get(q_name, {})
            for a, w in q_aw.items():
                if a in asset_w:
                    asset_w[a] += q_weight * w
    total = sum(asset_w.values())
    if total > 0:
        asset_w = {a: w / total for a, w in asset_w.items()}
    return asset_w


def compute_baseline(close_px, rebalance_dates,
                     etf_codes=None, quadrant_map=None, method="inv_vol"):
    """
    两层风险平价（inv_vol/erc）。
    修复版: 象限收益率按段累计，避免 backward bias。
    """
    if quadrant_map is None:
        quadrant_map = QUADRANT_MAP
    quadrant_names = list(quadrant_map.keys())
    all_assets = list(close_px.columns)

    daily_ret = close_px.pct_change()

    aw_list, qw_list = [], []
    qaw_store = {q: [] for q in quadrant_names}

    # ═══ 修复 #1: 逐段累计象限收益 ═══
    accumulated_q_returns = pd.DataFrame()
    prev_date = None
    prev_q_weights = {q: {} for q in quadrant_names}

    for i, date in enumerate(rebalance_dates):
        date_loc = close_px.index.get_loc(date)
        ret_start = max(1, date_loc - RISK_PARITY_WINDOW + 1)
        ret_window = daily_ret.iloc[ret_start:date_loc + 1]

        # 步骤1: 象限内 RP
        q_weights_current, q_store = _intra_quadrant_step(
            ret_window, quadrant_map, close_px, method, date_loc)
        for q in quadrant_names:
            qaw_store[q].extend(q_store.get(q, []))

        # 步骤2: 累计上一段 [prev_date, date] 的象限收益（用上一期的权重）
        if prev_date is not None:
            segment = _compute_q_returns_segment(
                daily_ret, prev_q_weights, quadrant_map, prev_date, date)
            # 非首段时去掉第一行（prev_date 已是上一段的 seg_end，避免重复计入）
            if not accumulated_q_returns.empty:
                segment = segment.iloc[1:]
            accumulated_q_returns = pd.concat(
                [accumulated_q_returns, segment])

        # 步骤3: 象限间 RP（用累计的真实路径收益）
        if len(accumulated_q_returns) > 0:
            q_ret_window = accumulated_q_returns.iloc[
                max(0, len(accumulated_q_returns) - RISK_PARITY_WINDOW):]
            qw_dict = _inter_quadrant_rp(q_ret_window, quadrant_names, method)
        else:
            # 首期无历史 → 等权
            qw_dict = dict.fromkeys(
                quadrant_names, 1.0 / len(quadrant_names))

        qw_list.append({"date": date, **qw_dict})

        # 更新状态给下一段使用
        prev_date = date
        prev_q_weights = {
            q: dict(q_weights_current.get(q, {})) for q in quadrant_names
        }

        # 步骤4: 还原底层
        asset_w = _map_to_assets(qw_dict, q_weights_current, all_assets)
        aw_list.append({"date": date, **asset_w})

    asset_df = pd.DataFrame(aw_list).set_index("date")
    quadrant_df = pd.DataFrame(qw_list).set_index("date")
    qaw_dfs = {
        q: pd.DataFrame(records).set_index("date")
        for q, records in qaw_store.items() if records
    }
    return asset_df, quadrant_df, qaw_dfs
