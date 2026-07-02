"""一次性恢复工具：order_id 重映射 + 成交回报重推。

适用场景（2026-07-02 事故）：客户端拉取信号后，服务器重算管线换掉了 order_id，
次日收盘成交回报全量 unmatched、成交没有入账。本脚本：

  1. GET /orders?date=<date> 拉服务器当前（新）订单批次
  2. 读本地 audit_log 里当日已推送过的成交（旧 order_id + 成交数据）
  3. 按 (symbol, direction, quantity) 一一对应旧→新 order_id（必须完全双射，否则中止）
  4. --apply 时：备份 DB → 重写 local_orders / server_orders / audit_log 的 order_id
     → 按新 ID 重推 POST /trade-result

用法（在 v2.3/client/ 下，激活 venv）：
    python remap_repush.py --date 20260702            # 干跑：只打印映射，不改任何东西
    python remap_repush.py --date 20260702 --apply    # 实际执行

不依赖 xtquant / QMT，任何时间都能跑（成交数据取自 audit_log，不需要 QMT 当日会话）。
推完后到服务器 GET /admin/bookkeeping-divergence 确认没有对账失败标记。
"""

import argparse
import os
import shutil
import sqlite3
from datetime import datetime

import requests

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

log = config.setup_logger("remap_repush")


def startup_check() -> None:
    assert os.path.exists(config.DB_PATH), f"本地 DB 不存在: {config.DB_PATH}"
    assert config.SERVER_BASE_URL, "SERVER_BASE_URL 未配置"
    assert config.API_KEY,         "API_KEY 未配置"
    log.info("启动检查通过")


def _fetch_server_orders(date: str) -> list[dict]:
    url     = config.SERVER_BASE_URL.rstrip("/") + "/orders"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    resp    = requests.get(url, params={"date": date}, headers=headers, timeout=30)
    body    = resp.json()
    assert body.get("code") == 0, f"GET /orders 失败: {body}"
    return (body.get("data") or {}).get("orders") or []


def _load_audit_rows(date: str) -> list[dict]:
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM audit_log WHERE trade_date = ?", (date,))
        return [dict(r) for r in cur.fetchall()]


def _key(symbol: str, direction: str, qty: int) -> tuple:
    return (symbol, direction, int(qty))


def _build_mapping(audit_rows: list[dict], server_orders: list[dict]) -> dict[str, str]:
    """旧 order_id → 新 order_id。任何一侧 key 重复或对不上就中止（宁可不动）。"""
    new_by_key: dict[tuple, dict] = {}
    for o in server_orders:
        k = _key(o["symbol"], o["direction"], o["quantity"])
        assert k not in new_by_key, f"服务器批次内 key 重复，无法安全映射: {k}"
        new_by_key[k] = o

    mapping: dict[str, str] = {}
    seen_keys: set[tuple] = set()
    for r in audit_rows:
        k = _key(r["symbol"], r["direction"], r["submitted_qty"])
        assert k not in seen_keys, f"audit_log 内 key 重复，无法安全映射: {k}"
        seen_keys.add(k)
        assert k in new_by_key, \
            f"服务器批次里找不到对应订单: {k}（旧 order_id={r['order_id']}）"
        mapping[r["order_id"]] = new_by_key[k]["order_id"]

    leftover = set(new_by_key) - seen_keys
    assert not leftover, f"服务器批次有 {len(leftover)} 条订单在 audit_log 里没有对应成交: {sorted(leftover)}"
    return mapping


def _backup_db() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst   = f"{config.DB_PATH}.bak-{stamp}"
    shutil.copy2(config.DB_PATH, dst)
    log.info(f"DB 已备份: {dst}")
    return dst


