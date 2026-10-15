"""Resolve the immutable security rules for an already published order."""
from sqlalchemy import select
from app.models import InstanceState, OrderSignalMap, RawSignal
from app.oms.instruments import instrument_from_dict
from app.oms.models import OmsOrder, OmsCycle, OmsManualInstruction


def explicit_order_instrument(session, order):
    oms = session.get(OmsOrder, order.order_id)
    if oms is not None:
        cycle = session.get(OmsCycle, oms.cycle_id)
        policy = cycle.policy if cycle else None
        if cycle is None:
            instruction = session.scalar(select(OmsManualInstruction).where(
                OmsManualInstruction.client_order_id == order.order_id,
                OmsManualInstruction.account_alias == order.qmt_account_alias))
            policy = (instruction.payload or {}).get("policy") if instruction else None
        if policy and policy.get("instruments"):
            payload = policy["instruments"].get(order.symbol)
            if payload is None:
                raise ValueError("INSTRUMENT_FEE_SPEC_MISSING_FROM_FROZEN_POLICY")
            return instrument_from_dict(payload)
    owners = session.execute(select(InstanceState).join(
        RawSignal, RawSignal.instance_id == InstanceState.instance_id).join(
        OrderSignalMap, OrderSignalMap.signal_id == RawSignal.signal_id).where(
        OrderSignalMap.order_id == order.order_id,
        InstanceState.execution_domain == order.execution_domain)).scalars().all()
    specs = [instrument_from_dict(payload) for owner in owners
             if (payload := (owner.strategy_state or {}).get("oms_instruments", {}).get(order.symbol)) is not None]
    if specs and any(spec != specs[0] for spec in specs):
        raise ValueError("INSTRUMENT_FEE_SPEC_CONFLICT")
    return specs[0] if specs else None
