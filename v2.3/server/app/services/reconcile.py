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


class ReconcileSanityCheckFailed(Exception):
    """对账数据通过 sanity check 失败（如 cash 偏离合理范围太远）。

    防止把 QMT 真账户「非 sandbox 部分」（如模拟盘自带的几亿股默认股 + 几亿元杂项资金）
    误同步进 V20H 虚拟账本，把 NAV 炸到天上。
    """
    pass


# 单股最大合理持仓（防 QMT 模拟器默认股的 100 亿股污染）
# V20H 单股理论上限：¥10M NAV × 1.5×cap / 800持仓 / 1元价 ≈ 19K 股
MAX_REASONABLE_QTY_PER_STOCK = 100_000

# Cash 容忍偏离倍数：reconcile 传入的 cash 不能超过 initial_cash 的 5 倍
# 例如 initial_cash=10M 时，cash 必须在 [-40M, +50M] 范围内才接受
# 5/21 事件中 reconcile 试图把 cash 从 930K 改成 188M（18.8× 偏离）被这个保护拦下
MAX_CASH_DEVIATION_MULTIPLE = 5.0


class ReconcileService:
    """持仓对账：server virtual vs QMT real。"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def reconcile(
        self,
        snapshot: QmtPositionSnapshot,
        initial_cash: float | None = None,
    ) -> ReconcileResult:
        """计算 diff；如果 dry_run=False，把 instance_state 改成 QMT 状态。

        Sanity checks（apply 模式下）：
          1. 单股持仓 > MAX_REASONABLE_QTY_PER_STOCK 的 reject（防模拟器默认股）
          2. cash 偏离 initial_cash 超过 MAX_CASH_DEVIATION_MULTIPLE 倍 reject

        Args:
            initial_cash: 实例的 virtual_initial_cash（从 strategies.yaml）。
                          None 时用 server 当前 virtual_cash 作为基准比较。
        """
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

            # Filter QMT positions: drop outliers + warn
            qmt_positions_raw = {
                s: int(q) for s, q in snapshot.qmt_positions.items()
                if int(q) > 0
            }
            qmt_positions: dict[str, int] = {}
            outliers: list[tuple[str, int]] = []
            for s, q in qmt_positions_raw.items():
                if q > MAX_REASONABLE_QTY_PER_STOCK:
                    outliers.append((s, q))
                    continue
                qmt_positions[s] = q
            if outliers:
                logger.warning(
                    "reconcile: filtered %d outlier positions (qty > %d, 疑似 QMT 模拟器默认仓): %s",
                    len(outliers), MAX_REASONABLE_QTY_PER_STOCK,
                    [(s, f"{q:,}") for s, q in outliers],
                )

            # Sanity check: cash 偏离不能太离谱（仅 apply 模式）
            if not snapshot.dry_run:
                baseline = initial_cash if initial_cash is not None else server_cash
                if baseline > 0:
                    deviation = abs(snapshot.qmt_cash - baseline) / baseline
                    if deviation > MAX_CASH_DEVIATION_MULTIPLE:
                        raise ReconcileSanityCheckFailed(
                            f"cash 偏离过大: qmt_cash=¥{snapshot.qmt_cash:,.2f} "
                            f"vs baseline=¥{baseline:,.2f} ({deviation:.1f}× 偏离, "
                            f"上限 {MAX_CASH_DEVIATION_MULTIPLE}×)。\n"
                            f"原因可能是：QMT 账户里有非 V20H 的资金/持仓被一起拉进来。\n"
                            f"建议：检查 QMT 账户是否被 V20H 独占，或修 client 端"
                            f"query_qmt_positions.py 加 cash 过滤。"
                        )

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
