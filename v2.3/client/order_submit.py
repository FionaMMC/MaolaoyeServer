"""
模块四：竞价下单（v2.1）
mock_qmt 模式：跳过 QMT 连接，从 mock_data/qmt_state_mock.json 读取账户资金/持仓，
风控逻辑不变，成功提交的订单生成顺序编号（10001, 10002, ...）作为 local_order_id。
"""

import os
import sqlite3
from collections import defaultdict
from datetime import datetime

from xtquant import xtconstant, xtdata
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

xtdata.data_dir = config.QMT_USERDATA_DIR
log = config.setup_logger("order_submit")

_mock_order_seq = 10000   # mock_qmt 模式下的委托编号起始值


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
    log.info(f"PUSH_MODE={config.PUSH_MODE}，启动检查通过")


def _init_local_orders_table() -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS local_orders (
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
        conn.commit()


def _load_today_orders(trade_date: str) -> list[dict]:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM server_orders WHERE valid_date = ?", (trade_date,)
        )
        return [dict(row) for row in cur.fetchall()]


def _get_already_submitted_order_ids(trade_date: str) -> set[str]:
    """查询当天已成功提交过的 order_id（防止重复下单）。"""
    try:
        trade_date_str = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
        with sqlite3.connect(config.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT DISTINCT order_id FROM local_orders "
                "WHERE DATE(submitted_at) = ? AND submit_status = 'SUCCESS'",
                (trade_date_str,)
            ).fetchall()
            return {r[0] for r in rows}
    except Exception:
        return set()


def _sleep_until(hhmm: str) -> None:
    """等待至今日 HH:MM；mock_qmt 模式下跳过。"""
    if config.PUSH_MODE == "mock_qmt":
        log.info(f"mock_qmt 模式：跳过等待至 {hhmm[:2]}:{hhmm[2:]}")
        return
    from time import sleep as _sleep
    now    = datetime.now()
    target = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)
    wait   = (target - now).total_seconds()
    if wait > 0:
        log.info(f"等待至 {hhmm[:2]}:{hhmm[2:]}（{wait:.0f} 秒）...")
        _sleep(wait)
    else:
        log.info(f"已过 {hhmm[:2]}:{hhmm[2:]}，直接执行")


# ── mock_qmt 专用 ─────────────────────────────────────────────────────────────

def _build_mock_trader(mock_state: dict, account_group: str):
    """从 qmt_state_mock.json 的 account_group 条目构建 mock 交易对象，接口与 XtQuantTrader 相同。"""
    state = mock_state.get(account_group, {"cash": 0.0, "positions": {}})

    class _MockAsset:
        def __init__(self): self.cash = state["cash"]

    class _MockPosition:
        def __init__(self, s, v): self.stock_code = s; self.can_use_volume = v

    class _MockTrader:
        def query_stock_asset(self, acc):
            return _MockAsset()
        def query_stock_positions(self, acc):
            return [_MockPosition(s, v) for s, v in state["positions"].items()]

    return _MockTrader()


def _mock_submit_single(order: dict, submitted_at: str) -> dict:
    global _mock_order_seq
    _mock_order_seq += 1
    local_oid = str(_mock_order_seq)
    log.info(
        f"  [mock] {order['symbol']} {order['direction']} "
        f"qty={order['quantity']} price={order['limit_price']} → local_order_id={local_oid}"
    )
    return {
        "local_order_id":     local_oid,
        "order_id":           order["order_id"],
        "account_group":      order["account_group"],
        "qmt_account_id":     f"MOCK_{order['account_group']}",
        "symbol":             order["symbol"],
        "direction":          order["direction"],
        "submitted_price":    order["limit_price"],
        "submitted_quantity": order["quantity"],
        "submitted_at":       submitted_at,
        "submit_status":      "SUCCESS",
        "fail_reason":        None,
    }


# ── 真实 QMT 专用 ─────────────────────────────────────────────────────────────

def _connect_qmt(qmt_account_ids: list[str]) -> tuple[XtQuantTrader, dict]:
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
    return xt_trader, accounts


