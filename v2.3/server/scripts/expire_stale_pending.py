"""一次性运维脚本：把僵尸 PENDING 挂单标记 EXPIRED，清理订单/告警视图。

背景
----
模拟盘成交回报带的 order_id 对不上 server 下单时生成的 order_id，真实卖单永远
拿不到匹配 fill，停在 PENDING（2026-06 复发，积压 29→40）。这些单过了竞价
(valid_date) 已不可能再成交；标记 EXPIRED 与既有语义一致（unfilled past
validity，库里本就有 14 笔 SELL EXPIRED），且不影响虚拟账本（这些卖单从未在
账本兑现，positions/cash 不变），只把视图擦干净。

安全
----
- 默认 dry-run，只打印不落库；--apply 才写。
- --apply 落库前自动备份 DB 到 <db>.bak-<ts>。
- 防御：candidate 里若有 order_id 命中 trades（真有成交），跳过不动，留人工核对。
- 自包含，只依赖 app.db / app.models，可跑在部署端较旧的代码上。

用法
----
    python -m scripts.expire_stale_pending --db pipeline-server.db --today 20260626          # 预演
    python -m scripts.expire_stale_pending --db pipeline-server.db --today 20260626 --apply   # 落库
"""
from __future__ import annotations

import argparse
import shutil
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.db import make_engine, make_session_factory
from app.models import Order, Trade


def _d(yyyymmdd: str) -> date:
    s = str(yyyymmdd)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def expire_stale_pending(session_factory, max_age_days: int = 2,
                         today: str | None = None, apply: bool = False) -> dict:
    """把 valid_date 早于 today-max_age_days 的 PENDING 单标记 EXPIRED。

    Returns: {candidates, to_expire, expired, skipped_filled, orders}
    apply=False 时 expired 恒为 0（仅预演）。
    """
    end = _d(today) if today else datetime.now().date()
    cutoff = (end - timedelta(days=max_age_days)).strftime("%Y%m%d")
    expired = 0
    with session_factory() as s:
        rows = s.execute(
            select(Order)
            .where(Order.status == "PENDING", Order.valid_date <= cutoff)
            .order_by(Order.valid_date)
        ).scalars().all()
        cand_ids = [o.order_id for o in rows]
        filled_ids = {x[0] for x in s.execute(
            select(Trade.order_id).where(Trade.order_id.in_(cand_ids))
        ).all()} if cand_ids else set()
        to_expire = [o for o in rows if o.order_id not in filled_ids]
        skipped_filled = [o.order_id for o in rows if o.order_id in filled_ids]
        orders = [{"order_id": o.order_id, "account_group": o.account_group,
                   "symbol": o.symbol, "direction": o.direction,
                   "quantity": o.quantity, "valid_date": o.valid_date,
                   "age_days": (end - _d(o.valid_date)).days} for o in rows]
        if apply:
            for o in to_expire:
                o.status = "EXPIRED"
                expired += 1
            s.commit()
    return {"candidates": len(rows), "to_expire": len(to_expire),
            "expired": expired, "skipped_filled": skipped_filled, "orders": orders}


def main() -> None:
    ap = argparse.ArgumentParser(description="清理僵尸 PENDING 挂单 → EXPIRED")
    ap.add_argument("--db", required=True, help="SQLite DB 路径")
    ap.add_argument("--max-age-days", type=int, default=2,
                    help="valid_date 早于 today-N 天才算僵尸（默认 2）")
    ap.add_argument("--today", default=None, help="YYYYMMDD，缺省取系统当天")
    ap.add_argument("--apply", action="store_true", help="落库（缺省仅预演）")
    args = ap.parse_args()

    if args.apply:
        backup = f"{args.db}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(args.db, backup)
        print(f"[backup] {backup}")

    sf = make_session_factory(make_engine(f"sqlite:///{args.db}"))
    res = expire_stale_pending(sf, max_age_days=args.max_age_days,
                               today=args.today, apply=args.apply)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] candidates={res['candidates']} to_expire={res['to_expire']} "
          f"expired={res['expired']} skipped_filled={len(res['skipped_filled'])}")
    for o in res["orders"]:
        print(f"  {o['valid_date']}  {o['account_group']:<11} {o['symbol']:<10} "
              f"{o['direction']} x{o['quantity']:<5} age={o['age_days']}d  {o['order_id']}")
    if res["skipped_filled"]:
        print(f"[!] 跳过（有匹配成交，需人工核对）: {res['skipped_filled']}")


if __name__ == "__main__":
    main()
