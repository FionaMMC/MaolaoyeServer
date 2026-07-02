"""StrategyPipeline：策略 → 预检 → 归集 → 写订单 → NAV 快照 一站式编排。"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Type

import yaml
from sqlalchemy import delete, select

from app.models import InstanceState, Order, OrderSignalMap, RawSignal, Trade
from app.services.aggregate import AggregateService, TaggedSignal
from app.services.blacklist import BlacklistService
from app.services.orders_queue import OrdersQueueService
from app.services.perf import PerfService
from app.services.precheck import PrecheckService
from app.storage.parquet import ParquetStore
from app.strategy.base import Strategy
from app.strategy.runner import StrategyRunner

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _to_date(yyyymmdd: int) -> date:
    d = int(yyyymmdd)
    return date(d // 10000, (d // 100) % 100, d % 100)


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
        max_staleness_days: int | None = None,
        freshness_probe_category: str = "indexes",
        freshness_probe_symbol: str = "000852.SH",
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
        # 数据新鲜度护栏：None=关闭（向后兼容）。设为天数则：最新行情比 trade_date
        # 旧超过该天数时，管线跳过，不写 raw_signals/orders，防止拿陈旧价下错单。
        self.max_staleness_days = max_staleness_days
        self.freshness_probe_category = freshness_probe_category
        self.freshness_probe_symbol = freshness_probe_symbol

    def _market_staleness_days(self, trade_date: int) -> int | None:
        """行情比 trade_date 旧几天。护栏关（None）或探针缺失（无法评估）时返回 None。"""
        if self.max_staleness_days is None:
            return None
        latest = self.store.latest_date(
            self.freshness_probe_category, self.freshness_probe_symbol)
        if latest is None:
            logger.warning(
                "freshness_probe_missing %s/%s — 无法评估行情新鲜度，放行",
                self.freshness_probe_category, self.freshness_probe_symbol)
            return None
        return (_to_date(trade_date) - _to_date(latest)).days

    def run(self, trade_date: int, force: bool = False) -> dict:
        """完整管线。返回执行摘要。

        幂等：本日（trade_date）已有 raw_signals/orders/order_signal_map 一律先清掉，
        再写新一批。重复触发不会造 dupe。

        force：仅放行『已拉取』护栏（00c），操作者必须保证客户端会重新拉取信号。
        『已结算』护栏（00b）任何情况下不可绕过。
        """
        valid_date_str = str(trade_date)
        logger.info("pipeline_start trade_date=%s", trade_date)

        # 00. 数据新鲜度护栏：行情比 trade_date 旧太多 → 跳过，绝不拿陈旧价下单。
        #     （灾备/回填历史 trade_date 时行情正好在该日附近，diff 小，自然放行。）
        stale = self._market_staleness_days(trade_date)
        if stale is not None and stale > self.max_staleness_days:
            latest = self.store.latest_date(
                self.freshness_probe_category, self.freshness_probe_symbol)
            logger.error(
                "pipeline_skipped stale_market_data probe=%s/%s latest=%s trade_date=%s "
                "staleness_days=%s tolerance=%s",
                self.freshness_probe_category, self.freshness_probe_symbol,
                latest, trade_date, stale, self.max_staleness_days)
            return {
                "signals": 0, "passed": 0, "orders": 0, "instances": 0,
                "skipped": "stale_market_data",
                "latest_market_date": latest, "trade_date": trade_date,
                "staleness_days": stale,
            }

        # 00b. 已结算护栏（2026-06 孤儿成交事故根因）：该 valid_date 已收到成交回报
        #      → 绝不重算。重算会先 _clear_for_date 删掉已结算订单 → 其 trades 变孤儿；
        #      再 aggregate 生成新 uuid 订单（PENDING），客户端永不针对它回报 → SELL
        #      永久卡 PENDING。何况该日竞价已成交，重算策略也无意义。直接跳过，保持原状。
        settled = self._settled_order_ids(valid_date_str)
        if settled:
            logger.error(
                "pipeline_skipped already_settled valid_date=%s settled_orders=%d "
                "— 该日订单已收到成交回报，拒绝重算以免孤儿化 trades / 重生 PENDING",
                valid_date_str, len(settled))
            return {
                "signals": 0, "passed": 0, "orders": 0, "instances": 0,
                "skipped": "already_settled", "valid_date": valid_date_str,
                "settled_orders": len(settled),
            }

        # 00c. 已拉取护栏（2026-07-02 成交未匹配事故根因）：客户端已 GET /orders
        #      拉走该日订单 → 默认拒绝重算。重算会换掉 order_id，客户端不会在下单前
        #      重新拉取 → 次日成交回报全量 unmatched，成交静默不入账。
        #      确需重算：force=true，且操作者必须让客户端在下单前重新拉取信号。
        fetched = self._fetched_order_ids(valid_date_str)
        if fetched and not force:
            logger.error(
                "pipeline_skipped already_fetched valid_date=%s fetched_orders=%d "
                "— 客户端已拉取该日订单，重算会换掉 order_id 导致次日成交回报全量 "
                "unmatched。确需重算请 force=true，并务必让客户端重新拉取信号",
                valid_date_str, len(fetched))
            return {
                "signals": 0, "passed": 0, "orders": 0, "instances": 0,
                "skipped": "already_fetched", "valid_date": valid_date_str,
                "fetched_orders": len(fetched),
            }
        if fetched and force:
            logger.error(
                "pipeline_force_regen_after_fetch valid_date=%s fetched_orders=%d "
                "— 强制重算已拉取批次；客户端必须在下单前重新拉取信号，"
                "否则次日成交回报无法匹配",
                valid_date_str, len(fetched))

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
        if fetched and force:
            # 审计标记：这次重算换掉了已被客户端拉走的批次
            summary["force_regen_after_fetch"] = len(fetched)
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

    def _settled_order_ids(self, valid_date: str) -> set[str]:
        """该 valid_date 下已收到成交回报（trades 表有记录）的 order_id 集合。

        "已结算" = 客户端已针对该订单回报成交。删这种订单会孤儿化其 trades，
        所以它既是 run() 重算护栏的判据，也是 _clear_for_date 的保留名单。
        """
        with self.session_factory() as session:
            return {
                r[0]
                for r in session.execute(
                    select(Order.order_id)
                    .where(Order.valid_date == valid_date)
                    .where(Order.order_id.in_(select(Trade.order_id)))
                ).all()
            }

    def _fetched_order_ids(self, valid_date: str) -> set[str]:
        """该 valid_date 下客户端已拉取（fetched_at 非空）的 order_id 集合。

        "已拉取" = 客户端可能已按这些 order_id 下了真实委托。换掉它们的 ID
        会让次日成交回报全量 unmatched，所以是 run() 重算护栏（00c）的判据。
        """
        with self.session_factory() as session:
            return {
                r[0]
                for r in session.execute(
                    select(Order.order_id)
                    .where(Order.valid_date == valid_date)
                    .where(Order.fetched_at.is_not(None))
                ).all()
            }

    def _clear_for_date(self, valid_date: str) -> dict[str, int]:
        """清除指定 valid_date 的 raw_signals + orders + order_signal_map。

        order_signal_map 通过 raw_signals 的 signal_id 反查删除。

        例外：已结算订单（trades 表有记录）一律保留——删它会让其 trades 变孤儿。
        其关联的 raw_signals/order_signal_map 一并保留，保持账面可追溯。
        """
        with self.session_factory() as session:
            # 受保护订单（已结算）+ 其关联 signal_id —— 不删。两个集合都很小
            # （只含本日已成交的少数单），用作排除名单，避免对全日 signal 物化大 IN 列表。
            settled_order_ids = {
                r[0]
                for r in session.execute(
                    select(Order.order_id)
                    .where(Order.valid_date == valid_date)
                    .where(Order.order_id.in_(select(Trade.order_id)))
                ).all()
            }
            protected_sig_ids: set[str] = set()
            if settled_order_ids:
                protected_sig_ids = {
                    r[0]
                    for r in session.execute(
                        select(OrderSignalMap.signal_id)
                        .where(OrderSignalMap.order_id.in_(settled_order_ids))
                    ).all()
                }

            # 本日全部 signal 用子查询表达（不物化成 Python 列表 → 不撞 SQLite IN 上限）。
            # 必须先删 OSM（其 WHERE 引用 raw_signals），再删 raw_signals。
            date_sig_ids = select(RawSignal.signal_id).where(
                RawSignal.valid_date == valid_date)
            osm_stmt = delete(OrderSignalMap).where(
                OrderSignalMap.signal_id.in_(date_sig_ids))
            sig_stmt = delete(RawSignal).where(RawSignal.valid_date == valid_date)
            order_stmt = delete(Order).where(Order.valid_date == valid_date)
            if protected_sig_ids:
                osm_stmt = osm_stmt.where(
                    OrderSignalMap.signal_id.notin_(protected_sig_ids))
                sig_stmt = sig_stmt.where(RawSignal.signal_id.notin_(protected_sig_ids))
            if settled_order_ids:
                order_stmt = order_stmt.where(Order.order_id.notin_(settled_order_ids))

            mapping_count = session.execute(osm_stmt).rowcount or 0
            sig_count = session.execute(sig_stmt).rowcount or 0
            order_count = session.execute(order_stmt).rowcount or 0
            session.commit()
        return {
            "raw_signals": sig_count,
            "orders": order_count,
            "order_signal_map": mapping_count,
            "preserved_settled": len(settled_order_ids),
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
