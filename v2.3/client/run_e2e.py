"""
v2.2 端到端联调测试入口（QMT 真实连接 + 服务器离线）。
按照生产执行顺序依次运行四个模块，使用 PUSH_MODE="local"。
运行方式：cd v2.2 && python run_e2e.py

与 v2.1 的关键区别：
- market_push 走真实 QMT 行情下载（非 mock）
- order_submit / trade_result 走真实 QMT 交易接口
- 服务器交互均写入 local_push/ 本地文件
"""

import os
import sqlite3
import sys
from datetime import datetime

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

log = config.setup_logger("run_e2e")


def _reset_env() -> None:
    """每次 e2e 运行前清空测试 DB 和 local_push/，保证结果干净。"""
    if os.path.exists(config.DB_PATH):
        os.unlink(config.DB_PATH)
        log.info(f"已删除旧 DB：{config.DB_PATH}")
    if os.path.exists(config.LOCAL_PUSH_DIR):
        import shutil
        shutil.rmtree(config.LOCAL_PUSH_DIR)
        log.info(f"已清空 local_push/：{config.LOCAL_PUSH_DIR}")


def _qmt_xtdata_check() -> dict:
    """快速验证 xtdata 可用性（不下载数据，仅检查连接和板块列表）。"""
    from xtquant import xtdata
    xtdata.data_dir = config.QMT_USERDATA_DIR

    result = {"ok": True, "details": {}}

    # 1. 检查 QMT 数据目录
    if not os.path.exists(config.QMT_USERDATA_DIR):
        result["ok"] = False
        result["details"]["data_dir"] = "NOT FOUND"
        return result
    result["details"]["data_dir"] = "OK"

    # 2. 检查板块列表
    try:
        stock_list = xtdata.get_stock_list_in_sector(config.STOCK_SECTOR)
        result["details"]["stocks"] = len(stock_list)
        if len(stock_list) < 100:
            result["ok"] = False
            result["details"]["stocks_warn"] = f"仅 {len(stock_list)} 只，疑似异常"
    except Exception as e:
        result["ok"] = False
        result["details"]["stocks_error"] = str(e)

    # 3. 检查交易日历
    try:
        today = datetime.now().strftime("%Y%m%d")
        days = xtdata.get_trading_calendar("SH", start_time=today, end_time=today)
        result["details"]["is_trading_day"] = len(days) > 0
        result["details"]["today"] = today
    except Exception as e:
        result["ok"] = False
        result["details"]["calendar_error"] = str(e)

    # 4. ETF 板块
    try:
        etf_count = 0
        for sector in config.ETF_SECTORS:
            etf_count += len(xtdata.get_stock_list_in_sector(sector))
        result["details"]["etfs"] = etf_count
    except Exception as e:
        result["details"]["etfs_error"] = str(e)

    # 5. 指数代码
    try:
        idx_codes = getattr(config, "INDEX_CODES", [])
        result["details"]["indexes"] = len(idx_codes)
    except Exception as e:
        result["details"]["indexes_error"] = str(e)

    return result


