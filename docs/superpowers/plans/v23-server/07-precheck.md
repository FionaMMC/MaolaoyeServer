# Plan 07: Precheck — 虚拟资金/持仓/手数预检

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 在原始信号写入 raw_signals 表前做安全网检查，把不合规信号标记 `FAIL` 并附原因；后续归集只取 `PASS` 的信号。

**Architecture:** 纯函数式 `PrecheckService`：输入 `RawSignal + 虚拟账本`，输出 `PrecheckResult(status, reason)`。无 I/O，无 DB 依赖（落地由调用方做）。手续费率从 Settings 注入，默认 0.001（万一）。

**规则（来自 v2.1 项目设计文档 §模块四 预检查）:**

| 检查项 | BUY | SELL |
|---|---|---|
| 手数 | quantity > 0 且为 100 整数倍 | quantity > 0；若 quantity < virtual_position 则必须 100 整数倍（清仓尾单除外） |
| 资金/持仓 | quantity × limit_price × (1 + fee) ≤ virtual_cash | quantity ≤ virtual_positions.get(symbol, 0) |

**Files:**
- `v2.3/server/app/services/precheck.py` (NEW)
- `v2.3/server/tests/unit/test_precheck.py` (NEW)

---

## Task 1: PrecheckService + 单测

### `app/services/precheck.py`

```python
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
        """fee_rate 默认 0.1% (万一手续费 + 印花税预留)。"""
        if fee_rate < 0:
            raise ValueError("fee_rate must be >= 0")
        self.fee_rate = fee_rate

    def check(
        self,
        signal: RawSignal,
        virtual_cash: float,
        virtual_positions: dict[str, int],
    ) -> PrecheckResult:
        """对单条信号做检查，返回 PASS / FAIL + reason。"""
        if signal.direction == "BUY":
            return self._check_buy(signal, virtual_cash)
        else:
            return self._check_sell(signal, virtual_positions)

    # ── BUY ──────────────────────────────────────────────────────────
    def _check_buy(self, signal: RawSignal, virtual_cash: float) -> PrecheckResult:
        # 手数：BUY 必为 100 整数倍
        if signal.quantity % 100 != 0:
            return PrecheckResult(
                status="FAIL",
                reason=f"BUY quantity={signal.quantity} 不是 100 整数倍",
            )

        # 资金（含手续费）
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

    # ── SELL ─────────────────────────────────────────────────────────
    def _check_sell(
        self,
        signal: RawSignal,
        virtual_positions: dict[str, int],
    ) -> PrecheckResult:
        held = virtual_positions.get(signal.symbol, 0)

        # 持仓
        if signal.quantity > held:
            return PrecheckResult(
                status="FAIL",
                reason=(
                    f"SELL position 不足: 持仓 {held}, 卖出 {signal.quantity}"
                ),
            )

        # 手数：clearance（quantity == held 清仓尾单）允许非 100 整数倍
        if signal.quantity < held and signal.quantity % 100 != 0:
            return PrecheckResult(
                status="FAIL",
                reason=(
                    f"SELL quantity={signal.quantity} 不是 100 整数倍且非清仓"
                ),
            )

        return PrecheckResult(status="PASS")
```

### `tests/unit/test_precheck.py`

