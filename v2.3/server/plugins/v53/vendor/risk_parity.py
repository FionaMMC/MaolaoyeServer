# VENDORED from magicboom1/permenant_portfolio master @ e55e0b1fadac4b10e030dd78a4c9435620dc9046
# DO NOT EDIT — sync only via Mac local refresh_v53_bundle.sh (vendor copy mode).
# See: docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md §4 (vendor)

"""
v4/engine/risk_parity.py — 风险平价权重求解器

支持两种方法：
  - inv_vol: 波动率倒数加权（快速、鲁棒、相关为0时等价于ERC）
  - erc:     风险等权（Equal Risk Contribution），需 scipy.optimize

关键约束：所有输入数据右边界 = 调仓日，不包含任何未来信息。
"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize


def inv_vol_weights(returns: pd.DataFrame) -> np.ndarray:
    """
    波动率倒数加权。

    w_i = (1/σ_i) / Σ(1/σ_j)

    当各资产相关性为0时，与风险等权（ERC）等价。
    """
    vols = returns.std(ddof=1).values
    vols = np.where(vols < 1e-8, 1e-8, vols)  # 防止除零
    inv_vols = 1.0 / vols
    weights = inv_vols / inv_vols.sum()
    return weights


def _erc_risk_contributions(weights: np.ndarray, cov: np.ndarray) -> np.ndarray:
    """计算各资产的风险贡献 RC_i = w_i * (Σw)_i / σ_p"""
    w = np.asarray(weights, dtype=float)
    sigma_w = cov @ w
    port_vol = np.sqrt(w @ sigma_w)
    if port_vol < 1e-12:
        return np.zeros_like(w)
    rc = w * sigma_w / port_vol
    return rc


def _erc_objective(weights: np.ndarray, cov: np.ndarray) -> float:
    """ERC优化目标：最小化风险贡献的方差"""
    rc = _erc_risk_contributions(weights, cov)
    n = len(rc)
    target = 1.0 / n
    return float(np.sum((rc - target) ** 2))


def erc_weights(returns: pd.DataFrame) -> np.ndarray:
    """
    风险等权（ERC）权重。

    求解权重使得每个资产的风险贡献相等：
      RC_i = w_i * (Σw)_i / sqrt(w^T Σ w) = 1/N

    使用 scipy.optimize 最小化 Σ(RC_i - 1/N)²。
    约束：w_i >= 0, Σw_i = 1
    """
    n = returns.shape[1]
    if n == 0:
        return np.array([])
    if n == 1:
        return np.array([1.0])

    cov = returns.cov().values

    # 初始权重 = 等权
    w0 = np.ones(n) / n
    bounds = [(0.0, 1.0)] * n
    constraints = {"type": "eq", "fun": lambda w: np.sum(w) - 1.0}

    try:
        result = minimize(
            _erc_objective,
            w0,
            args=(cov,),
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 500, "ftol": 1e-12},
        )
        if result.success:
            w = result.x
            w = np.clip(w, 0, None)
            w = w / w.sum()
        else:
            # 优化失败，退化为波动率倒数加权
            w = inv_vol_weights(returns)
    except Exception:
        w = inv_vol_weights(returns)

    return w


def solve_risk_parity(
    returns: pd.DataFrame,
    method: str = "inv_vol",
) -> np.ndarray:
    """
    求解风险平价权重。

    Parameters
    ----------
    returns : pd.DataFrame
        历史日收益率，行=日期，列=资产。右边界必须是调仓日。
    method : str
        "inv_vol" — 波动率倒数加权
        "erc"    — 风险等权（需scipy）

    Returns
    -------
    np.ndarray : 权重向量，和=1
    """
    # 只保留数据完整的资产（列），并去除含有NaN的行
    returns_clean = returns.dropna(axis=0)  # 删除含有NaN的行
    if len(returns_clean) < 20:
        return np.array([])
    valid_cols = returns_clean.columns[returns_clean.isna().sum() == 0]
    if len(valid_cols) == 0:
        return np.array([])
    returns_valid = returns_clean[valid_cols]

    if method == "erc":
        w_valid = erc_weights(returns_valid)
    else:
        w_valid = inv_vol_weights(returns_valid)

    # 重建完整权重向量（缺失资产权重=0）
    full_weights = np.zeros(len(returns.columns))
    for i, col in enumerate(returns.columns):
        if col in valid_cols:
            j = list(valid_cols).index(col)
            full_weights[i] = w_valid[j]

    return full_weights
