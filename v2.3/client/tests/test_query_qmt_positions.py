"""_compute_strategy_cash 的 account_group 过滤测试。

query_qmt_positions.py 顶层 import requests + xtquant（Windows/客户端依赖，
Mac 测试环境没有），所以导入前先把这些 + config stub 进 sys.modules。
被测函数本身只用 sqlite3 + config.DB_PATH，stub 后可在任意环境跑。
"""
from __future__ import annotations

import importlib
import logging
import sqlite3
import sys
import types
from pathlib import Path

import pytest

_CLIENT_DIR = Path(__file__).resolve().parent.parent


def _make_local_orders_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE local_orders (
            local_order_id     TEXT,
            order_id           TEXT PRIMARY KEY,
            account_group      TEXT,
            qmt_account_id     TEXT,
            symbol             TEXT,
            direction          TEXT,
            submitted_price    REAL,
            submitted_quantity INTEGER,
            submitted_at       TEXT,
            submit_status      TEXT,
            fail_reason        TEXT
        )
    """)
    rows = [
        # (order_id, account_group, symbol, direction, price, qty, submitted_at, status)
        ("o1", "paper_v20h", "600519.SH", "BUY",  10.0,  1000, "2026-05-20T09:15:00", "SUCCESS"),
        ("o2", "paper_v20h", "600519.SH", "SELL", 10.0,   500, "2026-05-21T09:15:00", "SUCCESS"),
        # V53 的大额建仓单（共用 local_orders 表）— V20H 对账应排除
        ("o3", "paper_v53",  "511260.SH", "BUY", 100.0, 100000, "2026-06-01T09:15:00", "SUCCESS"),
        # 失败单不计
        ("o4", "paper_v20h", "000001.SZ", "BUY",  10.0,  9999, "2026-05-22T09:15:00", "FAILED"),
    ]
    for oid, ag, sym, d, px, qty, at, st in rows:
        conn.execute(
            "INSERT INTO local_orders (local_order_id, order_id, account_group, "
            "qmt_account_id, symbol, direction, submitted_price, submitted_quantity, "
            "submitted_at, submit_status, fail_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (oid, oid, ag, "301300148788", sym, d, px, qty, at, st, None),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def qqp(tmp_path, monkeypatch):
    """stub requests/xtquant/config 后导入 query_qmt_positions，返回 (module, db_path)。"""
    db_path = tmp_path / "pipeline.db"
    _make_local_orders_db(db_path)

    monkeypatch.setitem(sys.modules, "requests", types.ModuleType("requests"))
    xq = types.ModuleType("xtquant")
    xtt = types.ModuleType("xtquant.xttrader")
    xtt.XtQuantTrader = object
    xtt.XtQuantTraderCallback = object
    xtty = types.ModuleType("xtquant.xttype")
    xtty.StockAccount = object
    xtc = types.ModuleType("xtquant.xtconstant")
    monkeypatch.setitem(sys.modules, "xtquant", xq)
    monkeypatch.setitem(sys.modules, "xtquant.xttrader", xtt)
    monkeypatch.setitem(sys.modules, "xtquant.xttype", xtty)
    monkeypatch.setitem(sys.modules, "xtquant.xtconstant", xtc)

    cfg = types.ModuleType("config")
    cfg.DB_PATH = str(db_path)
    cfg.setup_logger = lambda name: logging.getLogger(name)
    monkeypatch.setitem(sys.modules, "config", cfg)

    monkeypatch.syspath_prepend(str(_CLIENT_DIR))
    sys.modules.pop("query_qmt_positions", None)
    mod = importlib.import_module("query_qmt_positions")
    return mod, db_path


def test_compute_strategy_cash_filters_by_account_group(qqp):
    """共用 local_orders 表时，只算本 account_group 的成交：
    V20H 起始 100万，BUY 1000@10=-1万，SELL 500@10=+5千 → 99.5万。
    V53 的 100000@100=1000万 BUY 必须被排除（否则现金变成大负数）。"""
    mod, _ = qqp
    cash = mod._compute_strategy_cash(start_capital=1_000_000, account_group="paper_v20h")
    assert cash == pytest.approx(995_000.0)


def test_compute_strategy_cash_v53_isolated(qqp):
    """换 account_group=paper_v53：只算 V53 的 1000万 BUY → 起始-1000万。"""
    mod, _ = qqp
    cash = mod._compute_strategy_cash(start_capital=10_000_000, account_group="paper_v53")
    assert cash == pytest.approx(0.0)
