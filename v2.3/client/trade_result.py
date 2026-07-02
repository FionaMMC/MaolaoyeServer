"""
模块二：成交查询与推送（v2.2）
连 QMT 拉当日成交 → 聚合 → 推 server。不写本地表。
"""

import os
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace

import requests
from xtquant import xtconstant, xtdata
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

xtdata.data_dir = config.QMT_USERDATA_DIR
log = config.setup_logger("trade_result")

_REJECTED_STATUSES = {xtconstant.ORDER_JUNK}

_QMT_RETRY_COUNT = 1          # QMT 瞬断后重试次数
_QMT_RETRY_INTERVAL = 60      # 重试间隔（秒）


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


def _load_today_local_orders(trade_date: str) -> list[dict]:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT
                lo.local_order_id,
                lo.order_id,
                lo.qmt_account_id,
                lo.symbol,
                lo.direction,
                lo.submitted_quantity,
                lo.submitted_price
            FROM local_orders lo
            JOIN server_orders so ON lo.order_id = so.order_id
            WHERE so.valid_date = ? AND lo.submit_status = 'SUCCESS'
        """, (trade_date,))
        return [dict(row) for row in cur.fetchall()]


def _to_iso(traded_time: int) -> str:
    return datetime.fromtimestamp(traded_time).isoformat()


def _auto_backfill_from_qmt(trade_date: str, qmt_orders: list) -> int:
    """以 QMT 实际委托为准，将缺失的委托补录到 local_orders。
    只处理 strategy_name='pipeline_v21' 的竞价订单，
    通过 order_remark（截断的 UUID）前缀匹配 server_orders。
    返回补录条数。"""
    pipeline = [o for o in qmt_orders
                if getattr(o, 'strategy_name', None) == 'pipeline_v21']
    if not pipeline:
        return 0

    remark_map = {}
    for o in pipeline:
        remark = getattr(o, 'order_remark', None) or getattr(o, 'm_strOrderRemark', None)
        if remark and len(remark) >= 20:
            qmt_oid = getattr(o, 'order_id', None) or getattr(o, 'm_nOrderID', None)
            ot = getattr(o, 'order_time', None) or getattr(o, 'm_nOrderTime', None)
            remark_map[remark] = (qmt_oid, ot)

    if not remark_map:
        return 0

    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT * FROM server_orders
            WHERE valid_date = ? AND order_id NOT IN (
                SELECT order_id FROM local_orders
            )
        """, (trade_date,))
        missing = [dict(r) for r in cur.fetchall()]

    if not missing:
        return 0

    records = []
    submitted_at = datetime.now().isoformat()
    qmt_account_id = config.get_qmt_account_id("paper_v20h") or "301300148788"

    for m in missing:
        oid_prefix = m["order_id"][:20]
        for remark, (qmt_oid, order_time) in remark_map.items():
            if remark.startswith(oid_prefix):
                iso_time = submitted_at
                if order_time and order_time > 1000000000:
                    iso_time = datetime.fromtimestamp(order_time).isoformat()
                records.append({
                    "local_order_id": str(qmt_oid),
                    "order_id": m["order_id"],
                    "account_group": m["account_group"],
                    "qmt_account_id": qmt_account_id,
                    "symbol": m["symbol"],
                    "direction": m["direction"],
                    "submitted_price": m["limit_price"],
                    "submitted_quantity": m["quantity"],
                    "submitted_at": iso_time,
                    "submit_status": "SUCCESS",
                    "fail_reason": None,
                })
                break

    if records:
        with sqlite3.connect(config.DB_PATH) as conn:
            for r in records:
                try:
                    conn.execute("""
                        INSERT INTO local_orders (
                            local_order_id, order_id, account_group, qmt_account_id,
                            symbol, direction, submitted_price, submitted_quantity,
                            submitted_at, submit_status, fail_reason
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """, (r["local_order_id"], r["order_id"], r["account_group"],
                         r["qmt_account_id"], r["symbol"], r["direction"],
                         r["submitted_price"], r["submitted_quantity"],
                         r["submitted_at"], r["submit_status"], r["fail_reason"]))
                except sqlite3.IntegrityError:
                    pass
            conn.commit()
        log.warning(f"自动补录 {len(records)} 笔缺失委托（QMT 有但本地 DB 无）")

    return len(records)


def _connect_qmt_and_query(qmt_account_ids: list[str]) -> tuple[list, dict, list]:
    xt_trader = XtQuantTrader(config.QMT_USERDATA_DIR, config.QMT_SESSION_ID)
    xt_trader.register_callback(XtQuantTraderCallback())
    xt_trader.start()
    connect_result = xt_trader.connect()
    if connect_result != 0:
        xt_trader.stop()
        raise RuntimeError(f"QMT 连接失败（返回值={connect_result}）")
    accounts = {}
    for qmt_id in qmt_account_ids:
        acc        = StockAccount(qmt_id)
        sub_result = xt_trader.subscribe(acc)
        if sub_result != 0:
            xt_trader.stop()
            raise RuntimeError(f"账号 {qmt_id} 订阅失败（返回值={sub_result}）")
        accounts[qmt_id] = acc
        log.info(f"已订阅账户：{qmt_id}")
    all_trades, all_orders = [], []
    for qmt_id, acc in accounts.items():
        trades = xt_trader.query_stock_trades(acc) or []
        orders = xt_trader.query_stock_orders(acc) or []
        log.info(f"账户 {qmt_id}：成交 {len(trades)} 笔，委托 {len(orders)} 笔")
        all_trades.extend(trades)
        all_orders.extend(orders)
    xt_trader.stop()
    if all_trades:
        log.info(f"[诊断] traded_time 样例原始值：{all_trades[0].traded_time}")
    orders_map = {o.order_id: o for o in all_orders}
    return all_trades, orders_map, all_orders


