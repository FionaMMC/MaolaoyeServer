"""Account-wide ownership and reservations shared by automatic and operator orders.

Broker balances remain account facts. Strategy balances are never inferred from
them. All intent creation uses the same short account transaction boundary.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models import InstanceState, Order, OrderSignalMap, RawSignal
from app.oms.models import OmsCycle, OmsOrder
from app.oms.states import TERMINAL


def order_instance_id(session, order: OmsOrder) -> str | None:
    cycle = session.get(OmsCycle, order.cycle_id)
    if cycle is not None:
        return cycle.instance_id
    return session.execute(select(RawSignal.instance_id).join(
        OrderSignalMap, OrderSignalMap.signal_id == RawSignal.signal_id
    ).where(OrderSignalMap.order_id == order.client_order_id)).scalar_one_or_none()


def open_reservations(session, account_alias: str, *, exclude: set[str] | None = None) -> dict:
    from app.oms.instruments import buy_cost
    from app.services.order_instruments import explicit_order_instrument
    exclude = exclude or set()
    reservations: dict[str, dict] = {}
    rows = session.execute(select(OmsOrder).where(
        OmsOrder.account_alias == account_alias,
        OmsOrder.state.not_in([state.value for state in TERMINAL]),
    )).scalars().all()
    for order in rows:
        if order.client_order_id in exclude:
            continue
        remaining = max(0, order.quantity - order.filled_qty)
        if not remaining:
            continue
        instance_id = order_instance_id(session, order)
        if instance_id is None:
            raise ValueError(f"unattributed OMS order {order.client_order_id}")
        grant = reservations.setdefault(instance_id, {"cash": 0.0, "sellable": {}, "order_ids": []})
        grant["order_ids"].append(order.client_order_id)
        if order.side == "SELL":
            grant["sellable"][order.symbol] = grant["sellable"].get(order.symbol, 0) + remaining
        else:
            legacy = session.get(Order, order.client_order_id)
            spec = explicit_order_instrument(session, legacy) if legacy is not None else None
            if spec is not None:
                cost = buy_cost(spec, order.limit_price, remaining, reserve=True)
            else:
                # Existing Hydra intents predate explicit instrument metadata.
                cost = remaining * order.limit_price * 1.001 + 5.0
            grant["cash"] += cost
    return reservations


def instance_is_enabled(inst: InstanceState) -> bool:
    state = inst.strategy_state or {}
    default = inst.instance_id == "live_hydra_v481_rb"
    return bool(state.get("oms_enabled", default)) and not bool(state.get("oms_paused")) and not bool(
        state.get("accounting_hold"))


def strategy_resources(session, inst: InstanceState, trade_date: str, *, exclude: set[str] | None = None) -> dict:
    from app.services.strategy_sellable import strategy_sellable
    positions = {symbol: int(qty) for symbol, qty in (inst.virtual_positions or {}).items() if int(qty)}
    available = strategy_sellable(session, inst.instance_id, trade_date)
    reservations = open_reservations(session, inst.account_alias, exclude=exclude).get(
        inst.instance_id, {"cash": 0.0, "sellable": {}, "order_ids": []})
    account_orders = session.execute(select(OmsOrder).where(OmsOrder.account_alias == inst.account_alias)).scalars().all()
    orders = [row for row in account_orders if order_instance_id(session, row) == inst.instance_id]
    return {
        "strategy_cash": max(0.0, float(inst.virtual_cash) - reservations["cash"]),
        "strategy_positions": positions,
        "strategy_sellable": {symbol: max(0, qty - reservations["sellable"].get(symbol, 0))
                              for symbol, qty in available.items()},
        "server_reserved_order_ids": sorted(reservations["order_ids"]),
        "server_accounted_fill_quantities": {row.client_order_id: int(row.projected_qty) for row in orders},
        "server_terminal_orders": {row.client_order_id: {
            "state": row.state, "filled_qty": row.filled_qty, "projected_qty": row.projected_qty,
            "trade_date": row.trade_date, "last_observed_at": row.last_observed_at,
        } for row in account_orders if row.state in {state.value for state in TERMINAL}
            and row.projected_status is not None and row.projected_qty == row.filled_qty},
    }
