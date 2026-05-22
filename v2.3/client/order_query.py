"""
模块三：订单拉取与制单（v2.1）
mock_qmt 模式：跳过 xtdata.get_trading_calendar()，直接用 config.MOCK_TRADE_DATE
作为目标交易日，读取 mock_data/orders_mock.json 代替真实 GET /orders。
"""

import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from xtquant import xtdata

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

xtdata.data_dir = config.QMT_USERDATA_DIR
log = config.setup_logger("order_query")


def _wechat_alert(msg: str) -> None:
    log.error(f"[微信报警] {msg}")
    if not config.notify_wecom(msg, level="alert"):
        log.error(f"[微信报警] 推送失败：{msg}")

def _wechat_notify(msg: str) -> None:
    log.info(f"[微信通知] {msg}")
    if not config.notify_wecom(msg, level="info"):
        log.error(f"[微信通知] 推送失败：{msg}")


def startup_check() -> None:
    assert os.path.exists(config.QMT_USERDATA_DIR), \
        f"QMT 数据目录不存在: {config.QMT_USERDATA_DIR}"
    if config.PUSH_MODE == "server":
        assert config.SERVER_BASE_URL, "SERVER_BASE_URL 未配置"
        assert config.API_KEY,         "API_KEY 未配置"
    log.info(f"PUSH_MODE={config.PUSH_MODE}，启动检查通过")


def _init_server_orders_table() -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS server_orders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id      TEXT UNIQUE,
                account_group TEXT,
                symbol        TEXT,
                direction     TEXT,
                quantity      INTEGER,
                limit_price   REAL,
                valid_date    TEXT,
                received_at   TEXT
            )
        """)
        conn.commit()


def _get_next_trading_day(today_str: str) -> str | None:
    # v2.2：start_time 回退 30 天，确保落在已有缓存数据的日期范围内。
    # 若 start_time 落在无数据的日期（如未来假期），get_trading_calendar 返回空。
    start_str = (
        datetime.strptime(today_str, "%Y%m%d") - timedelta(days=30)
    ).strftime("%Y%m%d")
    end_str = (
        datetime.strptime(today_str, "%Y%m%d") + timedelta(days=14)
    ).strftime("%Y%m%d")
    days   = xtdata.get_trading_calendar("SH", start_time=start_str, end_time=end_str)
    future = [d for d in days if d > today_str]
    return future[0] if future else None


def _fetch_from_server(next_date: str) -> dict | None:
    url     = config.SERVER_BASE_URL.rstrip("/") + "/orders"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    try:
        resp = requests.get(url, params={"date": next_date}, headers=headers, timeout=30)
        return resp.json()
    except Exception as e:
        log.error(f"GET /orders 请求异常：{e}")
        return None


def _fetch_from_local() -> dict:
    data = config.load_local_orders_mock()
    if data is None:
        return {"code": 0, "message": "ok", "data": {"orders": []}}
    return data


def _write_server_orders(orders: list[dict], next_date: str, received_at: str) -> int:
    written      = 0
    skipped_date = 0
    with sqlite3.connect(config.DB_PATH) as conn:
        for order in orders:
            if order.get("valid_date") != next_date:
                log.warning(
                    f"order_id={order.get('order_id')} valid_date={order.get('valid_date')} "
                    f"与预期 {next_date} 不符，跳过"
                )
                skipped_date += 1
                continue
            cur = conn.execute("""
                INSERT OR IGNORE INTO server_orders (
                    order_id, account_group, symbol, direction,
                    quantity, limit_price, valid_date, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order["order_id"], order.get("account_group"),
                order.get("symbol"), order.get("direction"),
                order.get("quantity"), order.get("limit_price"),
                order["valid_date"], received_at,
            ))
            if cur.rowcount > 0:
                written += 1
        conn.commit()
    if skipped_date:
        log.warning(f"valid_date 不匹配，跳过 {skipped_date} 条")
    return written


def main():
    log.info("=== order_query 启动（v2.1）===")
    startup_check()
    _init_server_orders_table()

    today = datetime.now().strftime("%Y%m%d")

    if config.PUSH_MODE == "mock_qmt":
        next_date = config.MOCK_TRADE_DATE
        log.info(f"mock_qmt 模式：next_date 固定为 MOCK_TRADE_DATE={next_date}")
    elif getattr(config, "FORCE_TRADE_DATE", None):
        next_date = config.FORCE_TRADE_DATE
        log.info(f"FORCE_TRADE_DATE={next_date}，强制作为目标交易日")
    else:
        next_date = _get_next_trading_day(today)
        if next_date is None:
            _wechat_alert("order_query：14 天内未找到下一个交易日")
            return
        log.info(f"今日 {today}，查询次日订单 date={next_date}")

    body = None
    for attempt in range(1, 3):
        if config.PUSH_MODE in ("local", "mock_qmt"):
            body = _fetch_from_local()
        else:
            body = _fetch_from_server(next_date)

        if body is None:
            _wechat_alert(f"order_query：GET /orders 网络请求失败（date={next_date}）")
            return

        code = body.get("code")
        if code == 3002:
            if attempt == 1:
                log.warning(f"服务器返回 3002，{config.ORDER_RETRY_INTERVAL_SECONDS // 60} 分钟后重试...")
                time.sleep(config.ORDER_RETRY_INTERVAL_SECONDS)
                continue
            _wechat_alert(f"order_query：重试后仍返回 3002（date={next_date}）")
            return
        if code == 3001:
            _wechat_alert(f"order_query：服务器返回 3001（非交易日，date={next_date}）")
            return
        if code != 0:
            _wechat_alert(f"order_query：异常 code={code} message={body.get('message')}")
            return
        break

    data   = body.get("data", {})
    orders = data.get("orders") or []
    log.info(f"服务器返回 {len(orders)} 条订单")

    if not orders:
        log.info(f"次日 {next_date} 无订单，正常退出")
        _wechat_notify(f"订单拉取 {next_date}：今日无交易机会")
        return

    received_at = datetime.now().isoformat()
    written     = _write_server_orders(orders, next_date, received_at)
    log.info(f"写入 server_orders：新增 {written} 条")

    # 按账户/方向汇总
    by_acct = defaultdict(lambda: {"BUY": 0, "SELL": 0})
    for o in orders:
        d = o.get("direction", "")
        if d in ("BUY", "SELL"):
            by_acct[o.get("account_group", "?")][d] += 1
    parts = []
    for acct in sorted(by_acct.keys()):
        b = by_acct[acct]
        parts.append(f"{acct} 买入{b['BUY']}笔，卖出{b['SELL']}笔")
    _wechat_notify(
        f"order_query {next_date}：共 {len(orders)} 条委托，其中 {'；'.join(parts)}"
    )
    log.info("=== order_query 完成 ===")


if __name__ == "__main__":
    main()