def _submit_single(xt_trader, acc, qmt_account_id: str, order: dict, submitted_at: str) -> dict:
    direction_const = (
        xtconstant.STOCK_BUY if order["direction"] == "BUY" else xtconstant.STOCK_SELL
    )
    local_order_id = xt_trader.order_stock(
        acc, order["symbol"], direction_const, order["quantity"],
        xtconstant.FIX_PRICE, order["limit_price"], "pipeline_v21", order["order_id"],
    )
    base = {
        "order_id":           order["order_id"],
        "account_group":      order["account_group"],
        "qmt_account_id":     qmt_account_id,
        "symbol":             order["symbol"],
        "direction":          order["direction"],
        "submitted_price":    order["limit_price"],
        "submitted_quantity": order["quantity"],
        "submitted_at":       submitted_at,
    }
    if local_order_id >= 0:
        return {**base, "local_order_id": str(local_order_id),
                "submit_status": "SUCCESS", "fail_reason": None}
    reason = f"QMT order_stock 返回 {local_order_id}（失败）"
    log.error(f"{order['symbol']} {order['direction']} {reason}")
    return {**base, "local_order_id": f"FAIL_{order['order_id']}",
            "submit_status": "FAILED", "fail_reason": reason}


# ── 共用 ──────────────────────────────────────────────────────────────────────

def _risk_check(trader, acc, qmt_account_id: str, orders: list[dict]) -> tuple[list, list]:
    """风控逻辑（mock/真实模式通用，trader 接口相同）。"""
    asset = trader.query_stock_asset(acc)
    if asset is None:
        reason = f"查询账户资产失败（qmt_account_id={qmt_account_id}）"
        log.error(reason)
        return [], [{**o, "fail_reason": reason} for o in orders]

    positions     = trader.query_stock_positions(acc) or []
    pos_map       = {p.stock_code: p.can_use_volume for p in positions}
    remaining_cash = asset.cash
    remaining_pos  = dict(pos_map)
    log.info(f"账户 {qmt_account_id}：可用资金 {remaining_cash:.2f} 元，持仓 {len(pos_map)} 只")

    pass_orders: list[dict] = []
    fail_orders: list[dict] = []
    # 关键：依赖调用方已经把 SELL 排在 BUY 前面。
    # SELL 通过时 += 预期回笼现金（乐观假设全成交），让后面的 BUY 能用上。
    # 集合竞价场景下 SELL/BUY 同步匹配，这个乐观估计接近真实。
    for order in orders:
        sym   = order["symbol"]
        qty   = order["quantity"]
        price = order["limit_price"]
        if order["direction"] == "BUY":
            required = qty * price
            if remaining_cash >= required:
                remaining_cash -= required
                pass_orders.append(order)
            else:
                reason = (
                    f"资金不足：{sym} BUY {qty}@{price} 需 {required:.2f} 元，"
                    f"账户剩余可用 {remaining_cash:.2f} 元（含预期 SELL 回笼）"
                )
                log.warning(reason)
                fail_orders.append({**order, "fail_reason": reason})
        elif order["direction"] == "SELL":
            available = remaining_pos.get(sym, 0)
            if available >= qty:
                remaining_pos[sym] = available - qty
                remaining_cash += qty * price  # 预期 SELL 回笼现金给后面 BUY 用
                pass_orders.append(order)
            elif available > 0:
                # 持仓不足但仍有部分可用 → 按可用数量降量卖出
                original_qty = qty
                order = {**order, "quantity": available}
                remaining_pos[sym] = 0
                remaining_cash += available * price
                log.warning(f"{sym} SELL 原委托 {original_qty} 股 → 降为可用 {available} 股")
                pass_orders.append(order)
            else:
                reason = f"持仓不足：{sym} SELL {qty} 股，账户可用 0 股"
                log.warning(reason)
                fail_orders.append({**order, "fail_reason": reason})

    log.info(f"账户 {qmt_account_id} 风控结果：通过 {len(pass_orders)} 笔，拦截 {len(fail_orders)} 笔")
    return pass_orders, fail_orders


