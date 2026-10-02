"""策略实例每日 NAV 快照。"""
from __future__ import annotations

import logging
import math

from sqlalchemy import select

from app.models import CashFlowJournal, InstanceState, PerfSnapshot, PerfValuation
from app.storage.parquet import ParquetStore
from app.services.dividend_entitlement import outstanding_dividends

logger = logging.getLogger(__name__)


class PerfService:
    """计算并存储策略实例的每日净值快照。"""

    def __init__(self, session_factory, parquet_store: ParquetStore):
        self.session_factory = session_factory
        self.store = parquet_store

    def snapshot_all(self, trade_date: int, execution_domain: str = "paper") -> int:
        """对所有 instance 生成当日快照。返回写入条数。

        nav = virtual_cash + Σ(position[symbol] × close_price[symbol]) + dividend_receivable

        若某只持仓股票当日无 close 数据（停牌或新上市），用最近一条 close 兜底；
        仍无有效价格则该实例等待估值，不写伪造净值；其他实例继续。
        """
        date_str = str(trade_date)
        with self.session_factory() as session:
            instances = session.execute(
                select(InstanceState).where(
                    InstanceState.execution_domain == execution_domain
                )
            ).scalars().all()
            written = 0
            for inst in instances:
                nav = self._compute_nav(inst, trade_date)
                if nav is None:
                    continue
                try:
                    receivable = outstanding_dividends(session, inst.instance_id, execution_domain, date_str)
                except ValueError:
                    logger.exception("instance %s dividend valuation requires reconciliation; cash/trading unchanged", inst.instance_id)
                    continue
                nav = round(nav + float(receivable), 4)
                positions_json = dict(inst.virtual_positions or {})

                # upsert：先查再决定 add 或更新
                existing = session.get(
                    PerfSnapshot, (inst.instance_id, date_str)
                )
                daily_return = self._compute_daily_return(
                    session, inst.instance_id, date_str, nav, execution_domain,
                )
                if existing:
                    existing.nav = nav
                    existing.daily_return = daily_return
                    existing.positions_snapshot = positions_json
                else:
                    session.add(PerfSnapshot(
                        instance_id=inst.instance_id,
                        date=date_str,
                        execution_domain=execution_domain,
                        nav=nav,
                        daily_return=daily_return,
                        positions_snapshot=positions_json,
                    ))
                component = session.get(PerfValuation, (inst.instance_id, date_str))
                if component is None:
                    component = PerfValuation(instance_id=inst.instance_id, date=date_str)
                    session.add(component)
                component.execution_domain = execution_domain
                component.nav = nav
                component.cash = float(inst.virtual_cash)
                component.dividend_receivable = float(receivable)
                written += 1
            session.commit()
        return written

    # ── 内部 ──────────────────────────────────────────────────────────
    def _compute_nav(self, inst: InstanceState, trade_date: int) -> float | None:
        nav = float(inst.virtual_cash)
        positions = inst.virtual_positions or {}
        missing = []
        for symbol, qty in positions.items():
            if qty == 0:
                continue
            close = self._latest_close_on_or_before(symbol, trade_date)
            if close is None or not math.isfinite(close) or close <= 0:
                missing.append(symbol)
                continue
            nav += qty * close
        state = dict(inst.strategy_state or {})
        if missing or "valuation_status" in state:
            state["valuation_status"] = {
                "status": "WAITING_PRICE" if missing else "VALUED",
                "requested_date": str(trade_date), "missing_symbols": sorted(missing),
            }
            inst.strategy_state = state
        if missing:
            logger.warning("instance %s waiting_price date=%s symbols=%s; NAV not published", inst.instance_id, trade_date, missing)
            return None
        return round(nav, 4)

    def _latest_close_on_or_before(self, symbol: str, trade_date: int) -> float | None:
        # 先尝试 stocks，再 etfs（兼容 ETF 持仓）
        for category in ("stocks", "etfs"):
            df = self.store.read(category, symbol, end_date=trade_date)
            if not df.empty:
                return float(df["close"].iloc[-1])
        return None

    def _compute_daily_return(
        self,
        session,
        instance_id: str,
        date_str: str,
        today_nav: float,
        execution_domain: str = "paper",
    ) -> float | None:
        """End-of-period-flow-adjusted return since the preceding snapshot.

        Capital movements and external deposits/withdrawals are not performance.
        Dividends remain investment income. This compatibility calculation
        assumes flows occur at period end; it is not intraday time-weighted NAV.
        """
        prev = session.execute(
            select(PerfSnapshot)
            .where(PerfSnapshot.instance_id == instance_id)
            .where(PerfSnapshot.execution_domain == execution_domain)
            .where(PerfSnapshot.date < date_str)
            .order_by(PerfSnapshot.date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if prev is None or prev.nav == 0:
            return None
        flows = session.execute(select(CashFlowJournal.amount).where(
            CashFlowJournal.instance_id == instance_id,
            CashFlowJournal.execution_domain == execution_domain,
            CashFlowJournal.status == "APPLIED",
            CashFlowJournal.event_type.in_((
                "DEPOSIT", "WITHDRAWAL", "CAPITAL_ALLOCATION", "CAPITAL_DEALLOCATION",
            )),
            CashFlowJournal.event_date > prev.date,
            CashFlowJournal.event_date <= date_str,
        )).scalars().all()
        net_external_flow = sum(float(amount) for amount in flows)
        return round((today_nav - prev.nav - net_external_flow) / prev.nav, 6)
