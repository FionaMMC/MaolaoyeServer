"""Pure two-phase execution planner (policy C3). No database, no network.

Shared by the live execution core and reports/hydra_policy_20261002/policy_replay.py
so the backtest and production compute identical lot targets, limits and orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
from typing import Mapping, Sequence

LOT = 100
TICK = .001
POLICY_ID = "HYDRA_TWO_PHASE_C3_V1"


@dataclass(frozen=True)
class Policy:
    policy_id: str = POLICY_ID
    lot: str = "nearest"
    size_factor: float = 1.001
    default_buy_bps: float = 50.
    buy_bps: tuple = (("518880.SH", 100.), ("513100.SH", 150.), ("513500.SH", 150.))
    sell_schedule: tuple = (50., 200.)
    window: int = 3
    reserve: float = 0.

    def buy_band(self, symbol: str) -> float:
        return dict(self.buy_bps).get(symbol, self.default_buy_bps)

    def sell_band(self, attempt: int) -> float:
        return self.sell_schedule[min(attempt, len(self.sell_schedule) - 1)]


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: str
    quantity: int
    limit_price: float
    reference_price: float


@dataclass(frozen=True)
class Deferral:
    symbol: str
    side: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class Session:
    seq: int
    trade_date: str
    phase: str
    sell_attempt: int | None


def lot_target(nav, weights, prices, policy) -> dict[str, int]:
    """Whole-lot target. ``nearest`` adds a lot only while it cuts the error and
    the whole basket, grossed up by ``size_factor``, still fits the budget."""
    investable = nav * (1 - policy.reserve)
    target = {s: (math.floor(investable * w / prices[s] / policy.size_factor / LOT) * LOT if w > 0 else 0)
              for s, w in weights.items()}
    if policy.lot == "floor":
        return target
    if policy.lot != "nearest":
        raise ValueError(policy.lot)
    spent = sum(q * prices[s] for s, q in target.items() if q) * policy.size_factor
    while True:
        best, best_gain = None, 0.
        for s in sorted(weights):
            if weights[s] <= 0:
                continue
            gap = investable * weights[s] - target[s] * prices[s]
            step = LOT * prices[s]
            gain = gap - abs(gap - step)
            if gain > best_gain + 1e-9 and spent + step * policy.size_factor <= investable + 1e-9:
                best, best_gain = s, gain
        if best is None:
            return target
        target[best] += LOT
        spent += LOT * prices[best] * policy.size_factor


def buy_limit(anchor: float, bps: float) -> float:
    return math.floor(anchor * (1 + bps / 1e4) / TICK + 1e-8) * TICK


def sell_limit(anchor: float, bps: float) -> float:
    return math.ceil(anchor * (1 - bps / 1e4) / TICK - 1e-8) * TICK


def _number(value) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Allocation inputs must be finite and nonnegative")
    return number


def _cost(order, quantity) -> int:
    if not quantity:
        return 0
    notional = _number(order["limit_price"]) * quantity
    return int(((notional + max(Decimal(5), notional / 1000)) * 100).to_integral_value(rounding=ROUND_CEILING))


def allocate_buys(orders, cash, lot_size, priorities):
    """Priority tiers, proportional lots within a tier, then largest remainder.

    Verbatim port of reports/hydra_fill_replay_20261002/allocation_code.py; integer-cent
    budgeting includes each nonzero order's minimum commission.
    """
    budget = int((_number(cash) * 100).to_integral_value(rounding=ROUND_FLOOR))
    if not isinstance(lot_size, int) or isinstance(lot_size, bool) or lot_size <= 0:
        raise ValueError("Invalid lot size")
    if len({o["symbol"] for o in orders}) != len(orders):
        raise ValueError("Duplicate allocation symbol")
    default_priority = max(priorities.values(), default=0) + 1
    result = []
    for tier in sorted({priorities.get(o["symbol"], default_priority) for o in orders}):
        group = sorted((dict(o) for o in orders if priorities.get(o["symbol"], default_priority) == tier),
                       key=lambda o: o["symbol"])
        for order in group:
            if (order["direction"] != "BUY" or type(order["quantity"]) is not int or order["quantity"] <= 0
                    or order["quantity"] % lot_size or _number(order["limit_price"]) <= 0):
                raise ValueError("Invalid buy allocation")
        scale_base = 10**12

        def quantities(scale):
            return [o["quantity"] * scale // scale_base // lot_size * lot_size for o in group]

        low, high = 0, scale_base
        while low < high:
            middle = (low + high + 1) // 2
            if sum(_cost(o, q) for o, q in zip(group, quantities(middle))) <= budget:
                low = middle
            else:
                high = middle - 1
        allocated = quantities(low)
        budget -= sum(_cost(o, q) for o, q in zip(group, allocated))
        # One remaining lot per symbol; tie by code for reproducible plans.
        remainders = sorted(range(len(group)), key=lambda i: (
            -(group[i]["quantity"] * low - allocated[i] * scale_base), group[i]["symbol"]))
        for index in remainders:
            order, quantity = group[index], allocated[index]
            if quantity >= order["quantity"]:
                continue
            increment = _cost(order, quantity + lot_size) - _cost(order, quantity)
            if increment <= budget:
                allocated[index] += lot_size
                budget -= increment
        result.extend({**o, "quantity": q} for o, q in zip(group, allocated) if q)
    return sorted(result, key=lambda o: o["symbol"])


def plan_sell_session(*, target: Mapping[str, int], positions: Mapping[str, int], sellable: Mapping[str, int],
                      sell_anchor: Mapping[str, float], attempt: int, policy: Policy):
    """Sell the overweight part of the frozen target, never more than is sellable now."""
    band = policy.sell_band(attempt)
    orders, deferrals = [], []
    for s in sorted(target):
        excess = int(positions.get(s, 0)) - int(target[s])
        if excess <= 0:
            continue
        can = min(excess, int(sellable.get(s, 0)))
        if can < excess:
            deferrals.append(Deferral(s, "SELL", excess - can, "NOT_SELLABLE"))
        if can > 0:
            anchor = float(sell_anchor[s])
            orders.append(PlannedOrder(s, "SELL", can, sell_limit(anchor, band), anchor))
    return orders, deferrals


def plan_buy_session(*, target: Mapping[str, int], positions: Mapping[str, int], cash: float,
                     buy_anchor: Mapping[str, float], tradable, policy: Policy):
    """Buy the remaining frozen-target shares that the actual cash can pay for."""
    orders, deferrals, wanted = [], [], []
    for s in sorted(target):
        need = (int(target[s]) - int(positions.get(s, 0))) // LOT * LOT
        if need <= 0:
            continue
        if s not in tradable:
            deferrals.append(Deferral(s, "BUY", need, "SUSPENDED"))
            continue
        wanted.append(dict(symbol=s, direction="BUY", quantity=need,
                           limit_price=buy_limit(float(buy_anchor[s]), policy.buy_band(s))))
    allocated = ({o["symbol"]: o["quantity"] for o in allocate_buys(wanted, max(0., float(cash)), LOT, {})}
                 if wanted else {})
    for o in wanted:
        q = allocated.get(o["symbol"], 0)
        if q < o["quantity"]:
            deferrals.append(Deferral(o["symbol"], "BUY", o["quantity"] - q, "CASH"))
        if q:
            orders.append(PlannedOrder(o["symbol"], "BUY", q, o["limit_price"], float(buy_anchor[o["symbol"]])))
    return orders, deferrals


def _day(value: str) -> date:
    return date(int(value[:4]), int(value[4:6]), int(value[6:]))


def _adjacent(a: str, b: str) -> bool:
    return (_day(b) - _day(a)).days == 1


def session_schedule(calendar: Sequence[str], signal_date: str, window: int,
                     earliest_sell: str | None = None) -> list[Session]:
    """Close-sell / next-natural-day open-buy sessions; mirrors policy_replay eligibility.

    The window counts trading sessions from the first opening buy. A buy needs the
    previous trading day to be the previous natural day; a sell needs the next one.
    earliest_sell starts a late-published cycle at the first such pair on or after that
    day (a 9/30 signal published on 10/9 starts with a 10/12 sell and a 10/13 buy).
    """
    days = sorted(set(calendar))
    later = [d for d in days if d > signal_date and (earliest_sell is None or d >= earliest_sell)]
    first_sell = next((a for a, b in zip(later, later[1:]) if _adjacent(a, b)), None)
    if first_sell is None:
        raise ValueError("no adjacent trading-day pair after signal")
    i_buy = days.index(first_sell) + 1
    if i_buy + window - 1 >= len(days):
        raise ValueError("calendar shorter than execution window")
    sessions, seq, attempt = [], 0, 0
    for i in range(i_buy - 1, i_buy + window):
        d, age = days[i], i - i_buy + 1
        if 1 <= age <= window and _adjacent(days[i - 1], d):
            seq += 1
            sessions.append(Session(seq, d, "BUY", None))
        nxt = days[i + 1] if i + 1 < len(days) else None
        if age < window and nxt is not None and _adjacent(d, nxt):
            seq += 1
            sessions.append(Session(seq, d, "SELL", attempt))
            attempt += 1
    return sessions