def _mock_qmt_query(local_orders: list[dict]) -> tuple[list, dict]:
    """
    mock_qmt 模式：将所有 SUCCESS 委托模拟为 FILLED。
    成交量 = 委托量，成交价 = 委托价，时间戳 = 当前时间。
    返回值格式与 _connect_qmt_and_query 完全相同，可直接传给 _aggregate_trades。
    """
    now_ts = int(datetime.now().timestamp())
    mock_trades = []
    for lo in local_orders:
        try:
            qmt_oid = int(lo["local_order_id"])
        except (ValueError, TypeError):
            log.warning(f"local_order_id={lo['local_order_id']!r} 非整数，跳过（mock）")
            continue
        t = SimpleNamespace()
        t.order_id      = qmt_oid
        t.traded_volume = lo["submitted_quantity"]
        t.traded_price  = lo["submitted_price"]
        t.traded_time   = now_ts
        mock_trades.append(t)
    log.info(f"mock_qmt：生成 {len(mock_trades)} 笔模拟成交（全部 FILLED，价格 = 委托价）")
    return mock_trades, {}   # 空 orders_map → 无成交时归为 CANCELLED（不用 REJECTED）


def _aggregate_trades(xt_trades: list) -> dict:
    agg: dict[int, dict] = {}
    for t in xt_trades:
        oid = t.order_id
        if oid not in agg:
            agg[oid] = {"qty": 0, "amount": 0.0, "latest_time": None}
        agg[oid]["qty"]    += t.traded_volume
        agg[oid]["amount"] += t.traded_volume * t.traded_price
        if agg[oid]["latest_time"] is None or t.traded_time > agg[oid]["latest_time"]:
            agg[oid]["latest_time"] = t.traded_time
    result = {}
    for oid, data in agg.items():
        qty = data["qty"]
        result[oid] = {
            "filled_quantity": qty,
            "filled_price":    round(data["amount"] / qty, 4) if qty > 0 else 0.0,
            "filled_time":     _to_iso(data["latest_time"]) if data["latest_time"] else None,
        }
    return result


def _determine_status(
    local_order_id: str,
    agg_trades: dict,
    orders_map: dict,
    submitted_quantity: int,
) -> tuple[int, float, str | None, str]:
    try:
        qmt_oid = int(local_order_id)
    except (ValueError, TypeError):
        log.warning(f"local_order_id={local_order_id!r} 无法转为 int，归为 CANCELLED")
        return 0, 0.0, None, "CANCELLED"
    trade = agg_trades.get(qmt_oid)
    if trade and trade["filled_quantity"] > 0:
        qty    = trade["filled_quantity"]
        status = "FILLED" if qty >= submitted_quantity else "PARTIAL"
        return qty, trade["filled_price"], trade["filled_time"], status
    xt_order = orders_map.get(qmt_oid)
    if xt_order is not None and xt_order.order_status in _REJECTED_STATUSES:
        return 0, 0.0, None, "REJECTED"
    return 0, 0.0, None, "CANCELLED"


def _build_push_results(records: list[dict]) -> list[dict]:
    results = []
    for r in records:
        entry = {
            "order_id":        r["order_id"],
            "filled_quantity": r["filled_quantity"],
            "filled_price":    r["filled_price"],
            "status":          r["status"],
            # 可选字段（v2.3 服务端向后兼容）：order_id 未匹配时服务器
            # 据此给出候选订单，恢复不用再靠猜
            "symbol":          r.get("symbol"),
            "direction":       r.get("direction"),
        }
        if r["filled_time"]:
            entry["filled_time"] = r["filled_time"]
        results.append(entry)
    return results


