"""策略实例每日 NAV 快照。"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.models import InstanceState, PerfSnapshot
from app.storage.parquet import ParquetStore

logger = logging.getLogger(__name__)


class PerfService:
    """计算并存储策略实例的每日净值快照。"""

    def __init__(self, session_factory, parquet_store: ParquetStore):
        self.session_factory = session_factory
        self.store = parquet_store

    def snapshot_all(self, trade_date: int) -> int:
        """对所有 instance 生成当日快照。返回写入条数。

        nav = virtual_cash + Σ(position[symbol] × close_price[symbol])

        若某只持仓股票当日无 close 数据（停牌或新上市），用最近一条 close 兜底；
        仍无数据则跳过该持仓的市值（仅算现金部分），并记 warning。
        """
        date_str = str(trade_date)
        with self.session_factory() as session:
            instances = session.execute(select(InstanceState)).scalars().all()
            written = 0
            for inst in instances:
                nav = self._compute_nav(inst, trade_date)
                positions_json = dict(inst.virtual_positions or {})

                # upsert：先查再决定 add 或更新
                existing = session.get(
                    PerfSnapshot, (inst.instance_id, date_str)
                )
                daily_return = self._compute_daily_return(
                    session, inst.instance_id, date_str, nav,
                )
                if existing:
                    existing.nav = nav
                    existing.daily_return = daily_return
                    existing.positions_snapshot = positions_json
                else:
                    session.add(PerfSnapshot(
                        instance_id=inst.instance_id,
                        date=date_str,
                        nav=nav,
                        daily_return=daily_return,
                        positions_snapshot=positions_json,
                    ))
                written += 1
            session.commit()
        return written

    # ── 内部 ──────────────────────────────────────────────────────────
    def _compute_nav(self, inst: InstanceState, trade_date: int) -> float:
        nav = float(inst.virtual_cash)
        positions = inst.virtual_positions or {}
        for symbol, qty in positions.items():
            close = self._latest_close_on_or_before(symbol, trade_date)
            if close is None:
                logger.warning(
                    "instance %s 持仓 %s 在 %s 及之前无 close 数据，市值按 0 计算",
                    inst.instance_id, symbol, trade_date,
                )
                continue
            nav += qty * close
        return round(nav, 4)

    def _latest_close_on_or_before(self, symbol: str, trade_date: int) -> float | None:
        # 先尝试 stocks，再 etfs（兼容 ETF 持仓）
        for category in ("stocks", "etfs"):
            df = self.store.read(category, symbol, end_date=trade_date)
            if not df.empty:
                return float(df["close"].iloc[-1])
        return None

    def _compute_daily_return(
        self, session, instance_id: str, date_str: str, today_nav: float,
    ) -> float | None:
        """跟昨日（数据库里上一条快照）算日收益率。无昨日返回 None。"""
        prev = session.execute(
            select(PerfSnapshot)
            .where(PerfSnapshot.instance_id == instance_id)
            .where(PerfSnapshot.date < date_str)
            .order_by(PerfSnapshot.date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if prev is None or prev.nav == 0:
            return None
        return round((today_nav - prev.nav) / prev.nav, 6)
