"""Local OMS journal for the Windows agent (oms-agent.db).

Three durable things live here, each written in its own BEGIN IMMEDIATE
transaction on a fresh connection:

* the plan cache: the signed session plan, immutable once cached (only the
  server's ``executable`` answer is refreshed);
* order intents: the agent's own record of what it did with a planned order,
  written *before* the broker call so a crash can never cause a resubmission;
* the outbox: one event per intent change, kept until the server
  acknowledges it.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

INTENT_STATES = ("SUBMITTING", "ACKED", "REJECTED", "UNKNOWN")
EVENT_KIND = {
    "SUBMITTING": "SUBMIT_STARTED",
    "ACKED": "ACKED",
    "REJECTED": "SUBMIT_REJECTED",
    "UNKNOWN": "SUBMIT_UNKNOWN",
}
# SUBMITTING is the one-time claim that precedes the broker call; it can only
# be taken by an order that has no intent yet.  ACKED and REJECTED are final
# locally.  UNKNOWN is resolved only by broker evidence (found by remark).
_NEXT = {
    None: frozenset({"SUBMITTING", "ACKED"}),
    "SUBMITTING": frozenset({"ACKED", "REJECTED", "UNKNOWN"}),
    "UNKNOWN": frozenset({"ACKED", "REJECTED"}),
    "ACKED": frozenset(),
    "REJECTED": frozenset(),
}
_PLAN_KEYS = ("plan_sha256", "session_id", "cycle_id", "trade_date", "phase", "orders")
_ORDER_KEYS = ("client_order_id", "symbol", "side", "quantity", "limit_price")


class JournalConflict(RuntimeError):
    """The journal refused a write that would rewrite durable history."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validated_plan(plan: dict) -> dict:
    missing = [key for key in _PLAN_KEYS if key not in plan]
    if missing:
        raise ValueError(f"计划缺少字段: {missing}")
    if not re.fullmatch(r"\d{8}", str(plan["trade_date"])):
        raise ValueError(f"计划 trade_date 非法: {plan['trade_date']}")
    if plan["phase"] not in ("SELL", "BUY", "MANUAL"):
        raise ValueError(f"计划 phase 非法: {plan['phase']}")
    if not str(plan["plan_sha256"]) or not str(plan["session_id"]):
        raise ValueError("计划缺少 plan_sha256 或 session_id")
    if not isinstance(plan["orders"], list):
        raise ValueError("计划 orders 必须是列表")
    seen = set()
    for order in plan["orders"]:
        missing = [key for key in _ORDER_KEYS if key not in order]
        if missing:
            raise ValueError(f"计划订单缺少字段: {missing}")
        if order["side"] not in ("BUY", "SELL"):
            raise ValueError(f"计划订单方向非法: {order['side']}")
        if order["client_order_id"] in seen:
            raise ValueError(f"计划内订单号重复: {order['client_order_id']}")
        seen.add(order["client_order_id"])
    return json.loads(_canonical(plan))


def _frozen_part(plan: dict) -> str:
    """Everything the server signs or identifies; ``executable`` is status."""
    return _canonical({key: value for key, value in plan.items() if key != "executable"})