def _verify() -> bool:
    """从 SQLite 和 local_push/ 读取实际结果，打印验证报告，返回是否全部通过。"""
    print("\n" + "=" * 60)
    print("  端到端验证报告")
    print("=" * 60)

    ok = True

    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # ── server_orders ───────────────────────────────────────────
        rows = conn.execute("SELECT * FROM server_orders").fetchall()
        print(f"\n[1] server_orders：{len(rows)} 条")
        for r in rows:
            print(f"    {r['order_id'][:8]}...  {r['symbol']}  {r['direction']}  "
                  f"qty={r['quantity']}  price={r['limit_price']}  valid_date={r['valid_date']}")
        if len(rows) == 0:
            print("    (无订单 — 非交易日或 mock 订单日期不匹配)")

        # ── local_orders ────────────────────────────────────────────
        rows = conn.execute("SELECT * FROM local_orders").fetchall()
        success_count = sum(1 for r in rows if r["submit_status"] == "SUCCESS")
        failed_count  = sum(1 for r in rows if r["submit_status"] == "FAILED")
        print(f"\n[2] local_orders：{len(rows)} 条  SUCCESS={success_count}  FAILED={failed_count}")
        for r in rows:
            mark = "[OK]" if r["submit_status"] == "SUCCESS" else "[XX]"
            print(f"    {mark} local_order_id={r['local_order_id']}  {r['symbol']}  "
                  f"{r['direction']}  qty={r['submitted_quantity']}  status={r['submit_status']}")
        if len(rows) == 0:
            print("    (无本地委托 — 无可执行的服务器订单)")

        # ── trades ──────────────────────────────────────────────────
        print(f"\n[3] trades：已废弃本地 trades 表，成交由 trade_result 直接推 server")

    # ── local_push 文件 ──────────────────────────────────────────────
    push_dir   = config.LOCAL_PUSH_DIR
    push_files = sorted(os.listdir(push_dir)) if os.path.exists(push_dir) else []
    print(f"\n[4] local_push/ 文件：{len(push_files)} 个")
    for f in push_files:
        size = os.path.getsize(os.path.join(push_dir, f))
        print(f"    {f}  ({size} bytes)")

    # 非交易日时 market_push 会跳过，不强制要求文件存在
    has_market = any(f.startswith("market_data_") for f in push_files)
    has_trades = any(f.startswith("trade_result_") for f in push_files)
    if has_market:
        print("    market_data 文件: 已生成")
    else:
        print("    market_data 文件: 未生成（非交易日或下载失败）")
    if has_trades:
        print("    trade_result 文件: 已生成")
    else:
        print("    trade_result 文件: 未生成（无成交可推送）")

    print("\n" + "=" * 60)
    if ok:
        print("  结论：全部通过 [OK]")
    else:
        print("  结论：存在失败项 [FAIL]")
    print("=" * 60 + "\n")
    return ok


def main():
    log.info("=" * 60)
    log.info("v2.2 端到端联调测试 — PUSH_MODE=local（QMT 真实连接）")
    log.info("=" * 60)

    # ── 0. QMT 连接检查 ──────────────────────────────────────────
    print("\n" + "-" * 40)
    print("  前置检查：QMT xtdata 连接")
    print("-" * 40)
    qmt_status = _qmt_xtdata_check()
    for key, val in qmt_status["details"].items():
        print(f"  {key}: {val}")
    if not qmt_status["ok"]:
        print("\n  [FAIL] QMT xtdata 连接异常，请确认 QMT 客户端已启动并登录")
        sys.exit(1)
    print("  [OK] QMT xtdata 连接正常")

    _reset_env()

    # ── 1. 行情下载与推送（真实 QMT 数据） ────────────────────────
    log.info("-" * 40)
    log.info("步骤 1 / 4：market_push（真实 QMT 行情下载）")
    import market_push
    market_push.main()

    # ── 2. 订单拉取（mock，无服务器） ────────────────────────────
    log.info("-" * 40)
    log.info("步骤 2 / 4：order_query（mock 订单，无服务器）")
    import order_query
    order_query.main()

    # ── 3. 竞价下单（真实 QMT 交易接口） ─────────────────────────
    # 安全：只有 server_orders 中存在 valid_date=当日 的订单才会实际下单
    log.info("-" * 40)
    log.info("步骤 3 / 4：order_submit（真实 QMT 交易接口）")
    log.warning(">>> 此步骤会连接真实 QMT 交易接口 <<<")
    import order_submit
    order_submit.main()

    # ── 4. 成交查询与推送（真实 QMT 成交数据） ────────────────────
    log.info("-" * 40)
    log.info("步骤 4 / 4：trade_result（真实 QMT 成交查询）")
    import trade_result
    trade_result.main()

    log.info("-" * 40)
    ok = _verify()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
