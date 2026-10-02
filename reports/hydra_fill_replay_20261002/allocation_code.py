"""Server-owned fallback planning; never mutate a frozen client batch.

Old share targets remain the audit ceiling. Fresh weights can reduce a residual
but cannot reverse a completed leg or add exposure beyond the approved target.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


POLICY_ID = "PROPORTIONAL_RESIDUAL_V1"


def allocation_policy(priorities):
    return {"policy_id": POLICY_ID, "buy_priorities": dict(priorities),
            "queue_order": "PRIORITY_THEN_REMAINING_NOTIONAL",
            "budget_source": "SETTLED_STRATEGY_CASH",
            "fee_reserve_bps": 10, "min_commission": 5}


def _number(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Allocation inputs must be finite and nonnegative")
    return number


def _cost(order, quantity):
    if not quantity:
        return 0
    notional = _number(order["limit_price"]) * quantity
    return int(((notional + max(Decimal(5), notional / 1000)) * 100)
               .to_integral_value(rounding=ROUND_CEILING))


def allocate_buys(orders, cash, lot_size, priorities):
    """Priority tiers, proportional lots within a tier, then largest remainder.

    Integer-cent budgeting includes each nonzero order's minimum commission.
    The final one-lot pass makes sub-lot proportional shares useful without
    concentrating a multi-lot budget in the alphabetically first symbol.
    """
    budget = int((_number(cash) * 100).to_integral_value(rounding=ROUND_FLOOR))
    if not isinstance(lot_size, int) or isinstance(lot_size, bool) or lot_size <= 0:
        raise ValueError("Invalid lot size")
    if len({o["symbol"] for o in orders}) != len(orders):
        raise ValueError("Duplicate allocation symbol")
    default_priority = max(priorities.values(), default=0) + 1
    result = []
    tiers = sorted({priorities.get(o["symbol"], default_priority) for o in orders})
    for tier in tiers:
        group = sorted((dict(o) for o in orders
                        if priorities.get(o["symbol"], default_priority) == tier),
                       key=lambda o: o["symbol"])
        for order in group:
            if (order["direction"] != "BUY" or type(order["quantity"]) is not int
                    or order["quantity"] <= 0 or order["quantity"] % lot_size
                    or _number(order["limit_price"]) <= 0):
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


def replan_residual(*, target_shares, weights, cash_buffer_weight,
                    actual_positions, actual_cash, prices, anchors, lot_size,
                    priorities, blocked_symbols):
    cash = _number(actual_cash)
    marks = {code: _number(price) for code, price in prices.items()}
    if any(price <= 0 for price in marks.values()):
        raise ValueError("Execution marks must be positive")
    nav = cash + sum(marks[code] * qty for code, qty in actual_positions.items())
    buffer = nav * _number(cash_buffer_weight)
    investable = max(Decimal(0), nav - buffer)
    budget = max(Decimal(0), cash - buffer)
    deferred = {}
    sells, buys = [], []
    fresh_targets = {}
    for code, original_target in sorted(target_shares.items()):
        held = actual_positions.get(code, 0)
        delta = original_target - held
        if not delta:
            continue
        anchor = _number(anchors[code])
        if anchor <= 0:
            raise ValueError("Execution anchor must be positive")
        direction = "BUY" if delta > 0 else "SELL"
        raw_limit = anchor * (Decimal("1.005") if delta > 0 else Decimal("0.995"))
        rounding = ROUND_FLOOR if delta > 0 else ROUND_CEILING
        limit = (raw_limit * 1000).to_integral_value(rounding=rounding) / 1000
        if code in blocked_symbols:
            deferred[code] = "SUSPENDED_OR_NO_VOLUME"
            continue
        if (delta > 0 and marks[code] > limit) or (delta < 0 and marks[code] < limit):
            deferred[code] = "PRICE_GUARD"
            continue
        # Fresh marked NAV includes unsold holdings, but the cash budget does not.
        fresh = int(investable * _number(weights.get(code, 0))
                    / (marks[code] * Decimal("1.006005")) / lot_size) * lot_size
        fresh_targets[code] = fresh
        quantity = min(delta, max(0, fresh - held)) if delta > 0 else min(-delta, max(0, held - fresh))
        if delta > 0:
            quantity = quantity // lot_size * lot_size
        if not quantity:
            deferred[code] = "CASH_SCALED" if delta > 0 and not held and not fresh else "WEIGHT_CAP"
            continue
        row = dict(symbol=code, direction=direction, quantity=quantity,
                   reference_price=float(anchor), limit_price=float(limit))
        (buys if delta > 0 else sells).append(row)
    allocated = allocate_buys(buys, budget, lot_size, priorities)
    allocated_quantities = {o["symbol"]: o["quantity"] for o in allocated}
    for order in buys:
        if allocated_quantities.get(order["symbol"], 0) < order["quantity"]:
            deferred[order["symbol"]] = "CASH_SCALED"
    audit = {"policy_id": POLICY_ID, "actual_cash": float(cash),
             "cash_buffer": float(buffer), "cash_budget": float(budget),
             "buy_reserved_cash": sum(_cost(o, o["quantity"]) for o in allocated) / 100,
             "fresh_target_shares": fresh_targets, "deferred_symbols": deferred}
    return sorted(sells + allocated, key=lambda o: o["symbol"]), audit