class OmsJournal:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc).astimezone())
        self._init()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One transaction per call; the handle is always closed (Windows)."""
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _now(self) -> str:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("OMS journal clock must carry a timezone")
        return now.isoformat()

    def _init(self) -> None:
        # executescript manages its own transaction, so it runs outside _tx.
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_sha256 TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL UNIQUE,
                    trade_date TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    frozen_json TEXT NOT NULL,
                    executable INTEGER NOT NULL,
                    cached_at TEXT NOT NULL,
                    refreshed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_plans_day ON plans(trade_date, phase);
                CREATE TABLE IF NOT EXISTS plan_orders (
                    client_order_id TEXT PRIMARY KEY,
                    plan_sha256 TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intents (
                    client_order_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    broker_order_id TEXT,
                    detail TEXT,
                    last_event_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    client_order_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    broker_order_id TEXT,
                    detail TEXT,
                    acked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox(acked_at, seq);
            """)
        finally:
            conn.close()

    def cache_plan(self, plan: dict) -> None:
        """Cache a server plan. Keyed by plan_sha256; never rewritten.

        A second plan for the same session, or the same hash with different
        content, raises JournalConflict. Re-caching the same plan only
        refreshes ``executable`` (e.g. fetched before and after approval).
        """
        plan = _validated_plan(plan)
        frozen = _frozen_part(plan)
        executable = 1 if plan.get("executable") is True else 0
        now = self._now()
        with self._tx() as conn:
            same_hash = conn.execute(
                "SELECT session_id, frozen_json FROM plans WHERE plan_sha256 = ?",
                (plan["plan_sha256"],),
            ).fetchone()
            if same_hash is not None:
                if same_hash["frozen_json"] != frozen:
                    raise JournalConflict(
                        f"plan_sha256 {plan['plan_sha256']} 已缓存不同内容，拒绝覆盖"
                    )
                conn.execute(
                    "UPDATE plans SET executable = ?, refreshed_at = ? WHERE plan_sha256 = ?",
                    (executable, now, plan["plan_sha256"]),
                )
                return
            same_session = conn.execute(
                "SELECT plan_sha256 FROM plans WHERE session_id = ?", (plan["session_id"],),
            ).fetchone()
            if same_session is not None:
                raise JournalConflict(
                    f"session_id {plan['session_id']} 已缓存另一份计划 "
                    f"{same_session['plan_sha256']}，拒绝覆盖"
                )
            for order in plan["orders"]:
                owner = conn.execute(
                    "SELECT plan_sha256 FROM plan_orders WHERE client_order_id = ?",
                    (order["client_order_id"],),
                ).fetchone()
                if owner is not None:
                    raise JournalConflict(
                        f"订单号 {order['client_order_id']} 已属于计划 {owner['plan_sha256']}"
                    )
            conn.execute(
                "INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (plan["plan_sha256"], plan["session_id"], plan["trade_date"], plan["phase"],
                 frozen, executable, now, now),
            )
            conn.executemany(
                "INSERT INTO plan_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(o["client_order_id"], plan["plan_sha256"], plan["session_id"], plan["trade_date"],
                  plan["phase"], o["symbol"], o["side"], int(o["quantity"]), float(o["limit_price"]))
                 for o in plan["orders"]],
            )

    def plan(self, trade_date: str, phase: str) -> dict | None:
        """Most recently cached plan for the day and phase, with the latest
        ``executable`` answer seen from the server."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT frozen_json, executable FROM plans WHERE trade_date = ? AND phase = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (trade_date, phase),
            ).fetchone()
        if row is None:
            return None
        return {**json.loads(row["frozen_json"]), "executable": bool(row["executable"])}

    @staticmethod
    def _intent_row(conn: sqlite3.Connection, client_order_id: str) -> dict | None:
        row = conn.execute(
            """SELECT o.client_order_id, o.plan_sha256, o.session_id, o.trade_date, o.phase,
                      o.symbol, o.side, o.quantity, o.limit_price,
                      i.state, i.broker_order_id, i.detail, i.last_event_id,
                      i.created_at, i.updated_at
               FROM intents i JOIN plan_orders o USING (client_order_id)
               WHERE i.client_order_id = ?""",
            (client_order_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def intent(self, client_order_id: str) -> dict | None:
        with self._tx() as conn:
            return self._intent_row(conn, client_order_id)

    def mark(self, client_order_id: str, state: str, broker_order_id: str | None,
             detail: str | None) -> dict:
        """Record an intent change and its outbox event atomically.

        SUBMITTING must be marked before the broker call and can be taken
        only once per order, so a restarted agent can never submit twice.
        Re-marking the current ACKED/REJECTED/UNKNOWN state with the same
        broker id is a no-op without a new event.
        """
        if state not in INTENT_STATES:
            raise ValueError(f"非法意图状态: {state}")
        if state == "ACKED" and not broker_order_id:
            raise ValueError("ACKED 必须带券商委托号")
        broker_order_id = str(broker_order_id) if broker_order_id is not None else None
        now = self._now()
        with self._tx() as conn:
            planned = conn.execute(
                "SELECT 1 FROM plan_orders WHERE client_order_id = ?", (client_order_id,),
            ).fetchone()
            if planned is None:
                raise KeyError(f"订单号不在任何已缓存计划中: {client_order_id}")
            current = self._intent_row(conn, client_order_id)
            current_state = current["state"] if current else None
            if current is not None and state == current_state and state != "SUBMITTING":
                if broker_order_id not in (None, current["broker_order_id"]):
                    raise JournalConflict(
                        f"{client_order_id} 已记录券商委托号 {current['broker_order_id']}"
                    )
                return current
            if state not in _NEXT[current_state]:
                raise JournalConflict(f"{client_order_id}: {current_state} -> {state} 不允许")
            event_id = uuid.uuid4().hex
            if current is None:
                conn.execute(
                    "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (client_order_id, state, broker_order_id, detail, event_id, now, now),
                )
            else:
                conn.execute(
                    """UPDATE intents SET state = ?, broker_order_id = COALESCE(?, broker_order_id),
                       detail = ?, last_event_id = ?, updated_at = ? WHERE client_order_id = ?""",
                    (state, broker_order_id, detail, event_id, now, client_order_id),
                )
            conn.execute(
                """INSERT INTO outbox (event_id, client_order_id, kind, observed_at,
                   broker_order_id, detail, acked_at) VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (event_id, client_order_id, EVENT_KIND[state], now, broker_order_id, detail),
            )
            return self._intent_row(conn, client_order_id)

    def outbox(self, limit: int = 500) -> list[dict]:
        """Unacknowledged events, oldest first, in the server's EventIn shape."""
        with self._tx() as conn:
            rows = conn.execute(
                """SELECT event_id, client_order_id, kind, observed_at, broker_order_id, detail
                   FROM outbox WHERE acked_at IS NULL ORDER BY seq LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge(self, event_ids: list[str]) -> None:
        """Mark events as accepted by the server; rows are kept for audit."""
        now = self._now()
        with self._tx() as conn:
            conn.executemany(
                "UPDATE outbox SET acked_at = ? WHERE event_id = ? AND acked_at IS NULL",
                [(now, event_id) for event_id in event_ids],
            )

    def hold_flag(self) -> bool:
        """Local emergency switch: a file named HOLD next to the journal."""
        return (self.path.parent / "HOLD").exists()
