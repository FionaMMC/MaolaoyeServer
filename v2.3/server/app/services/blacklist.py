"""黑名单服务：自动从过去 N 天的 REJECTED orders 提取 symbols。

实盘里 QMT 可能因为 ST/退市/未签协议/涨跌停 等原因拒单。这些 symbol 服务器
没法在事前识别（缺成分股状态数据），但可以从历史拒单中学习。本服务在
pipeline 跑之前查 orders 表，把历史 REJECTED 的 symbol 收集成黑名单。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import Order

logger = logging.getLogger(__name__)


class BlacklistService:
    """从 orders 表自动提取被 QMT 拒单的 symbols。"""

    DEFAULT_LOOKBACK_DAYS = 30
    DEFAULT_MIN_REJECTIONS = 1

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def compute(
        self,
        lookback_days: int | None = None,
        min_rejections: int | None = None,
    ) -> set[str]:
        """返回过去 N 天 status='REJECTED' ≥ min 次的 symbol 集合。"""
        lookback = lookback_days if lookback_days is not None else self.DEFAULT_LOOKBACK_DAYS
        min_rej = min_rejections if min_rejections is not None else self.DEFAULT_MIN_REJECTIONS
        cutoff = (datetime.now() - timedelta(days=lookback)).strftime("%Y%m%d")

        with self.session_factory() as session:
            stmt = (
                select(Order.symbol)
                .where(Order.valid_date >= cutoff)
                .where(Order.status == "REJECTED")
            )
            rows = session.execute(stmt).all()

        counts = Counter(r[0] for r in rows)
        blacklist = {sym for sym, cnt in counts.items() if cnt >= min_rej}
        if blacklist:
            logger.info(
                "auto-blacklist: %d symbols (lookback=%dd, min_rejections=%d)",
                len(blacklist), lookback, min_rej,
            )
        return blacklist

    def stats(
        self,
        lookback_days: int | None = None,
    ) -> dict:
        """诊断用：返回每个被拒 symbol 的次数。"""
        lookback = lookback_days if lookback_days is not None else self.DEFAULT_LOOKBACK_DAYS
        cutoff = (datetime.now() - timedelta(days=lookback)).strftime("%Y%m%d")

        with self.session_factory() as session:
            stmt = (
                select(Order.symbol, Order.valid_date)
                .where(Order.valid_date >= cutoff)
                .where(Order.status == "REJECTED")
            )
            rows = session.execute(stmt).all()

        counts: dict[str, int] = {}
        for sym, _ in rows:
            counts[sym] = counts.get(sym, 0) + 1
        return {
            "lookback_days": lookback,
            "cutoff_date": cutoff,
            "rejected_total": len(rows),
            "unique_symbols": len(counts),
            "by_symbol": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        }