```python
"""PrecheckService 单元测试"""
import pytest

from app.services.precheck import PrecheckResult, PrecheckService
from app.strategy.base import RawSignal


def _buy(qty: int = 100, price: float = 10.0, offset: float = 0.005) -> RawSignal:
    return RawSignal(symbol="600519.SH", direction="BUY", quantity=qty,
                     reference_price=price, price_offset=offset)


def _sell(qty: int = 100, price: float = 10.0, offset: float = -0.005,
          symbol: str = "600519.SH") -> RawSignal:
    return RawSignal(symbol=symbol, direction="SELL", quantity=qty,
                     reference_price=price, price_offset=offset)


# ── 构造 ────────────────────────────────────────────────────────────────
def test_invalid_fee_rate_raises():
    with pytest.raises(ValueError, match="fee_rate"):
        PrecheckService(fee_rate=-0.01)


# ── BUY ─────────────────────────────────────────────────────────────────
def test_buy_pass_with_enough_cash():
    svc = PrecheckService(fee_rate=0.0)
    r = svc.check(_buy(qty=100, price=10.0, offset=0.005),
                  virtual_cash=10000.0, virtual_positions={})
    assert r.status == "PASS"
    assert r.reason is None


def test_buy_fail_insufficient_cash():
    svc = PrecheckService(fee_rate=0.001)
    # 需要 100 * 10.05 * 1.001 = ~1006，给 1000
    r = svc.check(_buy(qty=100, price=10.0, offset=0.005),
                  virtual_cash=1000.0, virtual_positions={})
    assert r.status == "FAIL"
    assert "cash" in (r.reason or "")


def test_buy_fail_quantity_not_100_multiple():
    svc = PrecheckService(fee_rate=0.0)
    r = svc.check(_buy(qty=150, price=10.0),
                  virtual_cash=10_000_000.0, virtual_positions={})
    assert r.status == "FAIL"
    assert "100 整数倍" in (r.reason or "")


def test_buy_includes_fee_in_cost():
    """手续费要计入资金检查。"""
    svc = PrecheckService(fee_rate=0.001)
    # cost = 100 * 10 * 1.001 = 1001。给 1000.5 应该 FAIL。
    r = svc.check(_buy(qty=100, price=10.0, offset=0.0),
                  virtual_cash=1000.5, virtual_positions={})
    assert r.status == "FAIL"


def test_buy_offset_increases_cost():
    """price_offset 应该被算进 limit_price。"""
    svc = PrecheckService(fee_rate=0.0)
    # limit = 10 * 1.005 = 10.05；cost = 100 * 10.05 = 1005
    # 给 1004 应该 FAIL，给 1005 应该 PASS
    r_fail = svc.check(_buy(qty=100, price=10.0, offset=0.005),
                       virtual_cash=1004.0, virtual_positions={})
    r_pass = svc.check(_buy(qty=100, price=10.0, offset=0.005),
                       virtual_cash=1005.0, virtual_positions={})
    assert r_fail.status == "FAIL"
    assert r_pass.status == "PASS"


# ── SELL ────────────────────────────────────────────────────────────────
def test_sell_pass_with_enough_position():
    svc = PrecheckService(fee_rate=0.0)
    r = svc.check(_sell(qty=100),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 200})
    assert r.status == "PASS"


def test_sell_fail_insufficient_position():
    svc = PrecheckService()
    r = svc.check(_sell(qty=200),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 100})
    assert r.status == "FAIL"
    assert "position" in (r.reason or "")


def test_sell_fail_no_position_at_all():
    svc = PrecheckService()
    r = svc.check(_sell(qty=100),
                  virtual_cash=0.0, virtual_positions={})
    assert r.status == "FAIL"


def test_sell_clearance_allows_non_100_multiple():
    """清仓尾单：quantity == held 时允许非整百。"""
    svc = PrecheckService()
    r = svc.check(_sell(qty=350),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 350})
    assert r.status == "PASS"


def test_sell_partial_must_be_100_multiple():
    """非清仓的 SELL 必须 100 整数倍。"""
    svc = PrecheckService()
    r = svc.check(_sell(qty=150),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 350})
    assert r.status == "FAIL"
    assert "100 整数倍" in (r.reason or "")


def test_sell_partial_100_multiple_passes():
    svc = PrecheckService()
    r = svc.check(_sell(qty=200),
                  virtual_cash=0.0, virtual_positions={"600519.SH": 350})
    assert r.status == "PASS"
```

### 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v   # 期望 79 + 12 = 91 PASS
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/services/precheck.py \
        v2.3/server/tests/unit/test_precheck.py
git commit -m "feat(server): add PrecheckService (cash/position/lot rules)"
```

---

## 收尾

- [ ] 91 PASS
- [ ] 1 commit

---

## 后续 plan

Plan 08: aggregate（同账户组同标的同方向归集为一条订单）
