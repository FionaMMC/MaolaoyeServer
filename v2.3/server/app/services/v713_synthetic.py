"""Read-only synthetic V713 ledger over two live legs.

Design: docs/V713_STOCK_LEG_SYNTHETIC_DESIGN_20261023.md section 6. It holds units of
the stock leg and of Hydra at each leg's flow-adjusted unit NAV and moves them only at
session closes. It never creates targets or orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

from app.oms.v713_stock_leg import STATE_STOCK_WEIGHT

AUX = "AUX_HYDRA"
CHINA = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class SyntheticPolicy:
    policy_id: str
    stock_instance: str
    hydra_instance: str
    label: str
    base_value: float = 400_000.0
    cost_bps: tuple = (0.0, 20.0, 50.0)
    primary_bps: float = 20.0
    liquidated_stock_weight: float = 0.01
    review_after_sessions: int = 10


@dataclass(frozen=True)
class MonthState:
    signal_date: str
    sleeve: str
    anchor_date: str  # the close at which this month's move applies: its first execution session


def load_synthetic_policy(path) -> SyntheticPolicy:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    policy = SyntheticPolicy(**{**payload, "cost_bps": tuple(float(b) for b in payload["cost_bps"])})
    if not policy.policy_id.startswith("V713_SYNTHETIC_"):
        raise ValueError("synthetic policy requires a versioned V713_SYNTHETIC_ policy_id")
    if policy.primary_bps not in policy.cost_bps or policy.base_value <= 0:
        raise ValueError("the primary cost must be a reported line and the base positive")
    if not 0 < policy.liquidated_stock_weight < .05 or policy.review_after_sessions < 1:
        raise ValueError("invalid liquidation completion rule")
    return policy


def unit_nav(snapshots) -> dict[str, float]:
    """Chain flow-adjusted daily returns (``PerfSnapshot.daily_return``); the first snapshot is 1."""
    units, value = {}, None
    for date, daily_return in sorted(snapshots):
        if value is None:
            value = 1.0
        elif daily_return is None or not math.isfinite(daily_return) or daily_return <= -1:
            raise ValueError(f"missing or invalid daily return on {date}")
        else:
            value *= 1 + daily_return
        units[date] = value
    return units


def rebalance(values, cash, stock_weight, rates):
    """Self-financing two-leg move with the Oct-16 overlay convention.

    Sells first, each side at its own extra rate; buys are scaled together when the
    net sale proceeds cannot fund them. Returns (values, cash, cost, traded).
    """
    values = list(values)
    before = cash + values[0] + values[1]
    delta = [stock_weight * before - values[0], (1 - stock_weight) * before - values[1]]
    cost = traded = 0.0
    for i in range(2):
        if delta[i] < 0:
            sale = -delta[i]
            values[i] -= sale
            cash += sale * (1 - rates[i])
            cost += sale * rates[i]
            traded += sale
    need = sum(delta[i] * (1 + rates[i]) for i in range(2) if delta[i] > 0)
    scale = min(1.0, max(0.0, cash) / need) if need else 0.0
    for i in range(2):
        if delta[i] > 0:
            purchase = delta[i] * scale
            values[i] += purchase
            cash -= purchase * (1 + rates[i])
            cost += purchase * rates[i]
            traded += purchase
    return values, cash, cost, traded


def run_synthetic(policy: SyntheticPolicy, *, dates, unit_stock, unit_hydra, exposure, months, bps,
                  open_sell_dates=frozenset(), all_virtual=False) -> dict:
    """Daily synthetic path at one overlay cost.

    ``exposure`` maps a date to the stock leg's stock market value / NAV and
    ``open_sell_dates`` holds the closes at which a stock-leg sell order was not yet
    terminal; AUX entry needs both a liquidated leg and no open sell. With
    ``all_virtual`` (research replay) every move charges both sides; otherwise a leg
    that trades for real is not charged again.
    """
    rate = bps / 10_000
    months = sorted(months, key=lambda month: month.signal_date)
    for month in months:
        if month.sleeve not in STATE_STOCK_WEIGHT:
            raise ValueError(f"unknown sleeve {month.sleeve}")
    dates = sorted(dates)
    if not months:
        return {"status": "WAITING_START", "rows": [], "events": []}
    if months[0].anchor_date not in dates:
        return {"status": "WAITING_LEG_NAV", "rows": [], "events": []}
    first, pending = months[0], months[1:]
    start = first.anchor_date
    weight = STATE_STOCK_WEIGHT[first.sleeve]
    units = [weight * policy.base_value / unit_stock[start], (1 - weight) * policy.base_value / unit_hydra[start]]
    cash, sleeve, status = 0.0, first.sleeve, "CURRENT"
    events = [{"date": start, "kind": "INIT", "signal_date": first.signal_date, "sleeve": first.sleeve,
               "to_stock_weight": weight, "traded": 0.0, "cost": 0.0,
               "value_before": policy.base_value, "value_after": policy.base_value}]
    rows, waiting = [], None

    def move(date, month, kind, rates):
        nonlocal units, cash, sleeve
        values = [units[0] * unit_stock[date], units[1] * unit_hydra[date]]
        before = cash + values[0] + values[1]
        values, cash, cost, traded = rebalance(values, cash, STATE_STOCK_WEIGHT[month.sleeve], rates)
        units = [values[0] / unit_stock[date], values[1] / unit_hydra[date]]
        events.append({"date": date, "kind": kind, "signal_date": month.signal_date, "sleeve": month.sleeve,
                       "to_stock_weight": STATE_STOCK_WEIGHT[month.sleeve], "traded": traded, "cost": cost,
                       "value_before": before, "value_after": cash + values[0] + values[1]})
        sleeve = month.sleeve

    for date in dates[dates.index(start):]:
        if date != start:
            due = pending[0] if pending and pending[0].anchor_date <= date else None
            if due is not None and due.anchor_date != date:
                raise ValueError(f"leg NAV missing on anchor date {due.anchor_date}")
            if due is not None and waiting is not None:
                status = "REVIEW_REQUIRED"
                events.append({"date": date, "kind": "STOPPED", "signal_date": due.signal_date,
                               "sleeve": due.sleeve})
                break
            if due is not None:
                pending.pop(0)
                if due.sleeve == AUX and sleeve == AUX:
                    pass
                elif due.sleeve == AUX and not all_virtual:
                    waiting = [due, 0]
                elif sleeve == AUX and not all_virtual:
                    move(date, due, "EXIT_AUX", (0.0, rate))
                else:
                    move(date, due, "VIRTUAL", (rate, rate))
            if waiting is not None:
                if date not in exposure:
                    raise ValueError(f"stock-leg valuation missing on {date}")
                if exposure[date] <= policy.liquidated_stock_weight and date not in open_sell_dates:
                    move(date, waiting[0], "ENTER_AUX", (0.0, rate))
                    waiting = None
                else:
                    waiting[1] += 1
                    if waiting[1] == policy.review_after_sessions + 1:
                        status = "REVIEW_REQUIRED"
                        events.append({"date": date, "kind": "REVIEW_REQUIRED",
                                       "signal_date": waiting[0].signal_date, "sleeve": waiting[0].sleeve})
        values = [units[0] * unit_stock[date], units[1] * unit_hydra[date]]
        value = cash + values[0] + values[1]
        rows.append({"date": date, "sleeve": sleeve, "unit_stock": unit_stock[date],
                     "unit_hydra": unit_hydra[date], "stock_units": units[0], "hydra_units": units[1],
                     "cash": cash, "value": value, "stock_weight": values[0] / value,
                     "pending": None if waiting is None else "ENTER_AUX"})
    return {"status": status, "rows": rows, "events": events}


def _china_date(stamp: str) -> str:
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (moment if moment.tzinfo is None else moment.astimezone(CHINA)).strftime("%Y%m%d")


def load_inputs(session_factory, policy: SyntheticPolicy) -> dict:
    """Leg unit NAVs, stock-leg exposure, open sells and monthly states from the live database.

    A sell counts as finished at a close only if it is terminal now and became terminal
    by the end of that day, so a later run never moves an AUX switch earlier.
    """
    from sqlalchemy import select
    from app.models import PerfSnapshot, PerfValuation
    from app.oms.models import OmsCycle, OmsOrder, V713StateRecord
    from app.oms.states import TERMINAL

    terminal = {state.value for state in TERMINAL}
    with session_factory() as session:
        def snapshots(instance_id):
            return [(row.date, row.daily_return) for row in session.scalars(select(PerfSnapshot).where(
                PerfSnapshot.instance_id == instance_id, PerfSnapshot.execution_domain == "live"))]

        unit_stock = unit_nav(snapshots(policy.stock_instance))
        unit_hydra = unit_nav(snapshots(policy.hydra_instance))
        exposure = {row.date: max(0.0, (row.nav - row.cash - row.dividend_receivable) / row.nav)
                    for row in session.scalars(select(PerfValuation).where(
                        PerfValuation.instance_id == policy.stock_instance,
                        PerfValuation.execution_domain == "live"))}
        sells = [(order.trade_date, order.state in terminal and _china_date(order.updated_at))
                 for order in session.scalars(select(OmsOrder).join(
                     OmsCycle, OmsCycle.cycle_id == OmsOrder.cycle_id).where(
                     OmsCycle.instance_id == policy.stock_instance, OmsOrder.side == "SELL"))]
        months = []
        for record in session.scalars(select(V713StateRecord).where(
                V713StateRecord.instance_id == policy.stock_instance).order_by(V713StateRecord.signal_date)):
            anchor = record.decision_date
            if record.target_version_id is not None:
                cycle = session.scalars(select(OmsCycle).where(
                    OmsCycle.target_version_id == record.target_version_id)).one()
                anchor = cycle.schedule[0]["trade_date"]
            months.append(MonthState(record.signal_date, record.sleeve, anchor))
    dates = sorted(set(unit_stock) & set(unit_hydra))
    open_sell_dates = {date for date in dates for traded, finished in sells
                       if traded <= date and not (finished and finished <= date)}
    return {"dates": dates, "unit_stock": unit_stock, "unit_hydra": unit_hydra, "exposure": exposure,
            "open_sell_dates": open_sell_dates, "months": months}