def _push_to_server(trade_date: str, results: list[dict]) -> bool:
    payload = {"trade_date": trade_date, "results": results}
    url     = config.SERVER_BASE_URL.rstrip("/") + "/trade-result"
    headers = {"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        body = resp.json()
        code = body.get("code")
        if code == 0:
            d         = body.get("data", {})
            unmatched = d.get("unmatched_order_ids") or []
            log.info(f"推送成功：matched_count={d.get('matched_count')}")
            if unmatched:
                _wechat_alert(f"成交回报：{len(unmatched)} 条 order_id 未匹配 {unmatched}")
            return True
        log.error(f"推送失败：code={code} message={body.get('message')}")
        return False
    except Exception as e:
        log.error(f"推送异常：{e}")
        return False


def _push(trade_date: str, records: list[dict]) -> bool:
    results = _build_push_results(records)
    payload = {"trade_date": trade_date, "results": results}
    if config.PUSH_MODE in ("local", "mock_qmt"):
        filename = f"trade_result_{trade_date}.json"
        config.save_local_push(filename, payload)
        log.info(f"[{config.PUSH_MODE}] payload 已写入 local_push/{filename}")
        return True
    return _push_to_server(trade_date, results)


def _init_audit_log() -> None:
    """建审计表。trade_date + order_id 联合主键，每天每订单独立一条。"""
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                trade_date       TEXT,
                order_id         TEXT,
                symbol           TEXT,
                direction        TEXT,
                submitted_qty    INTEGER,
                filled_qty       INTEGER,
                filled_price     REAL,
                status           TEXT,
                logged_at        TEXT,
                PRIMARY KEY (trade_date, order_id)
            )
        """)
        conn.commit()


def _write_audit_log(records: list[dict], trade_date: str) -> None:
    """写入审计表。已存在的 (trade_date, order_id) 会被 REPLACE 覆盖。"""
    logged_at = datetime.now().isoformat()
    with sqlite3.connect(config.DB_PATH) as conn:
        for r in records:
            conn.execute("""
                INSERT OR REPLACE INTO audit_log (
                    trade_date, order_id, symbol, direction,
                    submitted_qty, filled_qty, filled_price, status, logged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_date, r["order_id"], r.get("symbol", ""), r.get("direction", ""),
                r.get("submitted_quantity", 0), r["filled_quantity"],
                r["filled_price"], r["status"], logged_at,
            ))
        conn.commit()


def main():
    log.info("=== trade_result 启动（v2.2）===")
    startup_check()
    _init_audit_log()

    trade_date = getattr(config, "FORCE_TRADE_DATE", None) or (
        config.MOCK_TRADE_DATE if config.PUSH_MODE == "mock_qmt"
        else datetime.now().strftime("%Y%m%d")
    )
    log.info(f"查询交易日：{trade_date}")

    local_orders = _load_today_local_orders(trade_date)
    if not local_orders:
        log.info(f"今日 {trade_date} 无 SUCCESS 委托记录，退出")
        return
    log.info(f"今日委托 {len(local_orders)} 笔")

    if config.PUSH_MODE == "mock_qmt":
        xt_trades, orders_map = _mock_qmt_query(local_orders)
        all_orders_raw = []
    else:
        qmt_account_ids = list({lo["qmt_account_id"] for lo in local_orders})
        log.info(f"涉及账户：{qmt_account_ids}")

        xt_trades = []; orders_map = {}; all_orders_raw = []
        for attempt in range(1 + _QMT_RETRY_COUNT):
            try:
                xt_trades, orders_map, all_orders_raw = _connect_qmt_and_query(qmt_account_ids)
                break
            except RuntimeError as e:
                if attempt < _QMT_RETRY_COUNT:
                    log.warning(f"QMT 连接失败（第{attempt+1}次），{_QMT_RETRY_INTERVAL}秒后重试...")
                    time.sleep(_QMT_RETRY_INTERVAL)
                else:
                    _wechat_alert(str(e))
                    return

        log.info(f"QMT 返回：成交 {len(xt_trades)} 笔，委托 {len(orders_map)} 笔")
        backfilled = _auto_backfill_from_qmt(trade_date, all_orders_raw)
        if backfilled > 0:
            _wechat_notify(f"成交回报：自动补录 {backfilled} 笔缺失委托")
            local_orders = _load_today_local_orders(trade_date)
            log.info(f"补录后委托 {len(local_orders)} 笔")

    agg_trades = _aggregate_trades(xt_trades)
    log.info(f"聚合后：{len(agg_trades)} 个委托有成交")

    records = []
    for lo in local_orders:
        qty, price, filled_time, status = _determine_status(
            lo["local_order_id"], agg_trades, orders_map, lo["submitted_quantity"]
        )
        records.append({
            "order_id":          lo["order_id"],
            "symbol":            lo["symbol"],
            "direction":         lo["direction"],
            "submitted_quantity": lo["submitted_quantity"],
            "filled_quantity":    qty,
            "filled_price":      price,
            "filled_time":       filled_time,
            "status":            status,
        })
        log.info(f"  {lo['symbol']} {lo['direction']} → {status} qty={qty} price={price}")

    _write_audit_log(records, trade_date)

    filled    = sum(1 for r in records if r["status"] == "FILLED")
    partial   = sum(1 for r in records if r["status"] == "PARTIAL")
    cancelled = sum(1 for r in records if r["status"] == "CANCELLED")
    rejected  = sum(1 for r in records if r["status"] == "REJECTED")
    summary   = f"全成={filled} 部成={partial} 已撤={cancelled} 废单={rejected}"
    log.info(f"成交汇总：{summary}")

    ok = _push(trade_date, records)
    if not ok:
        _wechat_alert(f"成交回报推送失败（trade_date={trade_date}）")
    else:
        _wechat_notify(f"收盘成交汇总 {trade_date}：{summary}")
    log.info("=== trade_result 完成 ===")


if __name__ == "__main__":
    main()
