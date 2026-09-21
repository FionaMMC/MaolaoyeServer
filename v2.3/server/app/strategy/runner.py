"""StrategyRunner：迭代所有策略实例。"""
from __future__ import annotations

import logging
from typing import Type

from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal, Strategy, StrategyInputNotReady
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
        self.input_statuses: dict[str, dict] = {}
        self.errors: dict[str, str] = {}

    def run_all(
        self,
        trade_date: int,
        instances: list[dict],
        risk_blacklist: set[str] | None = None,
        execution_guards: dict[str, dict] | None = None,
    ) -> tuple[dict[str, list[RawSignal]], dict[str, dict | None]]:
        """返回 (signals_by_instance, next_strategy_state_by_instance)。

        next_strategy_state_by_instance: 策略 set_strategy_state 后留下的新状态；
        若策略没调用 set，则该 key 对应 None（pipeline 不会更新）。
        """
        results: dict[str, list[RawSignal]] = {}
        self.input_statuses = {}
        self.errors = {}
        next_states: dict[str, dict | None] = {}
        bl = set(risk_blacklist or ())
        guards = execution_guards or {}

        for inst in instances:
            instance_id = inst["instance_id"]
            strategy_id = inst["strategy_id"]

            strategy_cls = self.registry.get(strategy_id)
            if strategy_cls is None:
                self.errors[instance_id] = "strategy_not_registered"
                logger.error(
                    "instance %s: strategy '%s' 未注册",
                    instance_id, strategy_id,
                )
                results[instance_id] = []
                next_states[instance_id] = None
                continue

            ctx = Context(
                instance_id=instance_id,
                trade_date=trade_date,
                virtual_cash=inst["virtual_cash"],
                virtual_positions=inst.get("virtual_positions", {}),
                parquet_store=self.store,
                risk_blacklist=bl,
                strategy_state=inst.get("strategy_state"),
                execution_guard=guards.get(instance_id),
            )

            try:
                signals = strategy_cls().run(ctx, trade_date)
                if not isinstance(signals, list):
                    raise TypeError(
                        f"strategy.run 必须返回 list[RawSignal]，实际 {type(signals)}"
                    )
                results[instance_id] = signals
                next_states[instance_id] = ctx.pop_next_strategy_state()
                previous = dict(inst.get("strategy_state") or {})
                if "input_readiness" in previous:
                    updated = dict(next_states[instance_id] if next_states[instance_id] is not None else previous)
                    pending = previous["input_readiness"]
                    if pending.get("status") == "WAITING_INPUT" and pending.get("requested_trade_date") != str(trade_date):
                        # A later no-op day does not prove the missed rebalance
                        # recovered. Retain its business date for explicit replay.
                        updated["input_readiness"] = pending
                        self.input_statuses[instance_id] = pending
                    else:
                        updated["input_readiness"] = {"status": "READY", "requested_trade_date": str(trade_date)}
                    next_states[instance_id] = updated
            except StrategyInputNotReady as e:
                status = {"status": "WAITING_INPUT", "requested_trade_date": str(trade_date), "reason": str(e)}
                self.input_statuses[instance_id] = status
                results[instance_id] = []
                next_states[instance_id] = {**dict(inst.get("strategy_state") or {}), "input_readiness": status}
                logger.warning("instance %s waiting_input: %s", instance_id, e)
            except Exception as e:
                self.errors[instance_id] = type(e).__name__
                logger.exception("instance %s strategy.run 抛异常: %s", instance_id, e)
                results[instance_id] = []
                next_states[instance_id] = None

        return results, next_states
