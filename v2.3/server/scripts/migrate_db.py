"""DB schema migration: 把新增列加到老 SQLite 数据库上。

幂等：每个 ALTER 都先检查列是否已存在，多次跑无副作用。

新增列：
  - orders.bookkeeping_divergence  BOOLEAN DEFAULT 0  (Bug C)
  - instance_state.strategy_state  JSON NULL          (Bug D)

清理：
  - 删除残留的 real_A_momentum / real_A_buy_on_dip_example instance_state
    （strategies.yaml 已经移除了 real_A，残留 instance_state 会被 perf 服务
    误读为活跃实例，每天写 0% NAV snapshot）

使用：
    cd /opt/qmt-server/v2.3/server && venv/bin/python -m scripts.migrate_db
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能从 /opt/qmt-server/v2.3/server/ 直接跑
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from app.settings import get_settings  # noqa: E402


def column_exists(con, table: str, col: str) -> bool:
    rows = con.execute(text(f"PRAGMA table_info({table})")).all()
    return any(r[1] == col for r in rows)


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.db_url, future=True)

    with engine.begin() as con:
        # Bug C: orders.bookkeeping_divergence
        if not column_exists(con, "orders", "bookkeeping_divergence"):
            print("[migrate] ALTER TABLE orders ADD bookkeeping_divergence")
            con.execute(text(
                "ALTER TABLE orders ADD COLUMN bookkeeping_divergence "
                "BOOLEAN NOT NULL DEFAULT 0"
            ))
        else:
            print("[migrate] orders.bookkeeping_divergence already exists, skip")

        # Bug D: instance_state.strategy_state
        if not column_exists(con, "instance_state", "strategy_state"):
            print("[migrate] ALTER TABLE instance_state ADD strategy_state")
            con.execute(text(
                "ALTER TABLE instance_state ADD COLUMN strategy_state JSON"
            ))
        else:
            print("[migrate] instance_state.strategy_state already exists, skip")

        # v53 集成: instance_state.owned_symbols
        if not column_exists(con, "instance_state", "owned_symbols"):
            print("[migrate] ALTER TABLE instance_state ADD owned_symbols")
            con.execute(text(
                "ALTER TABLE instance_state ADD COLUMN owned_symbols JSON"
            ))
        else:
            print("[migrate] instance_state.owned_symbols already exists, skip")

        # 清理：删除 strategies.yaml 不再激活的残留 instance_state
        # （v2.3 当前只有 paper_v20h_v20h_v1_3 一个实例）
        stale_ids = [
            "real_A_momentum",
            "real_A_buy_on_dip_example",
        ]
        for sid in stale_ids:
            res = con.execute(
                text("DELETE FROM instance_state WHERE instance_id = :id"),
                {"id": sid},
            )
            if res.rowcount > 0:
                print(f"[migrate] removed stale instance_state: {sid}")

        # 同时删除这些残留的 perf_snapshots（避免 dashboard 显示废数据）
        for sid in stale_ids:
            res = con.execute(
                text("DELETE FROM perf_snapshots WHERE instance_id = :id"),
                {"id": sid},
            )
            if res.rowcount > 0:
                print(f"[migrate] removed {res.rowcount} stale perf_snapshots: {sid}")

    print("[migrate] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
