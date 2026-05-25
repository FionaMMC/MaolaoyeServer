"""V53 内部 key (vendor.reference_config 用) ↔ QMT 6 位代码 + 后缀 映射"""
from __future__ import annotations

# 顺序固定，便于 returns 矩阵列名稳定
ETF_KEYS: list[str] = [
    "hs300", "cyb", "bond", "gold", "commodity2",
    "commodity3", "crude_oil", "sp500", "nasdaq", "dividend",
]

V53_KEY_TO_QMT: dict[str, str] = {
    "hs300":      "510300.SH",
    "cyb":        "159915.SZ",
    "bond":       "511260.SH",
    "gold":       "518880.SH",
    "commodity2": "159981.SZ",
    "commodity3": "159985.SZ",
    "crude_oil":  "159930.SZ",
    "sp500":      "513500.SH",
    "nasdaq":     "513100.SH",
    "dividend":   "512890.SH",
}

QMT_TO_V53_KEY: dict[str, str] = {q: k for k, q in V53_KEY_TO_QMT.items()}

QDII_QMT_CODES: set[str] = {"513500.SH", "513100.SH"}

assert set(ETF_KEYS) == set(V53_KEY_TO_QMT.keys()), "ETF_KEYS 和 V53_KEY_TO_QMT 不一致"
