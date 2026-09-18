"""Read-only, instance-scoped Hydra operations facts; no process or broker probes."""
from sqlalchemy import select
from app.models import InstanceState, HydraMonthlyCycle, HydraExecutionPlan
from app.services.hydra_monthly import RESEARCH_COMMIT


def hydra_dashboard_status(sf, settings, instance_id):
    if instance_id != settings.hydra_monthly_instance_id:
        return None
    with sf() as session:
        state = session.get(InstanceState, instance_id)
        if state is None or state.execution_domain != "live":
            return None
        cycle = session.scalar(select(HydraMonthlyCycle).where(
            HydraMonthlyCycle.instance_id == instance_id,
            HydraMonthlyCycle.account_alias == state.account_alias,
            HydraMonthlyCycle.execution_domain == "live",
        ).order_by(HydraMonthlyCycle.as_of_date.desc()).limit(1))
        plan = session.scalar(select(HydraExecutionPlan).where(
            HydraExecutionPlan.instance_id == instance_id,
            HydraExecutionPlan.account_alias == state.account_alias,
            HydraExecutionPlan.execution_domain == "live",
        ).order_by(HydraExecutionPlan.created_at.desc()).limit(1))
        configured = settings.hydra_monthly_enabled and state.account_alias == settings.hydra_monthly_account_alias
        return {
            "name": "Hydra 4.8 · v48.1-RB", "instance_id": instance_id,
            "research_commit": RESEARCH_COMMIT,
            "ledger_mode": state.ledger_mode, "cash": state.virtual_cash,
            "positions": dict(state.virtual_positions or {}), "ledger_updated_at": state.last_update,
            "monthly_configured": configured,
            "monthly_status": cycle.status if cycle else "NO_MONTHLY_INPUT",
            "monthly_as_of": cycle.as_of_date if cycle else None,
            "plan_status": plan.status if plan else None,
            "plan_as_of": plan.request_payload.get("as_of_date") if plan else None,
            "generation_enabled": settings.live_order_generation_enabled,
            "delivery_enabled": settings.live_order_delivery_enabled,
            # No durable Windows receipt/heartbeat is available here.
            "client_readiness": "UNKNOWN",
        }
