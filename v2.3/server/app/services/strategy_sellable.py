"""Strategy-owned sellability; broker inventory cannot lend another owner's T+1 shares."""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import InstanceState, Order, OrderSignalMap, RawSignal, Trade
from app.services.settlement import largest_remainder_split


def _day(value, fallback):
    if not value:
        return fallback
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            # Older QMT reports are market-local timestamps.
            stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return stamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    except ValueError:
        return fallback


def strategy_sellable(session, instance_id: str, trade_date: str) -> dict[str, int]:
    """Return owned shares less that strategy's same-day T+1 buys.

    Reconstruct *incremental* allocations using the same remainder rule as
    settlement. Do not split the final cumulative quantity afresh: rounding
    across partial fills otherwise shifts shares between strategies.
    Broker and in-flight reservation caps must still be applied by the caller.
    """
    datetime.strptime(trade_date, "%Y%m%d")
    inst = session.get(InstanceState, instance_id)
    if inst is None:
        raise ValueError("unknown strategy instance")
    positions = {s: int(q) for s, q in (inst.virtual_positions or {}).items() if int(q) > 0}
    orders = session.execute(select(Order).join(OrderSignalMap, OrderSignalMap.order_id == Order.order_id)
        .join(RawSignal, RawSignal.signal_id == OrderSignalMap.signal_id).where(
            RawSignal.instance_id == instance_id, Order.execution_domain == inst.execution_domain,
            Order.qmt_account_alias == inst.account_alias, Order.direction == "BUY")).scalars().unique().all()
    bought = {}
    for order in orders:
        # The buy's frozen instrument rules are the authority even if a newer
        # strategy policy has changed since the acquisition.
        from app.services.order_instruments import explicit_order_instrument
        spec = explicit_order_instrument(session, order)
        if spec is not None and not spec.t_plus_one:
            continue
        mappings = session.execute(select(OrderSignalMap, RawSignal.instance_id)
            .join(RawSignal, RawSignal.signal_id == OrderSignalMap.signal_id)
            .where(OrderSignalMap.order_id == order.order_id).order_by(OrderSignalMap.signal_id)).all()
        trades = session.execute(select(Trade).where(Trade.order_id == order.order_id,
            Trade.execution_domain == inst.execution_domain).order_by(Trade.id)).scalars().all()
        previous = 0
        for trade in trades:
            delta = max(0, int(trade.filled_quantity) - previous)
            previous = max(previous, int(trade.filled_quantity))
            if not delta or _day(trade.filled_time, order.valid_date) != trade_date:
                continue
            split = largest_remainder_split(delta, [m.signal_quantity for m, _ in mappings])
            ours = sum(q for q, (_, owner) in zip(split, mappings) if owner == instance_id)
            bought[order.symbol] = bought.get(order.symbol, 0) + ours
    return {symbol: max(0, quantity - bought.get(symbol, 0)) for symbol, quantity in positions.items()}
