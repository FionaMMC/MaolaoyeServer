"""成交回报处理：拆单 + 更新虚拟账本 + 标记订单状态。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import InstanceState, Order, OrderSignalMap, RawSignal, Trade
from app.schemas.trade_result import TradeResult, TradeResultResponseData

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _flag_bookkeeping_divergence(order: Order) -> None:
    """标记一笔 order 的真账户成交与虚拟账本对账失败。

    用法：settlement 在防穿仓/超卖触发时调用。order.status 保持真账户上报的值
    （通常 FILLED/PARTIAL）；这个 flag 让 admin 端能快速找出"账面成功但账本未
    同步"的订单做人工对账。
    """
    order.bookkeeping_divergence = True


def largest_remainder_split(total: int, weights: list[int]) -> list[int]:
    """最大余数法把 total 按 weights 比例拆为整数列表（保证 sum == total）。

    若 sum(weights) == 0 或 total == 0，返回全 0。

    Examples:
        >>> largest_remainder_split(350, [100, 200, 300])
        [58, 117, 175]   # sum = 350
    """
    if total == 0 or sum(weights) == 0:
        return [0] * len(weights)
    sw = sum(weights)
    raw = [total * w / sw for w in weights]
    floors = [int(x) for x in raw]
    remainder = total - sum(floors)
    if remainder == 0:
        return floors
    fractional = [(raw[i] - floors[i], i) for i in range(len(raw))]
    fractional.sort(key=lambda t: -t[0])  # 余数从大到小
    for k in range(remainder):
        floors[fractional[k][1]] += 1
    return floors


class SettlementService:
    """成交回报处理服务。"""

    def __init__(
        self,
        session_factory,
        *,
        commission_rate: float = 0.0003,
        min_commission: float = 5.0,
        stamp_duty_sell: float = 0.0005,
    ):
        """
        Args:
            commission_rate: 双边佣金率（默认万三）
            min_commission: 单笔最低佣金（默认 5 元）
            stamp_duty_sell: 印花税（仅卖出收，默认千五）
        """
        self.session_factory = session_factory
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_duty_sell = stamp_duty_sell

    def _calc_fees(self, gross: float, direction: str) -> float:
        """计算单边交易费用：佣金（双边）+ 印花税（仅 SELL）。"""
        commission = max(self.min_commission, gross * self.commission_rate)
        duty = gross * self.stamp_duty_sell if direction == "SELL" else 0.0
        return commission + duty

    def settle(
        self,
        trade_date: str,
        results: list[TradeResult],
    ) -> TradeResultResponseData:
        """处理一批成交回报。"""
        matched = 0
        duplicate = 0
        unmatched: list[str] = []
        unmatched_candidates: dict[str, list[str]] = {}

        with self.session_factory() as session:
            for result in results:
                order = session.get(Order, result.order_id)
                if order is None:
                    unmatched.append(result.order_id)
                    candidates = self._find_candidates(session, trade_date, result)
                    if candidates:
                        unmatched_candidates[result.order_id] = candidates
                        logger.error(
                            "settle unmatched order_id=%s — 当日存在疑似同单候选 %s "
                            "(symbol=%s direction=%s qty=%s)。典型成因：客户端拉取后"
                            "管线被重算换掉了 order_id。请核对后人工重推或结算。",
                            result.order_id, candidates,
                            result.symbol, result.direction, result.filled_quantity,
                        )
                    continue

                # ── 幂等守卫（P0-1 / 5/12 重复结算事故）──────────────────────
                # 同一笔成交回报被重复推送（客户端网络重试 / 全量重推）时绝不能
                # 二次入账。幂等键 = order_id + filled_time + filled_quantity +
                # filled_price。命中已存在记录 → 整笔跳过：不写 trades、不动虚拟
                # 账本、不改 order.status。合法的分笔成交（filled_time/数量不同）
                # 不会命中，照常入账。
                already = session.execute(
                    select(Trade.id).where(
                        Trade.order_id == result.order_id,
                        Trade.filled_time == result.filled_time,
                        Trade.filled_quantity == result.filled_quantity,
                        Trade.filled_price == result.filled_price,
                    )
                ).first()
                if already is not None:
                    duplicate += 1
                    logger.warning(
                        "settle 幂等守卫跳过重复成交: order=%s time=%s qty=%s px=%s "
                        "— 客户端重推，已忽略不再入账",
                        result.order_id, result.filled_time,
                        result.filled_quantity, result.filled_price,
                    )
                    continue

                # 写 trades 表
                session.add(Trade(
                    order_id=result.order_id,
                    filled_quantity=result.filled_quantity,
                    filled_price=result.filled_price,
                    filled_time=result.filled_time,
                    status=result.status,
                    received_at=_now_iso(),
                ))

                # 拆单 + 更新虚拟账本（仅在有成交时）
                if result.filled_quantity > 0:
                    self._split_and_update_state(
                        session, order, result.filled_quantity, result.filled_price,
                    )

                # 标记订单状态
                order.status = result.status
                matched += 1

            session.commit()

        if duplicate:
            logger.warning(
                "settle trade_date=%s: 忽略 %d 笔重复成交回报（幂等守卫）",
                trade_date, duplicate,
            )

        return TradeResultResponseData(
            trade_date=trade_date,
            matched_count=matched,
            unmatched_order_ids=unmatched,
            unmatched_candidates=unmatched_candidates,
        )

    # ── 内部 ────────────────────────────────────────────────────────────
    @staticmethod
    def _find_candidates(session, trade_date: str, result: TradeResult) -> list[str]:
        """unmatched 回报的候选订单：当日同 symbol/direction/quantity 的未结算单。

        老客户端 payload 不带 symbol/direction → 退化为仅按 quantity 匹配
        （弱信号，仅供人工核对，不自动结算）。
        """
        stmt = (
            select(Order.order_id)
            .where(Order.valid_date == trade_date)
            .where(Order.status == "PENDING")
            .where(Order.quantity == result.filled_quantity)
        )
        if result.symbol:
            stmt = stmt.where(Order.symbol == result.symbol)
        if result.direction:
            stmt = stmt.where(Order.direction == result.direction)
        return [r[0] for r in session.execute(stmt).all()]

    def _split_and_update_state(
        self,
        session,
        order: Order,
        filled_qty: int,
        filled_price: float,
    ) -> None:
        # 取该 order 的所有 (signal_id, signal_quantity)
        mappings = session.execute(
            select(OrderSignalMap).where(OrderSignalMap.order_id == order.order_id)
        ).scalars().all()
        if not mappings:
            logger.warning(
                "order %s 无 order_signal_map 记录，跳过拆单更新",
                order.order_id,
            )
            return

        # 拆分
        weights = [m.signal_quantity for m in mappings]
        splits = largest_remainder_split(filled_qty, weights)

        # 对每条 signal 找 instance_id 然后更新虚拟账本
        for m, split_qty in zip(mappings, splits):
            if split_qty == 0:
                continue
            sig = session.get(RawSignal, m.signal_id)
            if sig is None:
                logger.warning(
                    "signal_id=%s 在 raw_signals 中不存在，跳过虚拟账本更新",
                    m.signal_id,
                )
                continue

            inst = session.get(InstanceState, sig.instance_id)
            if inst is None:
                logger.warning(
                    "instance_id=%s 在 instance_state 中不存在，自动创建",
                    sig.instance_id,
                )
                inst = InstanceState(
                    instance_id=sig.instance_id,
                    virtual_cash=0.0,
                    virtual_positions={},
                    last_update=_now_iso(),
                )
                session.add(inst)
                # 必须 flush 才能 get 出来
                session.flush()

            # 复制 dict（SQLAlchemy mutable JSON 需要新对象）
            positions = dict(inst.virtual_positions or {})
            sym = order.symbol
            gross = filled_price * split_qty
            fees = self._calc_fees(gross, order.direction)
            if order.direction == "BUY":
                # BUY 总现金消耗 = 成交金额 + 佣金
                total_cost = gross + fees
                # 防穿仓：现金不够就拒绝（避免重复 fill 把虚拟账本扣穿）
                if inst.virtual_cash < total_cost:
                    # Bug C 修复：升级为 ERROR 级日志 + 标记 order 状态，方便对账
                    logger.error(
                        "BOOKKEEPING_DIVERGENCE: instance %s 现金不足拒绝 BUY fill: "
                        "cash=%.2f < total_cost=%.2f (gross=%.2f fees=%.2f) "
                        "sym=%s qty=%d order=%s — QMT 真账户已 FILL，请人工对账",
                        sig.instance_id, inst.virtual_cash, total_cost, gross, fees,
                        sym, split_qty, order.order_id,
                    )
                    _flag_bookkeeping_divergence(order)
                    continue
                inst.virtual_cash = inst.virtual_cash - total_cost
                positions[sym] = positions.get(sym, 0) + split_qty
            else:  # SELL
                # SELL 净收入 = 成交金额 - 佣金 - 印花税
                net_proceeds = gross - fees
                # 防超卖：持仓不够就拒绝
                cur_qty = positions.get(sym, 0)
                if cur_qty < split_qty:
                    logger.error(
                        "BOOKKEEPING_DIVERGENCE: instance %s 持仓不足拒绝 SELL fill: "
                        "holding=%d < sell=%d sym=%s order=%s — "
                        "QMT 真账户已 FILL，请人工对账",
                        sig.instance_id, cur_qty, split_qty, sym, order.order_id,
                    )
                    _flag_bookkeeping_divergence(order)
                    continue
                inst.virtual_cash = inst.virtual_cash + net_proceeds
                new_qty = cur_qty - split_qty
                if new_qty <= 0:
                    positions.pop(sym, None)
                else:
                    positions[sym] = new_qty
            inst.virtual_positions = positions
            inst.last_update = _now_iso()
