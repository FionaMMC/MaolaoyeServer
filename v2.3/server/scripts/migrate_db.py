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

import argparse
import sys
from pathlib import Path

# 让脚本能从 /opt/qmt-server/v2.3/server/ 直接跑
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from app.models import Base  # noqa: E402
from app.settings import get_settings  # noqa: E402


def column_exists(con, table: str, col: str) -> bool:
    rows = con.execute(text(f"PRAGMA table_info({table})")).all()
    return any(r[1] == col for r in rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="幂等升级 QMT Pipeline SQLite schema。",
    )
    parser.add_argument(
        "--db-url",
        help="覆盖 QMT_DB_URL；建议演练时显式指向数据库副本",
    )
    parser.add_argument(
        "--skip-stale-cleanup",
        action="store_true",
        help="保留历史 real_A 示例状态，只执行 schema 迁移",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    db_url = args.db_url or settings.db_url
    print(f"[migrate] target={db_url}")
    engine = create_engine(db_url, future=True)
    # 新表（target/rebalance/attempt/cash-flow）由 metadata 幂等创建；旧表新增列
    # 仍由下面的显式 ALTER 完成。
    Base.metadata.create_all(engine)

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

        # 2026-07-02 成交未匹配事故: orders.fetched_at（已拉取护栏判据）
        if not column_exists(con, "orders", "fetched_at"):
            print("[migrate] ALTER TABLE orders ADD fetched_at")
            con.execute(text("ALTER TABLE orders ADD COLUMN fetched_at VARCHAR"))
        else:
            print("[migrate] orders.fetched_at already exists, skip")

        # v53 集成: instance_state.owned_symbols
        if not column_exists(con, "instance_state", "owned_symbols"):
            print("[migrate] ALTER TABLE instance_state ADD owned_symbols")
            con.execute(text(
                "ALTER TABLE instance_state ADD COLUMN owned_symbols JSON"
            ))
        else:
            print("[migrate] instance_state.owned_symbols already exists, skip")

        # Hydra live-readiness: execution_domain 必须贯穿信号→订单→成交→账本。
        for table in ("raw_signals", "orders", "trades", "instance_state", "perf_snapshots"):
            if not column_exists(con, table, "execution_domain"):
                print(f"[migrate] ALTER TABLE {table} ADD execution_domain")
                con.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN execution_domain "
                    "VARCHAR NOT NULL DEFAULT 'paper'"
                ))
            else:
                print(f"[migrate] {table}.execution_domain already exists, skip")

        if not column_exists(con, "instance_state", "account_alias"):
            print("[migrate] ALTER TABLE instance_state ADD account_alias")
            con.execute(text("ALTER TABLE instance_state ADD COLUMN account_alias VARCHAR"))
        else:
            print("[migrate] instance_state.account_alias already exists, skip")

        if not column_exists(con, "orders", "qmt_account_alias"):
            print("[migrate] ALTER TABLE orders ADD qmt_account_alias")
            con.execute(text("ALTER TABLE orders ADD COLUMN qmt_account_alias VARCHAR"))
        else:
            print("[migrate] orders.qmt_account_alias already exists, skip")

        order_columns = {
            "target_id": "VARCHAR",
            "rebalance_id": "VARCHAR",
            "attempt_id": "VARCHAR",
            "attempt_number": "INTEGER",
            "batch_id": "VARCHAR",
            "batch_sha256": "VARCHAR",
            "target_hash": "VARCHAR",
            "execution_reference_price": "FLOAT",
        }
        for col, sql_type in order_columns.items():
            if not column_exists(con, "orders", col):
                print(f"[migrate] ALTER TABLE orders ADD {col}")
                con.execute(text(f"ALTER TABLE orders ADD COLUMN {col} {sql_type}"))
            else:
                print(f"[migrate] orders.{col} already exists, skip")

        attempt_columns = {
            "pretrade_reconciliation_sha256": "VARCHAR",
            "posttrade_reconciliation_sha256": "VARCHAR",
            "reconciled_cash": "FLOAT",
            "reconciled_positions": "JSON",
            "risk_snapshot": "JSON",
        }
        for col, sql_type in attempt_columns.items():
            if not column_exists(con, "hydra_execution_attempts", col):
                print(f"[migrate] ALTER TABLE hydra_execution_attempts ADD {col}")
                con.execute(text(
                    f"ALTER TABLE hydra_execution_attempts ADD COLUMN {col} {sql_type}"
                ))
            else:
                print(f"[migrate] hydra_execution_attempts.{col} already exists, skip")
        con.execute(text(
            "UPDATE hydra_execution_attempts SET risk_snapshot = '{}' "
            "WHERE risk_snapshot IS NULL"
        ))

        if not column_exists(con, "hydra_targets", "research_input_hashes"):
            print("[migrate] ALTER TABLE hydra_targets ADD research_input_hashes")
            con.execute(text(
                "ALTER TABLE hydra_targets ADD COLUMN research_input_hashes JSON"
            ))
            con.execute(text(
                "UPDATE hydra_targets SET research_input_hashes = '{}' "
                "WHERE research_input_hashes IS NULL"
            ))
        else:
            print("[migrate] hydra_targets.research_input_hashes already exists, skip")

        # 清理：删除 strategies.yaml 不再激活的残留 instance_state
        # （v2.3 当前只有 paper_v20h_v20h_v1_3 一个实例）
        if not args.skip_stale_cleanup:
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
                    print(
                        f"[migrate] removed {res.rowcount} stale perf_snapshots: {sid}"
                    )

    print("[migrate] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
