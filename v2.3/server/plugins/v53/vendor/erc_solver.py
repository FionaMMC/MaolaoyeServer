# VENDORED from magicboom1/permenant_portfolio master @ e55e0b1fadac4b10e030dd78a4c9435620dc9046
# DO NOT EDIT — sync only via Mac local refresh_v53_bundle.sh (vendor copy mode).
# See: docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md §4 (vendor)

"""
v41/erc_solver.py — ERC 求解器（阻尼固定点迭代）

ERC 条件: w_i * (Σw)_i = 常数 对所有 i
等价于: w_i ∝ 1 / (Σw)_i

用阻尼迭代抑制振荡: w_new = (1-α)*w_old + α*w_new_raw
对强相关性矩阵稳定收敛。
"""
import numpy as np


def erc_weights_robust(returns, max_iter=1000, tol=1e-12):
    """
    ERC 权重（阻尼固定点迭代）。

    对正定协方差矩阵保证收敛到唯一解。
    """
    n = returns.shape[1]
    if n == 0:
        return np.array([])
    if n == 1:
        return np.array([1.0])

    cov = returns.cov().values
    # 正则化防止奇异
    reg = 1e-8 * np.trace(cov) / n
    cov_reg = cov + np.eye(n) * reg

    w = np.ones(n) / n  # 等权初值
    alpha = 0.3  # 阻尼系数（< 0.5 保证稳定）

    for iteration in range(max_iter):
        sw = cov_reg @ w
        # 原始更新
        w_new_raw = 1.0 / np.maximum(sw, 1e-12)
        w_new_raw = w_new_raw / w_new_raw.sum()

        # 阻尼
        w_new = (1.0 - alpha) * w + alpha * w_new_raw

        delta = np.max(np.abs(w_new - w))
        w = w_new
        if delta < tol:
            break

    # RC 均匀性校验
    sw = cov_reg @ w
    pv = np.sqrt(max(w @ sw, 1e-12))
    if pv > 0:
        rc = w * sw / pv
        rc_cv = float(np.std(rc) / np.mean(rc))
        if rc_cv > 0.15:  # RC 变异系数 > 15% → 不收敛，退化为 inv_vol
            vols = returns.std(ddof=1).values
            vols = np.where(vols < 1e-8, 1e-8, vols)
            w = 1.0 / vols
            w = w / w.sum()

    return w
