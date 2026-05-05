"""
v2.3 共享配置。
基于 v2.2 扩展：新增数据采集系统（QMT 全量 + AKShare 宏观），
Parquet 本地存储，增量更新。PUSH_MODE="local" 默认。
"""

import json
import logging
import os
from logging.handlers import TimedRotatingFileHandler

import yaml

# ── 路径 ──────────────────────────────────────────────────────────────────────
QMT_USERDATA_DIR = r"C:\parttime\平安证券量盈QMT策略交易平台\userdata_mini"

_V23_DIR        = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(_V23_DIR, "pipeline.db")
STRATEGIES_FILE = os.path.join(_V23_DIR, "server", "strategies.yaml")
LOG_DIR         = os.path.join(_V23_DIR, "logs")
LOCAL_PUSH_DIR  = os.path.join(_V23_DIR, "local_push")
MOCK_DATA_DIR   = os.path.join(_V23_DIR, "mock_data")
DATA_DIR        = os.path.join(_V23_DIR, "data")
COLLECTORS_YAML = os.path.join(_V23_DIR, "collectors.yaml")

# ── QMT 交易会话 ───────────────────────────────────────────────────────────────
QMT_SESSION_ID = 12345681   # 与 v2(12345678)、v2.1(12345679)、v2.2(12345680) 不同

# ── 服务器配置 ────────────────────────────────────────────────────────────────
# 跑 PUSH_MODE="server" 时必须填这俩；建议 .env 里 export 后在此读取。
# 例：export QMT_PIPELINE_BASE_URL=http://120.26.138.82:8000
SERVER_BASE_URL = os.environ.get("QMT_PIPELINE_BASE_URL", "")
API_KEY         = os.environ.get("QMT_PIPELINE_API_KEY", "")

# ── 推送模式 ──────────────────────────────────────────────────────────────────
# "server"   : 真实服务器（生产 / 实盘联调）— 默认
# "local"    : POST 写本地文件，GET 读 mock；QMT 走真实连接（v2.2 离线 dev 用）
# "mock_qmt" : local 基础上，所有 QMT API 调用改为读 mock_data/ JSON
PUSH_MODE = os.environ.get("QMT_PIPELINE_PUSH_MODE", "server")

# ── 测试用交易日强制覆盖（非交易日联调时使用）────────────────────────────
# 设置为有效日期字符串（如 "20260430"）时，各模块会使用此日期代替 datetime.now()
# 并跳过 is_trading_day 检查。**生产环境必须设为 None**。
FORCE_TRADE_DATE: str | None = None

# mock_qmt 模式专用：QMT 调用全部 mock 时使用的固定交易日
# 与 FORCE_TRADE_DATE 解耦：FORCE_TRADE_DATE 影响所有模式，MOCK_TRADE_DATE 仅 mock_qmt
MOCK_TRADE_DATE = "20260430"

# GET /orders 在 local / mock_qmt 模式下读取此文件（格式与真实响应相同）
LOCAL_ORDERS_MOCK_FILE = os.path.join(MOCK_DATA_DIR, "orders_mock.json")

# ── 行情板块名（仅 server/local 模式用到）────────────────────────────────────
STOCK_SECTOR  = "沪深A股"
ETF_SECTORS   = ["沪深ETF"]                         # 全市场 ETF，约 1530 只
# QMT 中无纯"市场指数"板块，"上证指数/深证指数"实际返回的是交易所分类股票。
# 因此核心指数改用硬编码列表，直接下载。
INDEX_CODES   = [
    # 上交所指数
    "000001.SH",  # 上证指数
    "000002.SH",  # 上证A指
    "000003.SH",  # 上证B指
    "000016.SH",  # 上证50
    "000300.SH",  # 沪深300
    "000688.SH",  # 科创50
    "000852.SH",  # 中证1000
    "000905.SH",  # 中证500
    "000906.SH",  # 中证800
    # 深交所指数
    "399001.SZ",  # 深证成指
    "399005.SZ",  # 中小100
    "399006.SZ",  # 创业板指
    "399016.SZ",  # 深证创新
    "399102.SZ",  # 创业板综
    "399330.SZ",  # 深证100
    "399905.SZ",  # 中证500（深）
    "399852.SZ",  # 中证1000（深）
]

# ── 重试 ──────────────────────────────────────────────────────────────────────
PUSH_MAX_RETRIES             = 3
PUSH_RETRY_INTERVAL_SECONDS  = 600
ORDER_RETRY_INTERVAL_SECONDS = 1800


# ── 日志工厂 ──────────────────────────────────────────────────────────────────
def setup_logger(name: str) -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger(f"v23.{name}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, f"{name}.log"),
        when="midnight", backupCount=30, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ── 本地推送工具 ───────────────────────────────────────────────────────────────
def save_local_push(filename: str, payload: dict) -> None:
    os.makedirs(LOCAL_PUSH_DIR, exist_ok=True)
    path = os.path.join(LOCAL_PUSH_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_local_orders_mock() -> dict:
    if not os.path.exists(LOCAL_ORDERS_MOCK_FILE):
        return {"code": 0, "message": "ok (mock: file not found)", "data": {"date": "", "orders": []}}
    with open(LOCAL_ORDERS_MOCK_FILE, encoding="utf-8") as f:
        return json.load(f)


# ── mock_data 加载工具（保留，方便调试时切换 mock_qmt 回退）──────────────────
def load_mock_market_payload() -> dict:
    path = os.path.join(MOCK_DATA_DIR, "market_payload_mock.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_mock_qmt_state() -> dict:
    path = os.path.join(MOCK_DATA_DIR, "qmt_state_mock.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── strategies.yaml 加载 ──────────────────────────────────────────────────────
def load_strategies() -> list[dict]:
    with open(STRATEGIES_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)["account_groups"]


def get_qmt_account_id(account_group: str) -> str | None:
    for ag in load_strategies():
        if ag["group_id"] == account_group:
            return ag.get("qmt_account_id") or None
    return None
