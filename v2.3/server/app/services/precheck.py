"""原始信号预检：虚拟资金/持仓/手数。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.strategy.base import RawSignal

PrecheckStatus = Literal["PASS", "FAIL"]


@dataclass(frozen=True)
class PrecheckResult:
    status: PrecheckStatus
    reason: str | None = None


class PrecheckService:
    """对单条 RawSignal 做合规检查。无 I/O，纯计算。"""

    def __init__(self, fee_rate: float = 0.001):
        if fee_rate < 0:
            raise ValueError("fee_rate must be >= 0")
        self.fee_rate = fee_rate

    def check(
        self,
        signal: RawSignal,
        virtual_cash: float,
        virtual_positions: dict[str, int],
    ) -> PrecheckResult:
        if signal.direction == "BUY":
            return self._check_buy(signal, virtual_cash)
        else:
            return self._check_sell(signal, virtual_positions)

    def _check_buy(self, signal: RawSignal, virtual_cash: float) -> PrecheckResult:
        if signal.quantity % 100 != 0:
            return PrecheckResult(
                status="FAIL",
                reason=f"BUY quantity={signal.quantity} 不是 100 整数倍",
            )

        limit_price = signal.reference_price * (1 + signal.price_offset)
        cost = signal.quantity * limit_price * (1 + self.fee_rate)
        if cost > virtual_cash:
            return PrecheckResult(
                status="FAIL",
                reason=(
                    f"BUY cash 不足: 需 {cost:.2f}（含手续费），仅有 {virtual_cash:.2f}"
                ),
            )

        return PrecheckResult(status="PASS")

    def _check_sell(
        self,
        signal: RawSignal,
        virtual_positions: dict[str, int],
    ) -> PrecheckResult:
        held = virtual_positions.get(signal.symbol, 0)

        if signal.quantity > held:
            return PrecheckResult(
                status="FAIL",
                reason=(
                    f"SELL position 不足: 持仓 {held}, 卖出 {signal.quantity}"
                ),
            )

        if signal.quantity < held and signal.quantity % 100 != 0:
            return PrecheckResult(
                status="FAIL",
                reason=(
                    f"SELL quantity={signal.quantity} 不是 100 整数倍且非清仓"
                ),
            )

        return PrecheckResult(status="PASS")
