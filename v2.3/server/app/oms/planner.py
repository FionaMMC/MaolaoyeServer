"""Pure two-phase execution planner (policy C3). No database, no network.

Shared by the live execution core and reports/hydra_policy_20261002/policy_replay.py
so the backtest and production compute identical lot targets, limits and orders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date, time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
from typing import Mapping, Sequence

from app.oms.instruments import (InstrumentSpec, buy_cost, floor_buy_quantity, hydra_instrument,
                                 instrument_from_dict, instrument_to_dict, next_buy_quantity)

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
    strategy_id: str = "hydra"
    require_instrument_specs: bool = False
    instruments: dict = field(default_factory=dict)
    execution_model: str = "close_sell_next_adjacent_open"
    execution_windows: dict = field(default_factory=lambda: {
        "BUY": ["09:14:00", "09:16:00"], "SELL": ["14:56:00", "15:00:00"]})
    submit_at: dict = field(default_factory=lambda: {"BUY": "09:15:05", "SELL": "14:57:05"})

    def __post_init__(self):
        if not self.policy_id or not self.strategy_id:
            raise ValueError("policy_id and strategy_id are required")
        if self.lot not in ("floor", "nearest"):
            raise ValueError("lot must be floor or nearest")
        for name in ("size_factor", "default_buy_bps", "reserve"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError(f"invalid policy {name}")
        if self.size_factor < 1 or not 0 <= self.reserve < 1 or not 0 <= self.default_buy_bps < 10000:
            raise ValueError("invalid policy sizing or price band")
        if type(self.window) is not int or not 1 <= self.window <= 60:
            raise ValueError("execution window must be 1..60 trading sessions")
        if type(self.require_instrument_specs) is not bool:
            raise ValueError("require_instrument_specs must be boolean")
        bands = list(self.sell_schedule) + [band for _, band in self.buy_bps]
        if not self.sell_schedule or any(isinstance(b, bool) or not isinstance(b, (int, float))
                                         or not math.isfinite(b) or not 0 <= b < 10000 for b in bands):
            raise ValueError("invalid policy execution bands")
        if len(dict(self.buy_bps)) != len(self.buy_bps):
            raise ValueError("duplicate symbol buy band")
        if self.execution_model != "close_sell_next_adjacent_open":
            raise ValueError("unsupported execution model")
        for symbol, spec in self.instruments.items():
            if not isinstance(symbol, str) or not symbol:
                raise ValueError("invalid instrument symbol")
            instrument_from_dict(spec)
        if set(self.execution_windows) != {"BUY", "SELL"} or set(self.submit_at) != {"BUY", "SELL"}:
            raise ValueError("BUY and SELL execution windows and submit_at required")
        for side in ("BUY", "SELL"):
            bounds = self.execution_windows[side]
            if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
                raise ValueError("execution window requires start and end")
            start, end = (time.fromisoformat(x) for x in bounds)
            submit = time.fromisoformat(self.submit_at[side])
            if start.tzinfo or end.tzinfo or submit.tzinfo or not start <= submit < end:
                raise ValueError("submit time must fall inside its local execution window")

    def buy_band(self, symbol: str) -> float:
        return dict(self.buy_bps).get(symbol, self.default_buy_bps)

    def sell_band(self, attempt: int) -> float:
        return self.sell_schedule[min(attempt, len(self.sell_schedule) - 1)]


def policy_to_dict(policy: Policy) -> dict:
    return asdict(policy)


def policy_from_dict(payload: Mapping) -> Policy:
    if not isinstance(payload, Mapping):
        raise ValueError("policy must be an object")
    unknown = set(payload) - {f.name for f in fields(Policy)}
    if unknown:
        raise ValueError(f"unknown policy fields: {sorted(unknown)}")
    data = dict(payload)
    if "buy_bps" in data:
        raw = data["buy_bps"]
        data["buy_bps"] = tuple(tuple(x) for x in (raw.items() if isinstance(raw, Mapping) else raw))
    if "sell_schedule" in data:
        data["sell_schedule"] = tuple(data["sell_schedule"])
    # Copy nested dictionaries: caller mutations must not change a frozen policy.
    if "instruments" in data:
        data["instruments"] = {s: instrument_to_dict(instrument_from_dict(v)) for s, v in data["instruments"].items()}
    if "execution_windows" in data:
        data["execution_windows"] = {s: list(v) for s, v in data["execution_windows"].items()}
    if "submit_at" in data:
        data["submit_at"] = dict(data["submit_at"])
    return Policy(**data)


def get_instrument(symbol: str, policy: Policy) -> InstrumentSpec:
    specs = getattr(policy, "instruments", {})
    if symbol in specs:
        return instrument_from_dict(specs[symbol])
    if getattr(policy, "require_instrument_specs", False):
        raise ValueError(f"explicit instrument specification required for {symbol}")
    return hydra_instrument(symbol)


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: str
    quantity: int
    limit_price: float
    reference_price: float
    instrument: dict | None = None


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
    if not math.isfinite(float(nav)) or nav <= 0:
        raise ValueError("NAV must be finite and positive")
    if any(isinstance(w, bool) or not math.isfinite(float(w)) or w < 0 for w in weights.values()):
        raise ValueError("weights must be finite and nonnegative")
    if sum(weights.values()) > 1 + 1e-6:
        raise ValueError("weights exceed the available portfolio")
    investable = nav * (1 - policy.reserve)
    specs = {s: get_instrument(s, policy) for s, w in weights.items() if w > 0}
    for s in specs:
        if s not in prices or not math.isfinite(float(prices[s])) or prices[s] <= 0:
            raise ValueError(f"positive signal price required for {s}")
    target = {s: (floor_buy_quantity(investable * w / prices[s] / policy.size_factor, specs[s]) if w > 0 else 0)
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
            increment = next_buy_quantity(target[s], specs[s]) - target[s]
            step = increment * prices[s]
            gain = gap - abs(gap - step)
            if gain > best_gain + 1e-9 and spent + step * policy.size_factor <= investable + 1e-9:
                best, best_gain = s, gain
        if best is None:
            return target
        increment = next_buy_quantity(target[best], specs[best]) - target[best]
        target[best] += increment
        spent += increment * prices[best] * policy.size_factor


def buy_limit(anchor: float, bps: float, tick: float = TICK) -> float:
    if tick == TICK:
        return math.floor(anchor * (1 + bps / 1e4) / TICK + 1e-8) * TICK
    step = _number(tick)
    if step <= 0:
        raise ValueError("tick must be positive")
    value = _number(anchor) * (Decimal(1) + _number(bps) / 10000)
    return float((value / step).to_integral_value(rounding=ROUND_FLOOR) * step)


def sell_limit(anchor: float, bps: float, tick: float = TICK) -> float:
    if tick == TICK:
        return math.ceil(anchor * (1 - bps / 1e4) / TICK - 1e-8) * TICK
    step = _number(tick)
    if step <= 0:
        raise ValueError("tick must be positive")
    value = _number(anchor) * (Decimal(1) - _number(bps) / 10000)
    return float((value / step).to_integral_value(rounding=ROUND_CEILING) * step)


def _number(value) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Allocation inputs must be finite and nonnegative")
    return number


def _cost(order, quantity, spec: InstrumentSpec | None = None) -> int:
    if not quantity:
        return 0
    if spec is not None:
        return int(Decimal(str(buy_cost(spec, order["limit_price"], quantity))) * 100)
    notional = _number(order["limit_price"]) * quantity
    return int(((notional + max(Decimal(5), notional / 1000)) * 100).to_integral_value(rounding=ROUND_CEILING))


def allocate_buys(orders, cash, lot_size, priorities, instruments=None):
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
    specs = {s: (v if isinstance(v, InstrumentSpec) else instrument_from_dict(v))
             for s, v in (instruments or {}).items()}
    if instruments is not None and any(o["symbol"] not in specs for o in orders):
        raise ValueError("missing allocation instrument specification")
    result = []
    for tier in sorted({priorities.get(o["symbol"], default_priority) for o in orders}):
        group = sorted((dict(o) for o in orders if priorities.get(o["symbol"], default_priority) == tier),
                       key=lambda o: o["symbol"])
        for order in group:
            spec = specs.get(order["symbol"])
            step = spec.buy_lot if spec else lot_size
            if (order["direction"] != "BUY" or type(order["quantity"]) is not int or order["quantity"] <= 0
                    or order["quantity"] % step or _number(order["limit_price"]) <= 0
                    or (spec and order["quantity"] < spec.min_buy_quantity)):
                raise ValueError("Invalid buy allocation")
        scale_base = 10**12

        def quantities(scale):
            return [(floor_buy_quantity(o["quantity"] * scale // scale_base, specs[o["symbol"]])
                     if o["symbol"] in specs else o["quantity"] * scale // scale_base // lot_size * lot_size)
                    for o in group]

        def cost(o, q):
            return _cost(o, q, specs.get(o["symbol"]))

        low, high = 0, scale_base
        while low < high:
            middle = (low + high + 1) // 2
            if sum(cost(o, q) for o, q in zip(group, quantities(middle))) <= budget:
                low = middle
            else:
                high = middle - 1
        allocated = quantities(low)
        budget -= sum(cost(o, q) for o, q in zip(group, allocated))
        # One remaining lot per symbol; tie by code for reproducible plans.
        remainders = sorted(range(len(group)), key=lambda i: (
            -(group[i]["quantity"] * low - allocated[i] * scale_base), group[i]["symbol"]))
        for index in remainders:
            order, quantity = group[index], allocated[index]
            if quantity >= order["quantity"]:
                continue
            spec = specs.get(order["symbol"])
            next_quantity = next_buy_quantity(quantity, spec) if spec else quantity + lot_size
            if next_quantity > order["quantity"]:
                continue
            increment = cost(order, next_quantity) - cost(order, quantity)
            if increment <= budget:
                allocated[index] = next_quantity
                budget -= increment
        result.extend({**o, "quantity": q} for o, q in zip(group, allocated) if q)
    return sorted(result, key=lambda o: o["symbol"])


def plan_sell_session(*, target: Mapping[str, int], positions: Mapping[str, int], sellable: Mapping[str, int],
                      sell_anchor: Mapping[str, float], attempt: int, policy: Policy):
    """Sell the overweight part of the frozen target, never more than is sellable now."""
    band = policy.sell_band(attempt)
    orders, deferrals = [], []
    for s in sorted(set(target) | set(positions)):
        held = int(positions.get(s, 0))
        excess = held - int(target.get(s, 0))
        if excess <= 0:
            continue
        spec = get_instrument(s, policy)
        can = max(0, min(excess, int(sellable.get(s, 0))))
        if can < excess:
            deferrals.append(Deferral(s, "SELL", excess - can, "NOT_SELLABLE"))
        legal = can if spec.odd_lot_sell_all and can == held else can // spec.sell_lot * spec.sell_lot
        if legal < held and legal < spec.min_sell_quantity:
            legal = 0
        if legal < can:
            deferrals.append(Deferral(s, "SELL", can - legal, "SELL_LOT"))
        if legal > 0:
            anchor = float(sell_anchor[s])
            orders.append(PlannedOrder(s, "SELL", legal, sell_limit(anchor, band, spec.price_tick), anchor,
                                       instrument_to_dict(spec)))
    return orders, deferrals


def plan_buy_session(*, target: Mapping[str, int], positions: Mapping[str, int], cash: float,
                     buy_anchor: Mapping[str, float], policy: Policy, tradable=None):
    """Plan next-session residuals using cash and frozen rules.

    ``tradable`` is accepted for wire/backtest compatibility only. A previous
    closing snapshot's is_trading flag cannot establish next-session suspension;
    the executor must check dated, current market eligibility before submission.
    """
    orders, deferrals, wanted = [], [], []
    specs = {}
    buy_anchor = buy_anchor or {}
    for s in sorted(target):
        residual = max(0, int(target[s]) - int(positions.get(s, 0)))
        if not residual:
            continue
        if s not in buy_anchor:
            # No usable close yet (suspended on the sell day): wait for a later anchor, spend nothing.
            deferrals.append(Deferral(s, "BUY", residual, "NO_ANCHOR"))
            continue
        spec = specs[s] = get_instrument(s, policy)
        need = floor_buy_quantity(residual, spec)
        if need < residual:
            deferrals.append(Deferral(s, "BUY", residual - need, "BUY_LOT"))
        if not need:
            continue
        wanted.append(dict(symbol=s, direction="BUY", quantity=need,
                           limit_price=buy_limit(float(buy_anchor[s]), policy.buy_band(s), spec.price_tick)))
    allocated = ({o["symbol"]: o["quantity"] for o in allocate_buys(wanted, max(0., float(cash)), LOT, {}, specs)}
                 if wanted else {})
    for o in wanted:
        q = allocated.get(o["symbol"], 0)
        if q < o["quantity"]:
            deferrals.append(Deferral(o["symbol"], "BUY", o["quantity"] - q, "CASH"))
        if q:
            orders.append(PlannedOrder(o["symbol"], "BUY", q, o["limit_price"], float(buy_anchor[o["symbol"]]),
                                       instrument_to_dict(specs[o["symbol"]])))
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
