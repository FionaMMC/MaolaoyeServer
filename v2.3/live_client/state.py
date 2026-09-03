"""live client 独立 SQLite；批次变化绝不自动覆盖。"""
from __future__ import annotations

import hashlib
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
                    execution_json TEXT,
                    order_remark TEXT
                );
                CREATE TABLE IF NOT EXISTS settlement_pushes (
                    trade_date TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    pushed_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, payload_sha256)
                );
                CREATE TABLE IF NOT EXISTS risk_checks (
                    batch_sha256 TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preflight_checks (
                    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_sha256 TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_preflight_batch
                    ON preflight_checks(batch_sha256, check_id);
            """)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()
            }
            if "execution_json" not in columns:
                conn.execute("ALTER TABLE submissions ADD COLUMN execution_json TEXT")
            if "order_remark" not in columns:
                conn.execute("ALTER TABLE submissions ADD COLUMN order_remark TEXT")

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

    def record_preflight(self, batch_sha256: str, payload: dict) -> str:
        if payload.get("batch_sha256") != batch_sha256:
            raise RuntimeError("preflight 回执与本地冻结批次不一致")
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_sha256 = hashlib.sha256(body.encode()).hexdigest()
        with self._connect() as conn:
            batch = conn.execute(
                "SELECT 1 FROM order_batches WHERE batch_sha256 = ?",
                (batch_sha256,),
            ).fetchone()
            if batch is None:
                raise RuntimeError("preflight 对应的本地冻结批次不存在")
            conn.execute(
                """INSERT INTO preflight_checks (
                    batch_sha256, payload_sha256, payload_json, checked_at
                ) VALUES (?, ?, ?, ?)""",
                (batch_sha256, payload_sha256, body, _now_iso()),
            )
            conn.commit()
        return payload_sha256

    def latest_preflight(self, batch_sha256: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT payload_json, payload_sha256, checked_at
                   FROM preflight_checks
                   WHERE batch_sha256 = ?
                   ORDER BY check_id DESC LIMIT 1""",
                (batch_sha256,),
            ).fetchone()
        if row is None:
            return None
        actual_sha256 = hashlib.sha256(row[0].encode()).hexdigest()
        if actual_sha256 != row[1]:
            raise RuntimeError("本地 preflight 回执 hash 校验失败")
        return {
            "payload": json.loads(row[0]),
            "payload_sha256": row[1],
            "checked_at": row[2],
        }

    def record_risk_check(self, batch_sha256: str, payload: dict) -> bool:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM risk_checks WHERE batch_sha256 = ?",
                (batch_sha256,),
            ).fetchone()
            if existing:
                if existing[0] != body:
                    raise RuntimeError("同一批次的 QMT 风控快照已变化，拒绝下单")
                return False
            conn.execute(
                "INSERT INTO risk_checks VALUES (?, ?, ?)",
                (batch_sha256, body, _now_iso()),
            )
            conn.commit()
        return True

    def risk_check(self, batch_sha256: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM risk_checks WHERE batch_sha256 = ?",
                (batch_sha256,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def prepare_submission(
        self, order_id: str, batch_sha256: str, order_remark: str,
    ) -> dict:
        """Durably record intent before any broker call.

        PREPARED proves that QMT has not been called by this state machine yet.
        The later SUBMITTING_UNKNOWN state deliberately means the opposite: a
        crash may have happened after broker acceptance and automatic replay is
        forbidden until the deterministic remark is found at QMT.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM submissions WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if existing:
                row = dict(existing)
                if (
                    row["batch_sha256"] != batch_sha256
                    or row.get("order_remark") not in (None, order_remark)
                ):
                    raise RuntimeError(f"order_id 本地提交意图与批次不一致: {order_id}")
                return row
            conn.execute(
                """INSERT INTO submissions (
                    order_id, batch_sha256, local_order_id, submit_status,
                    submitted_at, detail, execution_json, order_remark
                ) VALUES (?, ?, NULL, 'PREPARED', ?, ?, '{}', ?)""",
                (
                    order_id, batch_sha256, _now_iso(),
                    "durable intent recorded before QMT call", order_remark,
                ),
            )
            conn.commit()
        return self.submission(order_id)

    def claim_submission(self, order_id: str) -> bool:
        """Move PREPARED to the crash-ambiguous state exactly once."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """UPDATE submissions
                   SET submit_status = 'SUBMITTING_UNKNOWN', submitted_at = ?
                   WHERE order_id = ? AND submit_status = 'PREPARED'""",
                (_now_iso(), order_id),
            ).rowcount
            conn.commit()
        return changed == 1

    def complete_submission(
        self, order_id: str, local_order_id: str | None, status: str,
        detail: str | None = None, execution_meta: dict | None = None,
    ) -> bool:
        if status not in {"SUBMITTED", "REJECTED"}:
            raise ValueError(f"非法本地提交终态: {status}")
        body = json.dumps(
            execution_meta or {}, sort_keys=True, separators=(",", ":"),
        )
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM submissions WHERE order_id = ?", (order_id,),
            ).fetchone()
            if existing is None:
                raise RuntimeError(f"order_id 尚无 durable intent: {order_id}")
            row = dict(existing)
            if row["submit_status"] in {"SUBMITTED", "REJECTED"}:
                same = (
                    row["submit_status"] == status
                    and row["local_order_id"] == local_order_id
                )
                if not same:
                    raise RuntimeError(f"order_id 已有不同提交终态: {order_id}")
                return False
            if row["submit_status"] not in {"PREPARED", "SUBMITTING_UNKNOWN"}:
                raise RuntimeError(
                    f"order_id 本地提交状态不可完成: {order_id} {row['submit_status']}"
                )
            conn.execute(
                """UPDATE submissions
                   SET local_order_id = ?, submit_status = ?, submitted_at = ?,
                       detail = ?, execution_json = ?
                   WHERE order_id = ?""",
                (local_order_id, status, _now_iso(), detail, body, order_id),
            )
            conn.commit()
        return True

    def submission(self, order_id: str) -> dict:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM submissions WHERE order_id = ?", (order_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"本地没有 order_id 提交状态: {order_id}")
        return dict(row)

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
