#!/usr/bin/env python3
"""一键拉取云端策略健康度 + 本周 NAV / orders / blacklist 概览。

用法:
    python scripts/dashboard.py                  # 默认输出本周
    python scripts/dashboard.py --days 14        # 看两周
    python scripts/dashboard.py --instance paper_v20h_v20h_v1_3
    python scripts/dashboard.py --json           # 机器可读输出

环境变量:
    QMT_PIPELINE_BASE_URL  default http://120.26.138.82:8000
    QMT_PIPELINE_API_KEY   default pipeline-v23-shared-secret-2026
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import requests

BASE = os.environ.get("QMT_PIPELINE_BASE_URL", "http://120.26.138.82:8000").rstrip("/")
KEY = os.environ.get("QMT_PIPELINE_API_KEY", "pipeline-v23-shared-secret-2026")
HDR = {"Authorization": f"Bearer {KEY}"}


def _get(path: str, **params) -> dict:
    r = requests.get(f"{BASE}{path}", headers=HDR, params=params, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"{path}: code={body['code']} message={body['message']}")
    return body["data"]


def print_section(title: str):
    print()
    print("═" * 78)
    print(f"  {title}")
    print("═" * 78)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="orders summary 回溯天数 (default 7)")
    ap.add_argument("--instance", default=None, help="nav-history 指定 instance_id")
    ap.add_argument("--json", action="store_true", help="纯 JSON 输出（机器可读）")
    args = ap.parse_args()

    # 整合所有数据
    health = _get("/admin/health")
    orders_sum = _get("/admin/orders-summary", days=args.days)
    nav = _get(
        "/admin/nav-history",
        instance_id=args.instance or "paper_v20h_v20h_v1_3",
        limit=args.days + 3,
    )
    blacklist = _get("/admin/blacklist", lookback_days=30)

    if args.json:
        print(json.dumps({
            "health": health,
            "orders_summary": orders_sum,
            "nav_history": nav,
            "blacklist": blacklist,
        }, indent=2, ensure_ascii=False))
        return

    # ── 人类可读版 ──
    print(f"📡 V20H Server Dashboard — fetched {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   {BASE}")

    # 1. Health
    print_section("① 服务健康度")
    ps = health.get("pred_status") or {}
    if "error" not in ps:
        lag = ps.get("lag_hours")
        lag_str = f"{lag:.1f} h ago" if lag is not None else "?"
        warn = "⚠️" if lag and lag > 48 else "✅"
        print(f"  Pred mtime:       {ps.get('mtime')} ({lag_str}) {warn}")
        print(f"  Pred size:        {ps.get('size_mb')} MB")
    bl = health["blacklist"]
    print(f"  Blacklist:        auto={bl['auto_unique']:3d}  manual={bl['manual_count']:3d}  合并={bl['merged_total']:3d}")
    print(f"  Pending orders:   {health['pending_orders_total']:4d}")

    # 2. Instances
    print_section("② 各策略实例状态")
    print(f"  {'instance_id':<32} {'cash':>14} {'holdings':>10} {'最新 NAV':>14} {'日收益':>10}")
    print(f"  {'─'*32} {'─'*14} {'─'*10} {'─'*14} {'─'*10}")
    for inst in health["instances"]:
        ret = inst.get("latest_daily_return")
        ret_str = f"{ret*100:+.3f}%" if ret is not None else "—"
        nav_val = inst.get("latest_nav")
        nav_str = f"¥{nav_val:>12,.0f}" if nav_val else "—"
        print(f"  {inst['instance_id']:<32} ¥{inst['virtual_cash']:>12,.0f} "
              f"{inst['holdings_count']:>10} {nav_str:>14} {ret_str:>10}")

    # 3. 本周 orders 矩阵
    print_section(f"③ 最近 {args.days} 天 orders 状态矩阵 (status × direction)")
    by_date = orders_sum["by_date"]
    if not by_date:
        print("  本期无 orders")
    else:
        for d in sorted(by_date.keys()):
            print(f"\n  📅 {d}:")
            for ag, dirs in by_date[d].items():
                for direction, statuses in dirs.items():
                    total = sum(statuses.values())
                    parts = " | ".join(f"{st}={n}" for st, n in sorted(statuses.items()))
                    print(f"    {ag:<14} {direction}  total={total:<5}  {parts}")

    # 4. NAV 历史
    print_section(f"④ NAV 历史 (paper_v20h_v20h_v1_3, 最近 {args.days+3} 天)")
    items = nav["items"]
    if not items:
        print("  perf_snapshots 暂无数据")
    else:
        print(f"  {'date':<12} {'NAV':>14} {'daily_ret':>10} {'累计 ret':>10} {'持仓数':>8}")
        print(f"  {'─'*12} {'─'*14} {'─'*10} {'─'*10} {'─'*8}")
        base = items[-1]["nav"]  # 注意 items 是倒序
        for it in sorted(items, key=lambda x: x["date"]):
            ret = it.get("daily_return")
            ret_str = f"{ret*100:+.3f}%" if ret is not None else "—"
            cum = (it["nav"] / base - 1) * 100
            print(f"  {it['date']:<12} ¥{it['nav']:>12,.0f} {ret_str:>10} "
                  f"{cum:+9.2f}% {it.get('positions_count',0):>8}")

    # 5. Blacklist
    print_section(f"⑤ 黑名单 (合计 {blacklist['merged_total']} 个)")
    print(f"  自动派生 (近 30 天 REJECTED, 累计 {blacklist['auto_rejected_total']} 单):")
    auto = list(blacklist["auto_by_symbol"].items())[:15]
    if auto:
        for sym, cnt in auto:
            print(f"    {sym:<14} REJECTED × {cnt}")
        if len(blacklist["auto_by_symbol"]) > 15:
            print(f"    ... 还有 {len(blacklist['auto_by_symbol']) - 15} 个")

    print(f"\n  手工维护:")
    manual = blacklist.get("manual_entries", [])[:15]
    if manual:
        for e in manual:
            print(f"    {e['symbol']:<14} {e.get('reason') or '(无 reason)'}")
        if len(blacklist["manual_entries"]) > 15:
            print(f"    ... 还有 {len(blacklist['manual_entries']) - 15} 个")

    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(f"❌ 出错: {e}", file=sys.stderr)
        sys.exit(1)
