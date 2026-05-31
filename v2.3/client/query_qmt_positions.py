"""
模块八：QMT 真实账户对账

把 QMT 当前真实 cash + 持仓推到服务器 /admin/reconcile-positions，
和服务器 instance_state.virtual_* 做 diff。

用法:
    python query_qmt_positions.py                       # 默认 dry-run，只看 diff
    python query_qmt_positions.py --apply               # 真改 server 状态
    python query_qmt_positions.py --account-group X     # 指定哪个账户组
    python query_qmt_positions.py --instance Y          # 指定哪个 instance

典型场景：
    1. 怀疑 server 和 QMT 账本分叉（PENDING 总是失败、SELL 持续 REJECTED 等）
    2. 手动在 QMT 操作过持仓后，让 server 强制同步过来
    3. EOD 之后例行对账（建议每周一次）
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import requests
from xtquant import xtconstant  # noqa: F401
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

log = config.setup_logger("query_qmt_positions")


def startup_check() -> None:
    if not config.SERVER_BASE_URL:
        log.error("SERVER_BASE_URL 未配置（QMT_PIPELINE_BASE_URL）")
        sys.exit(2)
    if not config.API_KEY:
        log.error("API_KEY 未配置（QMT_PIPELINE_API_KEY）")
        sys.exit(2)
    if not os.path.exists(config.QMT_USERDATA_DIR):
        log.error(f"QMT 数据目录不存在: {config.QMT_USERDATA_DIR}")
        sys.exit(2)


def query_qmt_account(qmt_account_id: str) -> tuple[float, dict[str, int]]:
    """返回 (qmt_cash, {symbol: volume})。
    持仓自动过滤占位股（单支持仓 > 1000 万股）。
    """
    log.info(f"连接 QMT，账户 {qmt_account_id}")
    xt_trader = XtQuantTrader(config.QMT_USERDATA_DIR, config.QMT_SESSION_ID)
    xt_trader.register_callback(XtQuantTraderCallback())
    xt_trader.start()
    rc = xt_trader.connect()
    if rc != 0:
        xt_trader.stop()
        raise RuntimeError(f"QMT 连接失败 rc={rc}")

    acc = StockAccount(qmt_account_id)
    sub = xt_trader.subscribe(acc)
    if sub != 0:
        xt_trader.stop()
        raise RuntimeError(f"账户 {qmt_account_id} 订阅失败 rc={sub}")

    try:
        asset = xt_trader.query_stock_asset(acc)
        if asset is None:
            raise RuntimeError(f"query_stock_asset 返回 None")
        qmt_cash = float(asset.cash)

        positions_raw = xt_trader.query_stock_positions(acc) or []
        # XtQuant Position: stock_code (str), volume (int), can_use_volume (int)
        # 用 volume（总持仓）而非 can_use_volume（可用），对账要看实际持有
        positions: dict[str, int] = {}
        phantom = {}
        for p in positions_raw:
            v = int(p.volume)
            if v <= 0:
                continue
            if v > 10_000_000:  # 占位股(100亿股)过滤
                phantom[p.stock_code] = v
            else:
                positions[p.stock_code] = v

        if phantom:
            log.info(f"过滤占位股 {len(phantom)} 只: {list(phantom.keys())}")
        log.info(f"QMT 查询完成：cash=¥{qmt_cash:,.2f}，持仓 {len(positions)} 只（已过滤 {len(phantom)} 只占位）")
        return qmt_cash, positions
    finally:
        xt_trader.stop()


def post_reconcile(
    instance_id: str,
    qmt_account_id: str,
    qmt_cash: float,
    qmt_positions: dict[str, int],
    dry_run: bool,
) -> dict:
    payload = {
        "instance_id": instance_id,
        "qmt_account_id": qmt_account_id,
        "qmt_cash": qmt_cash,
        "qmt_positions": qmt_positions,
        "snapshot_time": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
    }
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    url = f"{config.SERVER_BASE_URL}/admin/reconcile-positions"
    log.info(f"POST {url}  dry_run={dry_run}")
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"server 返回 code={body.get('code')}: {body.get('message')}")
    return body["data"]


def print_report(result: dict) -> None:
    print("\n" + "=" * 70)
    print(f"  对账报告  instance={result['instance_id']}  "
          f"{'[DRY-RUN]' if result['dry_run'] else '[APPLIED]'}")
    print("=" * 70)
    print(f"  现金:  server ¥{result['server_cash']:,.2f}  "
          f"vs QMT ¥{result['qmt_cash']:,.2f}  "
          f"(diff {result['cash_diff']:+,.2f})")
    print(f"  持仓:  server {result['n_server_positions']} 只  "
          f"vs QMT {result['n_qmt_positions']} 只")
    print(f"    匹配             {result['n_matched']:>4} 只")
    print(f"    数量不一致       {result['n_mismatched']:>4} 只")
    print(f"    server 多余      {result['n_server_only']:>4} 只 ← 幽灵持仓嫌疑")
    print(f"    QMT 多余         {result['n_qmt_only']:>4} 只 ← 漏推 trade_result 嫌疑")
    print()
    if result["dry_run"] and result.get("diffs"):
        print("  Diff 详情（前 30）：")
        print(f"    {'Symbol':<12} {'Server':>8} {'QMT':>8} {'Diff':>8}")
        for d in result["diffs"][:30]:
            print(f"    {d['symbol']:<12} {d['server_qty']:>8} "
                  f"{d['qmt_qty']:>8} {d['diff']:>+8}")
        if len(result["diffs"]) > 30:
            print(f"    ... 还有 {len(result['diffs']) - 30} 个差异未显示")
        print()
        print("  -> 加 --apply 参数才会真正改 instance_state")
    elif result.get("applied"):
        print("  ✅ instance_state 已强制对齐到 QMT 真实状态")
    else:
        print("  ✓ server 和 QMT 完全一致，无需调整")
    print("=" * 70 + "\n")


def _compute_strategy_cash(start_capital: float = 10_000_000,
                           account_group: str = "paper_v20h") -> float:
    """从本地 DB 估算某 account_group 策略实际可用现金。
    排除 5.7 异常买入，用 local_orders SUCCESS 订单的委托金额近似。

    必须按 account_group 过滤：local_orders 是全策略共用一张表，V20H 和
    V53（共用 QMT 账户 301300148788）的成交都写在这里，不过滤会把别的策略
    的买卖也算进来，导致现金严重失真。
    """
    import sqlite3
    db_path = getattr(config, "DB_PATH", None)
    if not db_path or not os.path.exists(db_path):
        log.warning("本地 DB 不存在，无法计算策略现金")
        return None
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        SELECT direction, SUM(submitted_quantity * submitted_price)
        FROM local_orders
        WHERE submit_status = 'SUCCESS'
          AND account_group = ?
          AND NOT (submitted_at LIKE '2026-05-07%%' AND direction = 'BUY')
        GROUP BY direction
    """, (account_group,))
    buy_cost = 0.0
    sell_back = 0.0
    for direction, amt in cur.fetchall():
        if direction == "BUY":
            buy_cost = amt or 0.0
        else:
            sell_back = amt or 0.0
    conn.close()
    cash = start_capital - buy_cost + sell_back
    log.info(f"策略现金估算：start=¥{start_capital:,.0f} buy=-¥{buy_cost:,.0f} sell=+¥{sell_back:,.0f} → cash=¥{cash:,.2f}")
    return cash


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--account-group", default="paper_v20h",
        help="strategies.yaml 里的 group_id (默认 paper_v20h)",
    )
    parser.add_argument(
        "--instance", default="paper_v20h_v20h_v1_3",
        help="instance_state 表里的 instance_id (默认 paper_v20h_v20h_v1_3)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="不传则 dry_run；传了才真正改 server 状态",
    )
    parser.add_argument(
        "--start-capital", type=float, default=10_000_000,
        help="策略初始资金（默认 10,000,000）",
    )
    args = parser.parse_args()

    startup_check()

    qmt_account_id = config.get_qmt_account_id(args.account_group)
    if not qmt_account_id:
        log.error(f"account_group={args.account_group} 在 strategies.yaml 里没配 qmt_account_id")
        sys.exit(2)

    # 1. 拉 QMT 真实账户
    qmt_raw_cash, positions = query_qmt_account(qmt_account_id)

    # 2. 策略现金（排除全账户闲置资金 + 别的 account_group 的成交）
    strategy_cash = _compute_strategy_cash(args.start_capital, args.account_group)
    if strategy_cash is None:
        strategy_cash = qmt_raw_cash
    log.info(f"对账发送：现金=¥{strategy_cash:,.2f} 持仓={len(positions)}只")

    # 3. POST 对账 (默认 dry-run)
    result = post_reconcile(
        instance_id=args.instance,
        qmt_account_id=qmt_account_id,
        qmt_cash=strategy_cash,
        qmt_positions=positions,
        dry_run=not args.apply,
    )

    # 3. 打印报告
    print_report(result)

    # 4. dry_run 模式时友好提示
    if (not args.apply) and (
        result["n_server_only"] > 0
        or result["n_qmt_only"] > 0
        or result["n_mismatched"] > 0
        or abs(result["cash_diff"]) > 1.0
    ):
        log.warning("检测到分叉，再加 --apply 一次性同步过来：")
        log.warning(f"  python {__file__.split('/')[-1]} --apply")


if __name__ == "__main__":
    main()