def _apply_mapping(date: str, mapping: dict[str, str], server_orders: list[dict]) -> None:
    received_at = datetime.now().isoformat()
    with sqlite3.connect(config.DB_PATH) as conn:
        for old, new in mapping.items():
            conn.execute(
                "UPDATE local_orders SET order_id = ? WHERE order_id = ?", (new, old))
            conn.execute(
                "UPDATE audit_log SET order_id = ? WHERE trade_date = ? AND order_id = ?",
                (new, date, old))
        # server_orders 当日记录整体替换为服务器最新批次
        conn.execute("DELETE FROM server_orders WHERE valid_date = ?", (date,))
        for o in server_orders:
            conn.execute("""
                INSERT OR IGNORE INTO server_orders (
                    order_id, account_group, symbol, direction,
                    quantity, limit_price, valid_date, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                o["order_id"], o.get("account_group"), o.get("symbol"),
                o.get("direction"), o.get("quantity"), o.get("limit_price"),
                o["valid_date"], received_at,
            ))
        conn.commit()
    log.info(f"本地 DB 重映射完成：local_orders/audit_log/server_orders 共 {len(mapping)} 条")


def _repush(date: str) -> bool:
    """按（已重映射的）audit_log 重推成交回报。"""
    rows    = _load_audit_rows(date)
    results = []
    for r in rows:
        results.append({
            "order_id":        r["order_id"],
            "filled_quantity": r["filled_qty"],
            "filled_price":    r["filled_price"],
            "status":          r["status"],
            "symbol":          r["symbol"],
            "direction":       r["direction"],
        })
    url     = config.SERVER_BASE_URL.rstrip("/") + "/trade-result"
    headers = {"Authorization": f"Bearer {config.API_KEY}",
               "Content-Type": "application/json"}
    resp = requests.post(url, json={"trade_date": date, "results": results},
                         headers=headers, timeout=60)
    body = resp.json()
    assert body.get("code") == 0, f"POST /trade-result 失败: {body}"
    d         = body.get("data", {})
    unmatched = d.get("unmatched_order_ids") or []
    log.info(f"重推完成：matched={d.get('matched_count')} unmatched={len(unmatched)}")
    if unmatched:
        log.error(f"仍有未匹配 order_id：{unmatched}")
        log.error(f"服务器候选提示：{d.get('unmatched_candidates')}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="交易日 YYYYMMDD，如 20260702")
    parser.add_argument("--apply", action="store_true",
                        help="实际执行（默认只干跑打印映射）")
    args = parser.parse_args()

    startup_check()

    server_orders = _fetch_server_orders(args.date)
    audit_rows    = _load_audit_rows(args.date)
    log.info(f"服务器当前批次 {len(server_orders)} 条；本地 audit_log {len(audit_rows)} 条")
    assert server_orders, f"服务器 {args.date} 无 PENDING 订单，无需/无法重映射"
    assert audit_rows,    f"本地 audit_log 无 {args.date} 记录，没有可重推的成交"

    already_same = [r for r in audit_rows
                    if r["order_id"] in {o["order_id"] for o in server_orders}]
    assert not already_same, \
        f"audit_log 里有 {len(already_same)} 条 order_id 已与服务器一致，" \
        "看起来不需要重映射，请人工确认后再操作"

    mapping = _build_mapping(audit_rows, server_orders)

    print(f"\n=== order_id 映射（{len(mapping)} 条）===")
    audit_by_old = {r["order_id"]: r for r in audit_rows}
    for old, new in sorted(mapping.items(), key=lambda kv: audit_by_old[kv[0]]["symbol"]):
        r = audit_by_old[old]
        print(f"  {r['symbol']:<10} {r['direction']:<4} qty={r['submitted_qty']:<7} "
              f"fill={r['filled_qty']}@{r['filled_price']:<9} {r['status']:<9} "
              f"{old[:8]}… → {new[:8]}…")

    if not args.apply:
        print("\n[干跑] 未做任何修改。确认映射无误后加 --apply 执行。")
        return 0

    _backup_db()
    _apply_mapping(args.date, mapping, server_orders)
    ok = _repush(args.date)
    if ok:
        print("\n✅ 重推成功。请再到服务器核对：")
        print("   1. GET /admin/orders?date=" + args.date + " → 状态应为 FILLED")
        print("   2. GET /admin/bookkeeping-divergence → 应无新增对账失败标记")
        return 0
    print("\n❌ 重推后仍有未匹配，本地 DB 已重映射但服务器未完全入账，请把日志发给服务器端排查。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
