"""live client 独立 SQLite；批次变化绝不自动覆盖。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from live_client.core import ValidatedBatch


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class LiveStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS order_batches (
                    batch_sha256 TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL UNIQUE,
                    trade_date TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    order_id TEXT PRIMARY KEY,
                    batch_sha256 TEXT NOT NULL,
                    local_order_id TEXT,
                    submit_status TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    detail TEXT,
                    execution_json TEXT
                );
                CREATE TABLE IF NOT EXISTS settlement_pushes (
                    trade_date TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    pushed_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, payload_sha256)
                );
            """)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()
            }
            if "execution_json" not in columns:
                conn.execute("ALTER TABLE submissions ADD COLUMN execution_json TEXT")

    def save_batch(self, batch: ValidatedBatch) -> bool:
        payload = json.dumps(batch.as_payload(), sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT batch_sha256, payload_json FROM order_batches WHERE trade_date = ?",
                (batch.trade_date,),
            ).fetchone()
            if row:
                if row != (batch.batch_sha256, payload):
                    raise RuntimeError("服务器批次已变化；live client 拒绝自动替换本地快照")
                return False
            conn.execute(
                "INSERT INTO order_batches VALUES (?, ?, ?, ?, ?, ?)",
                (
                    batch.batch_sha256, batch.batch_id, batch.trade_date,
                    payload, "FETCHED", _now_iso(),
                ),
            )
            conn.commit()
        return True

    def load_batch(self, trade_date: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM order_batches WHERE trade_date = ?",
                (trade_date,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"本地没有 {trade_date} 的已核验订单批次")
        return json.loads(row[0])

    def assert_same_batch(self, batch: ValidatedBatch) -> None:
        local = self.load_batch(batch.trade_date)
        if local["batch_sha256"] != batch.batch_sha256 or local != batch.as_payload():
            raise RuntimeError("下单前服务器批次与本地快照不一致，停止下单")

    def record_submission(
        self, order_id: str, batch_sha256: str, local_order_id: str | None,
        status: str, detail: str | None = None, execution_meta: dict | None = None,
    ) -> None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT local_order_id, submit_status FROM submissions WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if existing:
                raise RuntimeError(f"order_id 已提交/记录，拒绝重复下单: {order_id}")
            conn.execute(
                "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id, batch_sha256, local_order_id, status, _now_iso(), detail,
                    json.dumps(execution_meta or {}, sort_keys=True, separators=(",", ":")),
                ),
            )
            conn.commit()

    def submissions_for_date(self, trade_date: str) -> list[dict]:
        batch = self.load_batch(trade_date)
        order_by_id = {order["order_id"]: order for order in batch["orders"]}
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM submissions WHERE batch_sha256 = ?",
                (batch["batch_sha256"],),
            ).fetchall()
        result = []
        for row in rows:
            record = {**order_by_id[row["order_id"]], **dict(row)}
            record["execution_meta"] = json.loads(record.pop("execution_json") or "{}")
            result.append(record)
        return result
