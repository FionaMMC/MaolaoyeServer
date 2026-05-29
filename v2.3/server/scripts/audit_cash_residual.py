"""精算 5/12 重复结算给虚拟账本注入的现金残差（纯只读诊断，绝不写库）。

settlement 旧代码无幂等守卫 → 5/12 同一批回报被应用了两次。每多应用一次：
  BUY  多扣 (gross + 佣金)        → 现金偏低，修正 = +(gross + fee)
  SELL 多进 (gross − 佣金 − 印花)  → 现金偏高，修正 = −(gross − fee)

线上 bookkeeping_divergence 计数为 0，说明所有重复应用当时余量充足、全部落账
（无 防穿仓 跳过），因此每个"多余副本"都精确贡献一份上述残差。

本脚本完全复刻 settlement 的拆单(largest_remainder_split) + 费用模型，对每个
重复 fill 的多余副本逐 signal 复算，按 instance 汇总精确残差。只读，不修改任何
数据；最终现金对账仍建议配合客户端推 QMT 真实账户快照做 positions+cash 联合
reconcile，本脚本只量化"已知注入误差"这一项。

用法：cd /opt/qmt-server/v2.3/server && venv/bin/python -m scripts.audit_cash_residual
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.settlement import largest_remainder_split  # noqa: E402
from app.settings import get_settings  # noqa: E402

# 与 app/settings.py 默认一致
COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
STAMP_DUTY_SELL = 0.0005


def _fees(gross: float, direction: str) -> float:
    commission = max(MIN_COMMISSION, gross * COMMISSION_RATE)
    duty = gross * STAMP_DUTY_SELL if direction == "SELL" else 0.0
    return commission + duty


def main() -> int:
    engine = create_engine(get_settings().db_url, future=True)
    per_instance: dict[str, float] = defaultdict(float)
    n_groups = 0
    n_extra = 0
    n_missing_map = 0

    with engine.connect() as con:
        dup_groups = con.execute(text(
            "SELECT order_id, filled_time, filled_quantity, filled_price, COUNT(*) n "
            "FROM trades "
            "GROUP BY order_id, filled_time, filled_quantity, filled_price "
            "HAVING n > 1 AND filled_quantity > 0"
        )).all()

        for order_id, _ftime, fqty, fprice, n in dup_groups:
            n_groups += 1
            extra = n - 1
            n_extra += extra

            order = con.execute(text(
                "SELECT direction, symbol FROM orders WHERE order_id = :o"
            ), {"o": order_id}).first()
            if not order:
                continue
            direction = order[0]

            maps = con.execute(text(
                "SELECT signal_id, signal_quantity FROM order_signal_map WHERE order_id = :o"
            ), {"o": order_id}).all()
            if not maps:
                n_missing_map += 1
                continue

            weights = [int(m[1]) for m in maps]
            splits = largest_remainder_split(int(fqty), weights)
            for (sid, _w), sq in zip(maps, splits):
                if sq == 0:
                    continue
                inst = con.execute(text(
                    "SELECT instance_id FROM raw_signals WHERE signal_id = :s"
                ), {"s": sid}).first()
                if not inst:
                    continue
                gross = float(fprice) * sq
                fee = _fees(gross, direction)
                if direction == "BUY":
                    correction = +(gross + fee)   # 多扣 → 加回
                else:
                    correction = -(gross - fee)   # 多进 → 减掉
                per_instance[inst[0]] += correction * extra

        print("=== 5/12 重复结算现金残差精算（只读）===")
        print(f"重复 fill 组: {n_groups}  多余副本总数: {n_extra}  "
              f"无 order_signal_map（已被 clear-state 清，未计入）: {n_missing_map}")
        if not per_instance:
            print("  未发现可归集的现金残差。")
        for inst_id, corr in sorted(per_instance.items()):
            cur = con.execute(text(
                "SELECT virtual_cash FROM instance_state WHERE instance_id = :i"
            ), {"i": inst_id}).first()
            cur_cash = float(cur[0]) if cur else float("nan")
            print(f"  {inst_id}:")
            print(f"     当前 virtual_cash = {cur_cash:,.2f}")
            print(f"     建议修正        = {corr:+,.2f}")
            print(f"     修正后          = {cur_cash + corr:,.2f}")

    print("\n注：以上为 5/12 已知双重应用的【精确】残差。最终现金口径仍建议用客户端")
    print("    推 QMT 真实账户快照做 positions+cash 联合 reconcile 兜底。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
