"""持仓对账服务。

Client 推 QMT 真实账户快照（cash + positions），server 比对 instance_state
的虚拟账本，生成 diff 报告。dry_run=False 时把 virtual_positions 强制
对齐到 QMT。

多实例说明（v53 集成后）：
  - virtual_cash 不再由 reconcile 覆写 —— 由 settlement service 维护，
    cash sanity check 改由 reconcile_cash_total() 在 portfolio 级检查。
  - owned_symbols 不为 None 的 instance：只看属于自己的 symbols。
  - owned_symbols=None 的 legacy instance（如 v20h）：看"其他 instance 没认领"的 symbols。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select

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
    """对账数据通过 sanity check 失败。

    保留此异常类以维持向后兼容（其他代码可能 catch 它），
    但 cash 偏离检查已移除（见 reconcile_cash_total）。
    """
    pass


class OwnershipOverlap(Exception):
    """两个 instance 的 owned_symbols 列表有重叠（启动校验失败）。"""


class ReconcileGuardTripped(ReconcileSanityCheckFailed):
    """apply 会清掉过多 server 持仓（疑似快照不完整）→ 拦截，需 force=true 覆盖。

    继承 ReconcileSanityCheckFailed，让 endpoint 现有的 422 处理直接覆盖。
    背景：V53 曾被一份只含 5/10 ETF 的快照 apply，误删 5 个好仓。
    """


# 单标的最大合理持仓（防 QMT 模拟器默认股的 100 亿股污染）。
# 真幽灵仓是 100 亿股（10^10）；与 client query_qmt_positions 的幽灵阈值(>10^7)对齐。
# 注意：阈值必须高于 ETF 量级！ETF 1-2 元/份，1000万建仓单只可达 70万+ 股
# （如红利 512890 ~71万股）。旧值 100K 是按股票(V20H 单股 ~19K)设的，会把
# 合法大额 ETF 仓当幽灵丢掉 → 导致 V53 reconcile 只看到 5/10 个 ETF（事故根因）。
MAX_REASONABLE_QTY_PER_STOCK = 10_000_000

# apply 大批量 close 护栏：当快照缺失大量持仓时，server_only 会被当幽灵清掉。
# 同时满足"绝对数 ≥ MIN"且"占 server 持仓 ≥ FRACTION"才拦截（避免小批量正常清理误伤）。
GUARD_MIN_CLOSE = 3
GUARD_CLOSE_FRACTION = 0.34


class ReconcileService:
    """持仓对账：server virtual vs QMT real。"""

    def __init__(self, session_factory, strategies_file: Path | None = None):
        self.session_factory = session_factory
        self.strategies_file = Path(strategies_file) if strategies_file else None

    def _configured_account_policy(
        self, instance_id: str, supplied_qmt_account_id: str,
    ) -> tuple[str | None, bool]:
        """Return (configured account, whether that account has shared ledger).

        A physical account becomes portfolio-ledger-only as soon as any strategy
        on it uses ``account_isolation: shared_ledger``.  In that topology a QMT
        snapshot cannot identify which overlapping shares belong to which strategy.
        """
        if self.strategies_file is None or not self.strategies_file.exists():
            return None, False
        try:
            cfg = yaml.safe_load(
                self.strategies_file.read_text(encoding="utf-8")
            ) or {}
        except Exception as exc:
            raise ReconcileGuardTripped(
                f"无法读取 strategies.yaml 账户归属，拒绝 apply: {exc}"
            ) from exc

        configured_accounts: dict[str, str] = {}
        shared_accounts: set[str] = set()
        for group in cfg.get("account_groups", []):
            account = group.get("qmt_account_id")
            if not account:
                continue
            account = str(account)
            for strategy in group.get("strategies", []):
                strategy_id = strategy.get("strategy_id")
                if not strategy_id:
                    continue
                configured_accounts[
                    f"{group.get('group_id')}_{strategy_id}"
                ] = account
                if strategy.get("account_isolation") == "shared_ledger":
                    shared_accounts.add(account)

        configured = configured_accounts.get(instance_id)
        if configured is not None and configured != str(supplied_qmt_account_id):
            raise ReconcileGuardTripped(
                f"qmt_account_id 与 strategies.yaml 不一致："
                f"instance={instance_id} configured={configured} "
                f"supplied={supplied_qmt_account_id}"
            )
        effective_account = configured or str(supplied_qmt_account_id)
        return configured, effective_account in shared_accounts

    def reconcile(
        self,
        snapshot: QmtPositionSnapshot,
        initial_cash: float | None = None,
    ) -> ReconcileResult:
        """计算 diff；如果 dry_run=False，把 instance_state 的 virtual_positions 改成 QMT 状态。

        多实例过滤：
          - owned_symbols 非空：只处理白名单内的 symbols。
          - owned_symbols=None（legacy）：排除所有其他 instance 已认领的 symbols。

        注意：virtual_cash 不再由此方法覆写。cash 对齐由 settlement service 负责；
        portfolio 级 cash sanity check 由 reconcile_cash_total() 负责。

        Args:
            initial_cash: 已废弃参数，保留以维持向后兼容，本方法不再使用。
        """
        with self.session_factory() as session:
            inst = session.get(InstanceState, snapshot.instance_id)
            if inst is None:
                raise InstanceNotFound(
                    f"instance_id={snapshot.instance_id} 不存在于 instance_state 表"
                )
            if inst.execution_domain != snapshot.execution_domain:
                raise ReconcileGuardTripped(
                    "snapshot execution_domain 与 instance_state 不一致"
                )
            if snapshot.account_alias is not None and inst.account_alias not in (
                None, snapshot.account_alias,
            ):
                raise ReconcileGuardTripped(
                    "snapshot account_alias 与 instance_state 不一致"
                )

            _, account_has_shared_ledger = self._configured_account_policy(
                snapshot.instance_id, snapshot.qmt_account_id,
            )
            if not snapshot.dry_run and account_has_shared_ledger:
                raise ReconcileGuardTripped(
                    "该 QMT 物理账户包含 shared_ledger 策略，任何实例都禁止用整账户"
                    "快照做单实例 apply（force 也不可绕过）。虚拟账本只能由 "
                    "order_signal_map/settlement 归因维护；请使用 reconcile_total() "
                    "校验账户总量。"
                )

            # 读取本 instance 的 owned_symbols 白名单
            my_owned = inst.owned_symbols  # list | None

            # 如果是 legacy instance（owned_symbols=None），计算其他 instance 已认领的 symbols
            others_owned: set[str] = set()
            if my_owned is None:
                for other in session.execute(select(InstanceState)).scalars().all():
                    if other.instance_id == snapshot.instance_id:
                        continue
                    if other.execution_domain != snapshot.execution_domain:
                        continue
                    if other.owned_symbols:
                        others_owned.update(other.owned_symbols)

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
                # owned_symbols 过滤
                if my_owned is not None:
                    # 白名单模式：只保留属于本 instance 的 symbols
                    if s not in my_owned:
                        continue
                else:
                    # legacy 模式：排除其他 instance 已认领的 symbols
                    if s in others_owned:
                        continue
                qmt_positions[s] = q
            if outliers:
                logger.warning(
                    "reconcile: filtered %d outlier positions (qty > %d, 疑似 QMT 模拟器默认仓): %s",
                    len(outliers), MAX_REASONABLE_QTY_PER_STOCK,
                    [(s, f"{q:,}") for s, q in outliers],
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

            # Dedicated-account strict-rebalance adapters mark a basket as pending
            # until a post-trade QMT snapshot agrees with both positions and cash.
            # A shared-ledger instance is different: the QMT snapshot belongs to
            # the whole physical account, so it must never overwrite or certify one
            # strategy's attributed virtual ledger.
            strategy_state = dict(inst.strategy_state or {})
            reconciliation_status = strategy_state.get("reconciliation_status")
            if reconciliation_status in {"pending", "reconciled", "failed"}:
                cash_tolerance = max(1.0, abs(server_cash) * 0.001)
                consistent = not diffs and abs(cash_diff) <= cash_tolerance
                strategy_state["reconciliation_status"] = (
                    "reconciled" if consistent else "failed"
                )
                strategy_state["last_reconciliation_snapshot_time"] = snapshot.snapshot_time
                strategy_state["last_reconciliation_cash_diff"] = cash_diff
                strategy_state["last_reconciliation_diff_count"] = len(diffs)
                inst.strategy_state = strategy_state
                inst.last_update = _now_iso()
                session.commit()

            if snapshot.dry_run:
                logger.info(
                    "reconcile DRY-RUN: instance=%s cash_diff=%.2f "
                    "matched=%d mismatched=%d server_only=%d qmt_only=%d",
                    snapshot.instance_id, cash_diff,
                    n_matched, n_mismatched, n_server_only, n_qmt_only,
                )
                return result

            if reconciliation_status == "attributed_ledger":
                raise ReconcileGuardTripped(
                    "共享 QMT 账户的实例只能按 order_signal_map/settlement 维护虚拟账本；"
                    "禁止用单实例 QMT 快照覆盖。请使用 reconcile_total() 校验物理账户总量。"
                )

            # 护栏：apply 会把 server_only 持仓清掉。若快照不完整（缺大量持仓），
            # 会误删一批好仓（V53 曾被只含 5/10 ETF 的快照 apply 删掉 5 个）。
            # 绝对数 ≥ MIN 且占 server 持仓 ≥ FRACTION 时拦截，须 force=true 才覆盖。
            n_srv = len(server_positions)
            if (not snapshot.force
                    and n_server_only >= GUARD_MIN_CLOSE
                    and n_srv > 0
                    and n_server_only >= GUARD_CLOSE_FRACTION * n_srv):
                logger.error(
                    "reconcile GUARD: instance=%s 阻止 apply — 会清掉 %d/%d 持仓"
                    "（占比 %.0f%%），疑似快照不完整。确认完整后加 force=true。",
                    snapshot.instance_id, n_server_only, n_srv,
                    100.0 * n_server_only / n_srv,
                )
                raise ReconcileGuardTripped(
                    f"apply 会清掉 {n_server_only}/{n_srv} 个 server 持仓"
                    f"（{100.0 * n_server_only / n_srv:.0f}%），疑似快照不完整，已拦截。"
                    f"确认 QMT 快照完整后用 force=true 覆盖。"
                )

            # 实际 apply：覆盖 virtual_positions（不覆盖 virtual_cash，由 settlement 维护）
            # 注意：直接 assign 一个 dict 才能让 SQLAlchemy 的 mutable JSON 类型识别为 dirty
            inst.virtual_positions = dict(qmt_positions)
            inst.last_update = _now_iso()
            session.commit()

            result.applied = True
            logger.warning(
                "reconcile APPLIED: instance=%s positions %d → %d "
                "(server_only %d closed, qmt_only %d added, mismatched %d adjusted) "
                "[cash NOT overwritten — maintained by settlement]",
                snapshot.instance_id,
                len(server_positions), len(qmt_positions),
                n_server_only, n_qmt_only, n_mismatched,
            )
            return result

    def validate_no_overlap(self) -> None:
        """启动校验：同一 execution domain 内的 owned symbols 不重叠。

        paper 与 live 是隔离账本、隔离账户；两域可有相同标的。legacy
        instance（owned_symbols=None）不参与校验（它消费"剩下的"）。
        如果 raise OwnershipOverlap 应该让 app startup 失败。
        """
        all_owned: dict[tuple[str, str], str] = {}
        with self.session_factory() as session:
            for inst in session.execute(select(InstanceState)).scalars().all():
                if not inst.owned_symbols:
                    continue
                for s in inst.owned_symbols:
                    key = (inst.execution_domain, s)
                    if key in all_owned and all_owned[key] != inst.instance_id:
                        raise OwnershipOverlap(
                            f"symbol {s} owned by both "
                            f"{all_owned[key]} and {inst.instance_id} "
                            f"in {inst.execution_domain} domain"
                        )
                    all_owned[key] = inst.instance_id

    def reconcile_cash_total(
        self,
        qmt_total_cash: float,
        tolerance: float = 0.05,
        execution_domain: str = "paper",
    ) -> bool:
        """检查 Σ(virtual_cash) ≈ QMT total cash。仅报警，不修改 state。

        返回 True = OK，False = 偏差超阈值（已记 warning，调用方决定是否触发 alert）。
        此方法为 portfolio 级 cash sanity check，替代原先 instance 级的 cash 偏离检查。
        Task 23 deploy 会把此方法接入 scheduler 定时检查。
        """
        with self.session_factory() as session:
            instances = session.execute(
                select(InstanceState).where(
                    InstanceState.execution_domain == execution_domain
                )
            ).scalars().all()
            total_virtual = sum(float(inst.virtual_cash) for inst in instances)
            if total_virtual <= 0:
                return True
            deviation = abs(qmt_total_cash - total_virtual) / total_virtual
            if deviation > tolerance:
                logger.warning(
                    "cash_total mismatch: virtual=%.2f qmt=%.2f deviation=%.2f%% (tol=%.2f%%)",
                    total_virtual, qmt_total_cash, deviation * 100, tolerance * 100,
                )
                return False
            return True

    def reconcile_total(
        self,
        qmt_positions: dict[str, int],
        qmt_cash: float,
        snapshot_time: str,
        cash_tolerance: float = 0.05,
        execution_domain: str = "paper",
    ):
        """Portfolio 级总量对账：对每个 symbol 断言 Σ_i virtual_positions[X] == QMT[X]。
        只报警、绝不改账（归属由 settlement 血缘维护）。多个 instance 可共同持有同一
        symbol（如 v53 + v79 都持 511260.SH），本方法只校验其和。"""
        from app.schemas.reconcile import TotalReconcileResult
        with self.session_factory() as session:
            instances = session.execute(
                select(InstanceState).where(
                    InstanceState.execution_domain == execution_domain
                )
            ).scalars().all()
            ledger_sum: dict[str, int] = {}
            per_instance: dict[str, dict[str, int]] = {}
            cash_total = 0.0
            for inst in instances:
                cash_total += float(inst.virtual_cash)
                for s, q in (inst.virtual_positions or {}).items():
                    qi = int(q)
                    if qi <= 0:
                        continue
                    ledger_sum[s] = ledger_sum.get(s, 0) + qi
                    per_instance.setdefault(s, {})[inst.instance_id] = qi
        qmt = {s: int(q) for s, q in qmt_positions.items()
               if int(q) > 0 and int(q) <= MAX_REASONABLE_QTY_PER_STOCK}
        all_syms = set(ledger_sum) | set(qmt)
        matched = 0
        mismatches: list[dict] = []
        for s in sorted(all_syms):
            lg = ledger_sum.get(s, 0)
            qq = qmt.get(s, 0)
            if lg == qq:
                matched += 1
            else:
                mismatches.append({"symbol": s, "qmt": qq, "ledger_sum": lg,
                                   "diff": qq - lg, "per_instance": per_instance.get(s, {})})
        # 现金充足性（不是相等）：QMT 账户现金通常远超各策略预算之和（存在大量未分配
        # 现金），故校验"账户现金能否覆盖各台账占用的现金"，而非要求相等。只有当
        # Σ台账现金 > QMT 现金（策略以为的钱比账户实有还多 → 透支风险）才判 not ok。
        cash_ok = True
        if cash_total > 0:
            shortfall = cash_total - qmt_cash
            cash_ok = shortfall <= cash_total * cash_tolerance
        result = TotalReconcileResult(snapshot_time=snapshot_time, n_symbols=len(all_syms),
            n_matched=matched, n_mismatched=len(mismatches), mismatches=mismatches,
            cash_ok=cash_ok, ledger_cash_total=cash_total, qmt_cash=float(qmt_cash))
        if mismatches or not cash_ok:
            logger.error("reconcile_total MISMATCH: %d/%d symbols off, cash_ok=%s "
                         "(ledger_cash=%.0f qmt_cash=%.0f). mismatches=%s",
                         len(mismatches), len(all_syms), cash_ok,
                         cash_total, qmt_cash, mismatches[:10])
        else:
            logger.info("reconcile_total OK: %d symbols ledger==QMT, cash sufficient "
                        "(ledger_cash=%.0f <= qmt_cash=%.0f)", len(all_syms), cash_total, qmt_cash)
        return result

    def shadow_compare(
        self,
        qmt_positions: dict[str, int],
        qmt_cash: float,
        snapshot_time: str,
        execution_domain: str = "paper",
    ) -> dict:
        """影子对比：跑 reconcile_total，返回 {consistent: bool, total: TotalReconcileResult}。
        consistent = 台账之和逐 symbol == QMT 且 cash 在容差内。切权前每日 log，
        连续 N 天 True 才允许把 total 变权威。"""
        total = self.reconcile_total(
            qmt_positions,
            qmt_cash,
            snapshot_time,
            execution_domain=execution_domain,
        )
        consistent = (total.n_mismatched == 0 and total.cash_ok)
        logger.info("shadow_compare: consistent=%s (mismatched=%d cash_ok=%s)",
                    consistent, total.n_mismatched, total.cash_ok)
        return {"consistent": consistent, "total": total}
