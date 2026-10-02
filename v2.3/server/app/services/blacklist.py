"""黑名单服务：自动从 REJECTED orders + 手工维护的持久黑名单。

实盘里 QMT 可能因为 ST/退市/未签协议/涨跌停 等原因拒单。两种来源：

1. **自动派生**：扫过去 N 天 orders 表里 status='REJECTED' 的 symbol（短期记忆）
2. **手工维护**：risk_blacklist 表里手工添加的 symbol（持久记忆，clear-state 不会清）

compute() 返回两者的并集，传给策略 ctx 做过滤。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Order, RiskBlacklistEntry

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class BlacklistService:
    """从 orders 表 + risk_blacklist 表合并 symbols。"""

    DEFAULT_LOOKBACK_DAYS = 30
    DEFAULT_MIN_REJECTIONS = 1

    def __init__(self, session_factory):
        self.session_factory = session_factory

    # ── 计算 ────────────────────────────────────────────────────────────
    def compute(
        self,
        lookback_days: int | None = None,
        min_rejections: int | None = None,
        execution_domain: str = "paper",
    ) -> set[str]:
        """返回（自动派生 ∪ 手工维护）的并集。"""
        return self._compute_auto(
            lookback_days, min_rejections, execution_domain,
        ) | self._load_manual()

    def _compute_auto(
        self, lookback_days, min_rejections, execution_domain: str = "paper",
    ) -> set[str]:
        lookback = lookback_days if lookback_days is not None else self.DEFAULT_LOOKBACK_DAYS
        min_rej = min_rejections if min_rejections is not None else self.DEFAULT_MIN_REJECTIONS
        cutoff = (datetime.now() - timedelta(days=lookback)).strftime("%Y%m%d")

        with self.session_factory() as session:
            stmt = (
                select(Order.symbol)
                .where(Order.execution_domain == execution_domain)
                .where(Order.valid_date >= cutoff)
                .where(Order.status == "REJECTED")
            )
            rows = session.execute(stmt).all()

        counts = Counter(r[0] for r in rows)
        return {sym for sym, cnt in counts.items() if cnt >= min_rej}

    def _load_manual(self) -> set[str]:
        with self.session_factory() as session:
            rows = session.execute(select(RiskBlacklistEntry.symbol)).all()
        return {r[0] for r in rows}

    def stats(self, lookback_days: int | None = None) -> dict:
        """诊断：自动派生的频次 + 手工维护的列表。"""
        lookback = lookback_days if lookback_days is not None else self.DEFAULT_LOOKBACK_DAYS
        cutoff = (datetime.now() - timedelta(days=lookback)).strftime("%Y%m%d")

        with self.session_factory() as session:
            rows = session.execute(
                select(Order.symbol, Order.valid_date)
                .where(Order.valid_date >= cutoff)
                .where(Order.status == "REJECTED")
            ).all()
            manual_rows = session.execute(
                select(RiskBlacklistEntry.symbol, RiskBlacklistEntry.reason,
                       RiskBlacklistEntry.added_at)
            ).all()

        auto_counts: dict[str, int] = {}
        for sym, _ in rows:
            auto_counts[sym] = auto_counts.get(sym, 0) + 1

        manual_list = [
            {"symbol": s, "reason": r, "added_at": a}
            for s, r, a in manual_rows
        ]
        return {
            "lookback_days": lookback,
            "cutoff_date": cutoff,
            "auto_rejected_total": len(rows),
            "auto_unique_symbols": len(auto_counts),
            "auto_by_symbol": dict(sorted(auto_counts.items(), key=lambda kv: -kv[1])),
            "manual_entries": manual_list,
            "manual_count": len(manual_list),
            "merged_total": len(set(auto_counts) | {r["symbol"] for r in manual_list}),
        }

    # ── 手工维护 CRUD ───────────────────────────────────────────────────
    def add_manual(self, symbols: list[str], reason: str = "") -> dict:
        """批量加入持久黑名单（symbols 已存在则更新 reason）。"""
        added = 0
        updated = 0
        with self.session_factory() as session:
            for sym in symbols:
                existing = session.get(RiskBlacklistEntry, sym)
                if existing:
                    existing.reason = reason
                    updated += 1
                else:
                    session.add(RiskBlacklistEntry(
                        symbol=sym, reason=reason, added_at=_now(),
                    ))
                    added += 1
            session.commit()
        return {"added": added, "updated": updated, "total_input": len(symbols)}

    def remove_manual(self, symbol: str) -> bool:
        with self.session_factory() as session:
            entry = session.get(RiskBlacklistEntry, symbol)
            if entry is None:
                return False
            session.delete(entry)
            session.commit()
            return True

    # ── 自动晋升 ────────────────────────────────────────────────────────
    def auto_promote(
        self,
        lookback_days: int | None = None,
        min_rejections: int | None = None,
        execution_domain: str = "paper",
    ) -> dict:
        """把过去 N 天 REJECTED ≥ min 次的 symbol 永久存进 risk_blacklist。

        每次 pipeline 跑前调一次：
        - 新出现的 REJECTED symbol 进表
        - 即便后续 clear-state 清了 orders 表，永久黑名单不丢
        """
        auto_set = self._compute_auto(
            lookback_days, min_rejections, execution_domain,
        )
        if not auto_set:
            return {"promoted": 0, "already_present": 0}

        with self.session_factory() as session:
            existing = {
                r[0] for r in session.execute(select(RiskBlacklistEntry.symbol)).all()
            }
            new_symbols = auto_set - existing
            for sym in new_symbols:
                session.add(RiskBlacklistEntry(
                    symbol=sym,
                    reason="auto-promoted from REJECTED orders",
                    added_at=_now(),
                ))
            session.commit()

        if new_symbols:
            logger.info("blacklist auto-promoted %d new symbols: %s",
                        len(new_symbols), sorted(new_symbols))
        return {
            "promoted": len(new_symbols),
            "already_present": len(auto_set & existing),
        }
