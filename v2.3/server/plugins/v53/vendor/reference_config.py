# VENDORED from magicboom1/permenant_portfolio master @ e55e0b1fadac4b10e030dd78a4c9435620dc9046
# DO NOT EDIT — sync only via Mac local refresh_v53_bundle.sh (vendor copy mode).
# See: docs/superpowers/specs/2026-05-24-v53-allweather-bottom-integration-design.md §4 (vendor)

"""
v53/config.py — 10资产全天候（v48 + 红利低波100 ETF）

新增资产:
  dividend: 512890.SH（红利低波100ETF，2019-01-18上市）
  归属象限: growth_up / inflation_down（价值因子，低波动红利）

版本对比基准: v48（9资产，夏普1.59，最大回撤-7.74%）
"""
BACKTEST_START = "2019-12-05"
BACKTEST_END   = "2026-04-30"
RISK_FREE_RATE  = 0.015
COMMISSION_RATE = 0.00015
SLIPPAGE_RATE   = 0.0005
RISK_PARITY_WINDOW = 252
MIN_HISTORY_DAYS   = 126

ETF_CODES = {
    "hs300":      "510300.SH",
    "cyb":        "159915.SZ",
    "bond":       "511260.SH",   # 10Y国债
    "gold":       "518880.SH",
    "commodity2": "159981.SZ",
    "commodity3": "159985.SZ",
    "crude_oil":  "159930.SZ",
    "sp500":      "513500.SH",
    "nasdaq":     "513100.SH",
    "dividend":   "512890.SH",   # 红利低波100（新增）
}

# 红利低波归入 growth_up（作为股票多元化）和 inflation_down（低波动防御特征）
QUADRANT_MAP = {
    "growth_up":    ["hs300", "cyb", "gold", "commodity2", "commodity3", "sp500", "nasdaq", "dividend"],
    "growth_down":  ["bond", "gold"],
    "inflation_up": ["gold", "commodity2", "commodity3", "crude_oil"],
    "inflation_down": ["hs300", "bond", "dividend"],
}

ASSET_NAMES = {
    "hs300": "沪深300", "cyb": "创业板指",
    "bond": "10年国债", "gold": "黄金",
    "commodity2": "能源化工", "commodity3": "豆粕",
    "crude_oil": "原油ETF", "sp500": "标普500", "nasdaq": "纳斯达克",
    "dividend": "红利低波100",
}

QUADRANT_NAMES = {
    "growth_up": "增长上行", "growth_down": "增长下行",
    "inflation_up": "通胀上行", "inflation_down": "通胀下行",
}

# ── 债券桥接参数（10Y，保持不变）──
BOND_10Y_CODE   = "511260.SH"
BOND_5Y_CODE    = "511010.SH"
BOND_BRIDGE_DATE = "2017-08-24"