def _write_local_orders(records: list[dict]) -> None:
    with sqlite3.connect(config.DB_PATH) as conn:
        for r in records:
            conn.execute("""
                INSERT INTO local_orders (
                    local_order_id, order_id, account_group, qmt_account_id,
                    symbol, direction, submitted_price, submitted_quantity,
                    submitted_at, submit_status, fail_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r["local_order_id"], r["order_id"],
                r["account_group"],  r["qmt_account_id"],
                r["symbol"],         r["direction"],
                r["submitted_price"], r["submitted_quantity"],
                r["submitted_at"],   r["submit_status"],
                r["fail_reason"],
            ))
        conn.commit()


def main():
    log.info("=== order_submit 启动（v2.1）===")
    startup_check()
    _init_local_orders_table()

    trade_date = getattr(config, "FORCE_TRADE_DATE", None) or (
        config.MOCK_TRADE_DATE if config.PUSH_MODE == "mock_qmt"
        else datetime.now().strftime("%Y%m%d")
    )
    orders = _load_today_orders(trade_date)
    if not orders:
        log.info(f"{trade_date} 无 server_orders 记录，退出")
        return
    log.info(f"{trade_date} 从 server_orders 加载 {len(orders)} 条订单")

    # 去重：跳过已在 local_orders 中成功提交过的 order_id
    already_submitted = _get_already_submitted_order_ids(trade_date)
    if already_submitted:
        orders_before = len(orders)
        orders = [o for o in orders if o["order_id"] not in already_submitted]
        skipped = orders_before - len(orders)
        if skipped > 0:
            log.warning(f"去重：跳过 {skipped} 条已成功提交的订单，剩余 {len(orders)} 条待提交")
            _wechat_notify(f"去重检查：{trade_date} 已提交 {skipped} 条，跳过，剩余 {len(orders)} 条")
    if not orders:
        log.info(f"{trade_date} 全部订单已提交过，退出")
        return

    # 关键：SELL 排在 BUY 前面执行
    # V20H 的 SELL→BUY 周转逻辑依赖卖出回笼现金来覆盖 BUY 成本。
    # FIFO 风控如果先吃 BUY，会因 cash 不足误拒 BUY，且这些 BUY 会被服务器
    # auto-promote 进黑名单（错杀好股票）。SELL 优先能避免这个问题。
    orders.sort(key=lambda o: (0 if o["direction"] == "SELL" else 1,
                                o["account_group"], o["symbol"]))
    n_sell = sum(1 for o in orders if o["direction"] == "SELL")
    n_buy = sum(1 for o in orders if o["direction"] == "BUY")
    log.info(f"{trade_date} 共 {len(orders)} 条订单 (SELL={n_sell} BUY={n_buy}, SELL 优先排序)")

    group_qmt_map: dict[str, str | None] = {}
    for order in orders:
        ag = order["account_group"]
        if ag not in group_qmt_map:
            group_qmt_map[ag] = config.get_qmt_account_id(ag)

    valid_groups   = {ag: qid for ag, qid in group_qmt_map.items() if qid is not None}
    invalid_groups = {ag for ag, qid in group_qmt_map.items() if qid is None}
    for ag in invalid_groups:
        _wechat_alert(
            f"account_group={ag!r} 在 strategies.yaml 中未找到 qmt_account_id，"
            f"本组 {sum(1 for o in orders if o['account_group']==ag)} 笔订单跳过"
        )
    if not valid_groups and config.PUSH_MODE != "mock_qmt":
        log.error("所有 account_group 均无有效映射，退出")
        return

    _sleep_until("0915")
    log.info("=== 开始风控校验与下单 ===")

    groups: dict[str, list[dict]] = {}
    for order in orders:
        groups.setdefault(order["account_group"], []).append(order)

    all_records: list[dict] = []
    submitted_at = datetime.now().isoformat()

    if config.PUSH_MODE == "mock_qmt":
        mock_state = config.load_mock_qmt_state()
        for ag, group_orders in groups.items():
            log.info(f"[mock] 处理 account_group={ag}，共 {len(group_orders)} 笔")
            mock_trader = _build_mock_trader(mock_state, ag)
            pass_orders, fail_orders = _risk_check(mock_trader, None, f"MOCK_{ag}", group_orders)
            for fo in fail_orders:
                _wechat_alert(
                    f"本地风控拦截：{fo['symbol']} {fo['direction']} account={ag}，{fo['fail_reason']}"
                )
                all_records.append({
                    "local_order_id": f"FAIL_{fo['order_id']}",
                    "order_id": fo["order_id"], "account_group": ag,
                    "qmt_account_id": f"MOCK_{ag}", "symbol": fo["symbol"],
                    "direction": fo["direction"], "submitted_price": fo["limit_price"],
                    "submitted_quantity": fo["quantity"], "submitted_at": submitted_at,
                    "submit_status": "FAILED", "fail_reason": fo["fail_reason"],
                })
            for order in pass_orders:
                all_records.append(_mock_submit_single(order, submitted_at))
    else:
        needed_qmt_ids = list(set(valid_groups.values()))
        try:
            xt_trader, accounts = _connect_qmt(needed_qmt_ids)
        except RuntimeError as e:
            _wechat_alert(str(e))
            return
        log.info("QMT 连接成功")
        try:
            for ag, group_orders in groups.items():
                if ag not in valid_groups:
                    continue
                qmt_id = valid_groups[ag]
                acc    = accounts[qmt_id]
                log.info(f"处理 account_group={ag}，qmt_account_id={qmt_id}，共 {len(group_orders)} 笔")
                pass_orders, fail_orders = _risk_check(xt_trader, acc, qmt_id, group_orders)
                for fo in fail_orders:
                    _wechat_alert(f"本地风控拦截：{fo['symbol']} {fo['direction']} account={ag}，{fo['fail_reason']}")
                    all_records.append({
                        "local_order_id": f"FAIL_{fo['order_id']}",
                        "order_id": fo["order_id"], "account_group": ag,
                        "qmt_account_id": qmt_id, "symbol": fo["symbol"],
                        "direction": fo["direction"], "submitted_price": fo["limit_price"],
                        "submitted_quantity": fo["quantity"], "submitted_at": submitted_at,
                        "submit_status": "FAILED", "fail_reason": fo["fail_reason"],
                    })
                if not pass_orders:
                    continue
                for o in pass_orders:
                    record = _submit_single(xt_trader, acc, qmt_id, o, submitted_at)
                    all_records.append(record)
                    log.info(
                        f"  {record['symbol']} {record['direction']} "
                        f"qty={record['submitted_quantity']} price={record['submitted_price']} "
                        f"→ {record['submit_status']}"
                    )
                    if record["submit_status"] == "FAILED":
                        _wechat_alert(f"QMT 下单失败：{record['symbol']} account={ag}，{record['fail_reason']}")
        finally:
            xt_trader.stop()

    _write_local_orders(all_records)
    success = sum(1 for r in all_records if r["submit_status"] == "SUCCESS")
    failed  = sum(1 for r in all_records if r["submit_status"] == "FAILED")
    # 按账户/方向汇总成功订单
    by_acct = defaultdict(lambda: {"BUY": 0, "SELL": 0})
    for r in all_records:
        if r["submit_status"] == "SUCCESS":
            d = r.get("direction", "")
            if d in ("BUY", "SELL"):
                by_acct[r.get("account_group", "?")][d] += 1
    parts = []
    for acct in sorted(by_acct.keys()):
        b = by_acct[acct]
        parts.append(f"{acct} 买入{b['BUY']}笔，卖出{b['SELL']}笔")
    summary = f"成功 {success} 笔" + (f"，失败 {failed} 笔" if failed else "")
    log.info(f"下单汇总：{summary}")
    _wechat_notify(
        f"order_submit {trade_date}：{summary}，其中 {'；'.join(parts)}"
    )
    log.info("=== order_submit 完成 ===")


if __name__ == "__main__":
    main()
