"""StrategyRunner：迭代所有策略实例。"""
from __future__ import annotations

import logging
from typing import Type

from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal, Strategy
from app.strategy.context import Context

logger = logging.getLogger(__name__)


class StrategyRunner:
    def __init__(
        self,
        registry: dict[str, Type[Strategy]],
        parquet_store: ParquetStore,
    ):
        self.registry = registry
        self.store = parquet_store

    def run_all(
        self,
        trade_date: int,
        instances: list[dict],
        risk_blacklist: set[str] | None = None,
    ) -> dict[str, list[RawSignal]]:
        results: dict[str, list[RawSignal]] = {}
        bl = set(risk_blacklist or ())

        for inst in instances:
            instance_id = inst["instance_id"]
            strategy_id = inst["strategy_id"]

            strategy_cls = self.registry.get(strategy_id)
            if strategy_cls is None:
                logger.error(
                    "instance %s: strategy '%s' 未注册",
                    instance_id, strategy_id,
                )
                results[instance_id] = []
                continue

            ctx = Context(
                instance_id=instance_id,
                trade_date=trade_date,
                virtual_cash=inst["virtual_cash"],
                virtual_positions=inst.get("virtual_positions", {}),
                parquet_store=self.store,
                risk_blacklist=bl,
            )

            try:
                signals = strategy_cls().run(ctx, trade_date)
                if not isinstance(signals, list):
                    raise TypeError(
                        f"strategy.run 必须返回 list[RawSignal]，实际 {type(signals)}"
                    )
                results[instance_id] = signals
            except Exception as e:
                logger.exception("instance %s strategy.run 抛异常: %s", instance_id, e)
                results[instance_id] = []

        return results
