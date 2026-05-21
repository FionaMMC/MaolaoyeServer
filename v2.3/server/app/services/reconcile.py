"""持仓对账服务。

Client 推 QMT 真实账户快照（cash + positions），server 比对 instance_state
的虚拟账本，生成 diff 报告。dry_run=False 时把 virtual_cash/positions 强制
对齐到 QMT。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.models import InstanceState
from app.schemas.reconcile import (
    PositionDiff,
    QmtPositionSnapshot,
    ReconcileResult,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class InstanceNotFound(Exception):
    pass


class ReconcileService:
    """持仓对账：server virtual vs QMT real。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def reconcile(self, snapshot: QmtPositionSnapshot) -> ReconcileResult:
        """计算 diff；如果 dry_run=False，把 instance_state 改成 QMT 状态。"""
        with self.session_factory() as session:
            inst = session.get(InstanceState, snapshot.instance_id)
            if inst is None:
                raise InstanceNotFound(
                    f"instance_id={snapshot.instance_id} 不存在于 instance_state 表"
                )

            server_cash = float(inst.virtual_cash)
            server_positions = {
                s: int(q) for s, q in (inst.virtual_positions or {}).items()
                if int(q) > 0  # 防御性：忽略 qty=0 的脏数据
            }
            qmt_positions = {
                s: int(q) for s, q in snapshot.qmt_positions.items()
                if int(q) > 0
            }

            # 计算 diffs
            all_symbols = set(server_positions) | set(qmt_positions)
            diffs: list[PositionDiff] = []
            n_matched = 0
            n_mismatched = 0
            n_server_only = 0
            n_qmt_only = 0

            for sym in sorted(all_symbols):
                sq = server_positions.get(sym, 0)
                qq = qmt_positions.get(sym, 0)
                if sq == qq:
                    n_matched += 1
                    continue
                diff = PositionDiff(
                    symbol=sym, server_qty=sq, qmt_qty=qq, diff=qq - sq,
                )
                diffs.append(diff)
                if sq > 0 and qq == 0:
                    n_server_only += 1
                elif sq == 0 and qq > 0:
                    n_qmt_only += 1
                else:
                    n_mismatched += 1

            cash_diff = float(snapshot.qmt_cash) - server_cash

            result = ReconcileResult(
                instance_id=snapshot.instance_id,
                snapshot_time=snapshot.snapshot_time,
                dry_run=snapshot.dry_run,
                applied=False,
                server_cash=server_cash,
                qmt_cash=float(snapshot.qmt_cash),
                cash_diff=cash_diff,
                n_server_positions=len(server_positions),
                n_qmt_positions=len(qmt_positions),
                n_matched=n_matched,
                n_mismatched=n_mismatched,
                n_server_only=n_server_only,
                n_qmt_only=n_qmt_only,
                diffs=diffs if snapshot.dry_run else [],
            )

            if snapshot.dry_run:
                logger.info(
                    "reconcile DRY-RUN: instance=%s cash_diff=%.2f "
                    "matched=%d mismatched=%d server_only=%d qmt_only=%d",
                    snapshot.instance_id, cash_diff,
                    n_matched, n_mismatched, n_server_only, n_qmt_only,
                )
                return result

            # 实际 apply：覆盖 instance_state
            # 注意：直接 assign 一个 dict 才能让 SQLAlchemy 的 mutable JSON 类型识别为 dirty
            inst.virtual_cash = float(snapshot.qmt_cash)
            inst.virtual_positions = dict(qmt_positions)
            inst.last_update = _now_iso()
            session.commit()

            result.applied = True
            logger.warning(
                "reconcile APPLIED: instance=%s cash %.2f → %.2f, positions %d → %d "
                "(server_only %d closed, qmt_only %d added, mismatched %d adjusted)",
                snapshot.instance_id,
                server_cash, snapshot.qmt_cash,
                len(server_positions), len(qmt_positions),
                n_server_only, n_qmt_only, n_mismatched,
            )
            return result
