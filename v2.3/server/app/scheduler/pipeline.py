"""StrategyPipeline：策略 → 预检 → 归集 → 写订单 → NAV 快照 一站式编排。"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Type

import yaml
from sqlalchemy import delete, select

from app.models import InstanceState, Order, OrderSignalMap, RawSignal
from app.services.aggregate import AggregateService, TaggedSignal
from app.services.blacklist import BlacklistService
from app.services.orders_queue import OrdersQueueService
from app.services.perf import PerfService
from app.services.precheck import PrecheckResult, PrecheckService
from app.storage.parquet import ParquetStore
from app.strategy.base import RawSignal as RawSignalModel
from app.strategy.base import Strategy
from app.strategy.runner import StrategyRunner

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class StrategyPipeline:
    """同步执行：策略 → 预检 → 归集 → 落库 → NAV 快照。"""

    def __init__(
        self,
        registry: dict[str, Type[Strategy]],
        parquet_store: ParquetStore,
        session_factory,
        precheck: PrecheckService,
        aggregate: AggregateService,
        orders_queue: OrdersQueueService,
        perf: PerfService,
        strategies_yaml_path: Path,
        blacklist: BlacklistService | None = None,
    ):
        self.registry = registry
        self.store = parquet_store
        self.session_factory = session_factory
        self.precheck = precheck
        self.aggregate = aggregate
        self.orders_queue = orders_queue
        self.perf = perf
        self.strategies_yaml_path = Path(strategies_yaml_path)
        self.blacklist = blacklist or BlacklistService(session_factory)

    def run(self, trade_date: int) -> dict:
        """完整管线。返回执行摘要。

        幂等：本日（trade_date）已有 raw_signals/orders/order_signal_map 一律先清掉，
        再写新一批。重复触发不会造 dupe。
        """
        valid_date_str = str(trade_date)
        logger.info("pipeline_start trade_date=%s", trade_date)

        # 0a. 自动晋升：把最近 REJECTED 的 symbol 持久化到 risk_blacklist 表
        #     这样即使后面 clear-state 清了 orders，黑名单不丢
        promoted = self.blacklist.auto_promote()
        if promoted["promoted"]:
            logger.info("pipeline blacklist auto-promote: %s", promoted)

        # 0b. 幂等：清同一 trade_date 的旧 raw_signals + orders + order_signal_map
        cleared = self._clear_for_date(valid_date_str)
        if any(cleared.values()):
            logger.info("pipeline cleared stale data for %s: %s", trade_date, cleared)

        # 1. 加载 strategies.yaml
        instances = self._load_instances()
        if not instances:
            logger.warning("strategies.yaml 无 instance 定义，pipeline 退出")
            return {"signals": 0, "passed": 0, "orders": 0, "instances": 0}

        # 2. 加载/创建 instance_state
        states = self._ensure_instance_states(instances)

        # 2.5. 计算 risk blacklist（自动 + 手工）
        risk_bl = self.blacklist.compute()

        # 3. 跑策略
        runner = StrategyRunner(registry=self.registry, parquet_store=self.store)
        signals_by_instance, next_states_by_instance = runner.run_all(
            trade_date,
            instances=[
                {
                    "instance_id": inst["instance_id"],
                    "strategy_id": inst["strategy_id"],
                    "virtual_cash": states[inst["instance_id"]]["cash"],
                    "virtual_positions": states[inst["instance_id"]]["positions"],
                    "strategy_state": states[inst["instance_id"]].get("strategy_state"),
                }
                for inst in instances
            ],
            risk_blacklist=risk_bl,
        )

        # 3.5. 写回 strategy_state（仅在策略 set_strategy_state 时）
        if any(s is not None for s in next_states_by_instance.values()):
            with self.session_factory() as session:
                for inst_id, new_state in next_states_by_instance.items():
                    if new_state is None:
                        continue
                    row = session.get(InstanceState, inst_id)
                    if row is None:
                        continue
                    row.strategy_state = new_state
                    row.last_update = _now_iso()
                session.commit()

        # 4. 预检 + 写 raw_signals 表 + 收集 PASS 的
        # 关键顺序（Bug A 修复）：每个实例内 SELL 先 precheck，
        # 把 SELL 净收入累加到 running_cash，再 precheck BUY。
        # 否则 BUY 会因为没看到同日待执行的 SELL 释放出来的现金而误报"cash 不足"。
        # （客户端早就修了同样的 SELL-first 风控，commit 8984f0f。）
        all_pass_tagged: list[TaggedSignal] = []
        signals_total = 0
        passed_total = 0
        with self.session_factory() as session:
            for inst in instances:
                instance_id = inst["instance_id"]
                signals = signals_by_instance.get(instance_id, [])
                state = states[instance_id]

                # 排序：SELL 先 BUY 后；同方向保持原序（stable sort）
                ordered = sorted(signals, key=lambda s: 0 if s.direction == "SELL" else 1)

                # 每个实例独立的 running cash/positions（不修改 instance_state，
                # 只供 precheck 链式累积）
                running_cash = float(state["cash"])
                running_positions = dict(state["positions"])

                for sig in ordered:
                    signals_total += 1
                    pre = self.precheck.check(
                        sig, running_cash, running_positions,
                    )

                    # 若 PASS，预扣 running 现金/持仓供下一笔同实例 signal 使用
                    if pre.status == "PASS":
                        limit_p = sig.reference_price * (1.0 + sig.price_offset)
                        gross = sig.quantity * limit_p
                        if sig.direction == "SELL":
                            # 卖出净收入：扣佣金（rate, min）+ 印花税
                            running_cash += gross * (1.0 - self.precheck.fee_rate)
                            cur = running_positions.get(sig.symbol, 0)
                            new_qty = cur - sig.quantity
                            if new_qty <= 0:
                                running_positions.pop(sig.symbol, None)
                            else:
                                running_positions[sig.symbol] = new_qty
                        else:  # BUY
                            running_cash -= gross * (1.0 + self.precheck.fee_rate)
                            running_positions[sig.symbol] = (
                                running_positions.get(sig.symbol, 0) + sig.quantity
                            )

                    signal_id = uuid.uuid4().hex
                    limit_price = sig.reference_price * (1 + sig.price_offset)
                    session.add(RawSignal(
                        signal_id=signal_id,
                        instance_id=instance_id,
                        symbol=sig.symbol,
                        direction=sig.direction,
                        quantity=sig.quantity,
                        reference_price=sig.reference_price,
                        price_offset=sig.price_offset,
                        limit_price=round(limit_price, 4),
                        valid_date=valid_date_str,
                        signal_time=_now_iso(),
                        precheck_status=pre.status,
                        precheck_reason=pre.reason,
                    ))
                    if pre.status == "PASS":
                        passed_total += 1
                        all_pass_tagged.append(TaggedSignal(
                            signal_id=signal_id,
                            account_group=inst["account_group"],
                            raw=sig,
                        ))
            session.commit()

        # 5. 归集
        agg = self.aggregate.aggregate(all_pass_tagged, valid_date=valid_date_str)

        # 6. 写订单 + 映射
        self.orders_queue.write_aggregated(agg.orders, agg.mappings)

        # 7. NAV 快照
        # 关键：用「**today**」作为快照日期，不是 trade_date。
        # 因为 trigger 通常在 T 日 16:00 跑、trade_date 是 T+1，如果用 trade_date 写
        # 会出现「T+1 还没到就写了 T+1 的 NAV 快照」（错误地用 T 收盘价 + T EOD state
        # 当成 T+1 的收益）。
        # 改成用 today，意味着：
        #   - T 日 16:00 trigger T+1 → 写 snapshot[today=T]，用 T EOD state + T close → 正确
        #   - T 日 9:00 同日 trigger T  → 也写 snapshot[today=T]，但是 pre-trade state
        #     → 后面 16:00 trigger 时 UPSERT 覆盖为正确版本
        today_str = datetime.now().strftime("%Y%m%d")
        self.perf.snapshot_all(int(today_str))

        summary = {
            "trade_date": trade_date,
            "instances": len(instances),
            "signals": signals_total,
            "passed": passed_total,
            "orders": len(agg.orders),
        }
        logger.info("pipeline_done %s", summary)
        return summary

    # ── 内部 ──────────────────────────────────────────────────────────
    def _load_instances(self) -> list[dict]:
        """从 strategies.yaml 解析出扁平化的实例列表。"""
        if not self.strategies_yaml_path.exists():
            logger.warning("strategies.yaml 不存在: %s", self.strategies_yaml_path)
            return []
        with self.strategies_yaml_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        instances: list[dict] = []
        for ag in cfg.get("account_groups", []):
            group_id = ag["group_id"]
            for strat in ag.get("strategies", []):
                instances.append({
                    "instance_id": f"{group_id}_{strat['strategy_id']}",
                    "account_group": group_id,
                    "strategy_id": strat["strategy_id"],
                    "virtual_initial_cash": float(strat.get("virtual_initial_cash", 0)),
                    "owned_symbols": strat.get("owned_symbols"),  # list[str] or None
                })
        return instances

    def _ensure_instance_states(
        self, instances: list[dict],
    ) -> dict[str, dict]:
        """加载 InstanceState；缺失的用 yaml 里的 virtual_initial_cash 创建。"""
        result: dict[str, dict] = {}
        with self.session_factory() as session:
            existing = {
                row.instance_id: row
                for row in session.execute(select(InstanceState)).scalars().all()
            }
            for inst in instances:
                instance_id = inst["instance_id"]
                if instance_id in existing:
                    row = existing[instance_id]
                    # 同步 owned_symbols (yaml 是 source of truth)
                    yaml_owned = inst.get("owned_symbols")
                    if row.owned_symbols != yaml_owned:
                        row.owned_symbols = yaml_owned
                    result[instance_id] = {
                        "cash": float(row.virtual_cash),
                        "positions": dict(row.virtual_positions or {}),
                        "strategy_state": (
                            dict(row.strategy_state) if row.strategy_state else None
                        ),
                    }
                else:
                    cash = inst["virtual_initial_cash"]
                    session.add(InstanceState(
                        instance_id=instance_id,
                        virtual_cash=cash,
                        virtual_positions={},
                        owned_symbols=inst.get("owned_symbols"),
                        last_update=_now_iso(),
                    ))
                    result[instance_id] = {
                        "cash": cash, "positions": {}, "strategy_state": None,
                    }
            session.commit()
        return result

    def _clear_for_date(self, valid_date: str) -> dict[str, int]:
        """清除指定 valid_date 的 raw_signals + orders + order_signal_map。

        order_signal_map 通过 raw_signals 的 signal_id 反查删除。
        """
        with self.session_factory() as session:
            sig_ids = [
                r[0]
                for r in session.execute(
                    select(RawSignal.signal_id).where(RawSignal.valid_date == valid_date)
                ).all()
            ]
            order_ids = [
                r[0]
                for r in session.execute(
                    select(Order.order_id).where(Order.valid_date == valid_date)
                ).all()
            ]
            mapping_count = 0
            if sig_ids:
                mapping_count = session.execute(
                    delete(OrderSignalMap).where(OrderSignalMap.signal_id.in_(sig_ids))
                ).rowcount or 0
            sig_count = session.execute(
                delete(RawSignal).where(RawSignal.valid_date == valid_date)
            ).rowcount or 0
            order_count = session.execute(
                delete(Order).where(Order.valid_date == valid_date)
            ).rowcount or 0
            session.commit()
        return {
            "raw_signals": sig_count,
            "orders": order_count,
            "order_signal_map": mapping_count,
        }

    def reset_instance_states(self) -> dict[str, dict]:
        """把所有 instance_state 的 virtual_cash 还原成 strategies.yaml 初始值，
        virtual_positions 清空。配合 /admin/clear-state?include_instance_state=true。
        """
        instances = self._load_instances()
        result = {}
        with self.session_factory() as session:
            existing = {
                row.instance_id: row
                for row in session.execute(select(InstanceState)).scalars().all()
            }
            for inst in instances:
                instance_id = inst["instance_id"]
                if instance_id in existing:
                    row = existing[instance_id]
                    row.virtual_cash = inst["virtual_initial_cash"]
                    row.virtual_positions = {}
                    row.last_update = _now_iso()
                    result[instance_id] = {
                        "cash": inst["virtual_initial_cash"],
                        "positions": {},
                    }
            session.commit()
        return result
