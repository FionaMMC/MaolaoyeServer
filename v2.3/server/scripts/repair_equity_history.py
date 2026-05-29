"""修复被污染的 strategy_state.equity_history / daily_rets（默认 dry-run）。

背景
----
5/21 NAV 暴涨事故（reconcile 误拉进 QMT 模拟器默认股）把 ¥198M 假净值固化进
instance_state.strategy_state.equity_history；同日重复 trigger 又 append 了
一串重复点（0.0 daily_ret）。后果：vol-target 的 realized vol 被假尖峰污染，
一旦 daily_rets 填满 20 个窗口就会把 vol_scale 钉到地板 0.3，长期欠配资金。

修复思路
--------
perf_snapshots 的 NAV 序列是干净的（已 backfill 修正、且 UPSERT 一天一条），
用它重建 equity_history + daily_rets。last_rb_idx / prev_hedge / last_di /
last_trade_date 一律保持不动（只修被污染的两列）。

vol-target 只用 daily_rets（收益率序列）算波动，equity_history 的绝对值仅用于
下次 step() 续算一条新收益率，所以用 NAV 序列重建在数学上是干净且充分的。
（首日 post-repair 可能因 pred-close vs parquet-close 价差产生一个极小 blip，
可忽略，远好于现在的 +1895% 假尖峰。）

用法
----
    # dry-run（只读，打印旧/新对比 + 波动率影响，不写库）
    cd /opt/qmt-server/v2.3/server && venv/bin/python -m scripts.repair_equity_history
    # 确认无误后写回
    venv/bin/python -m scripts.repair_equity_history --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.settings import get_settings  # noqa: E402

DEFAULT_INSTANCE = "paper_v20h_v20h_v1_3"
VOL_LOOKBACK = 20      # 与 plugins/v20h/config.yaml 一致
TARGET_VOL = 0.15
KEEP = max(VOL_LOOKBACK * 4, 100)   # 与 adapter 截断一致


def _vol_scale(daily_rets: list[float]) -> float:
    """复刻 V20HStrategy.compute_vol_scale。"""
    if len(daily_rets) < VOL_LOOKBACK:
        return 1.0
    recent = np.array(daily_rets[-VOL_LOOKBACK:])
    rv = float(np.std(recent) * np.sqrt(252))
    scale = TARGET_VOL / rv if rv > TARGET_VOL else 1.0
    return max(0.3, min(1.0, scale))


def _realized_vol(rets: list[float]) -> float:
    """年化波动率（对现有全部 rets，便于在 <20 时也看出尖峰影响）。"""
    if not rets:
        return float("nan")
    return float(np.std(np.array(rets)) * np.sqrt(252))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回数据库（默认 dry-run）")
    ap.add_argument("--instance", default=DEFAULT_INSTANCE)
    args = ap.parse_args()

    engine = create_engine(get_settings().db_url, future=True)

    # 1) 读当前 strategy_state（只读）
    with engine.connect() as con:
        row = con.execute(
            text("SELECT strategy_state FROM instance_state WHERE instance_id = :i"),
            {"i": args.instance},
        ).first()
        if not row or not row[0]:
            print(f"[repair] {args.instance} 无 strategy_state，退出")
            return 1
        state = json.loads(row[0])
        old_eq = [float(x) for x in state.get("equity_history", [])]
        old_dr = [float(x) for x in state.get("daily_rets", [])]

        navs = con.execute(
            text(
                "SELECT date, nav FROM perf_snapshots "
                "WHERE instance_id = :i AND nav > 0 ORDER BY date"
            ),
            {"i": args.instance},
        ).all()

    nav_series = [float(n) for _, n in navs]
    if len(nav_series) < 2:
        print(f"[repair] perf_snapshots NAV 序列不足（{len(nav_series)} 条），退出")
        return 1

    # 2) 重建
    new_eq = nav_series[-KEEP:]
    new_dr = [
        (nav_series[i] - nav_series[i - 1]) / nav_series[i - 1]
        for i in range(1, len(nav_series))
        if nav_series[i - 1] != 0
    ][-KEEP:]

    # 3) 对比报告
    print(f"=== {args.instance} equity_history 修复  (dry-run={not args.apply}) ===")
    print(f"[旧] equity_history len={len(old_eq)}  "
          f"max={max(old_eq):,.0f}  min={min(old_eq):,.0f}")
    print(f"     尾部6: {[round(x, 1) for x in old_eq[-6:]]}")
    print(f"[旧] daily_rets    len={len(old_dr)}  "
          f"max={max(old_dr):+.4f}  min={min(old_dr):+.4f}  "
          f"年化波动={_realized_vol(old_dr):.2%}  vol_scale(若激活)={_vol_scale(old_dr):.3f}")
    print(f"[新] equity_history len={len(new_eq)}  "
          f"max={max(new_eq):,.0f}  min={min(new_eq):,.0f}")
    print(f"     尾部6: {[round(x, 1) for x in new_eq[-6:]]}")
    print(f"[新] daily_rets    len={len(new_dr)}  "
          f"max={max(new_dr):+.4f}  min={min(new_dr):+.4f}  "
          f"年化波动={_realized_vol(new_dr):.2%}  vol_scale(若激活)={_vol_scale(new_dr):.3f}")

    # 4) 写回
    if args.apply:
        state["equity_history"] = [float(x) for x in new_eq]
        state["daily_rets"] = [float(x) for x in new_dr]
        with engine.begin() as con:
            con.execute(
                text("UPDATE instance_state SET strategy_state = :s WHERE instance_id = :i"),
                {"s": json.dumps(state), "i": args.instance},
            )
        print("[repair] APPLIED ✓ (last_rb_idx / prev_hedge / last_trade_date 未改动)")
    else:
        print("[repair] DRY-RUN — 未写库。确认无误后加 --apply 才会写回。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
