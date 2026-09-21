"""Materialize live-ledger EOD valuations without running a strategy or orders.

Backfill is bounded by the last authoritative ledger update. Today's holdings
must never be projected back before that observation. Older snapshots survive.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import math

from sqlalchemy import select
from app.models import InstanceState, PerfSnapshot, PerfValuation
from app.services.dividend_entitlement import outstanding_dividends
from app.services.ledger_transaction import begin_ledger_transaction
from app.services.perf import PerfService
from app.services.daily_risk import DailyRiskSnapshotService


def materialize_live_performance(sf, store, instance_id, *, now=None, backfill=False):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    today = now.strftime("%Y%m%d")
    probe = store.read("indexes", "000852.SH")
    if probe.empty:
        return {"written": 0, "status": "WAITING_MARKET_DATA"}
    dates = sorted({str(int(d)) for d in probe.trade_date if str(int(d)) < today
                    or (str(int(d)) == today and now.hour >= 16)})
    if not dates:
        return {"written": 0, "status": "WAITING_EOD"}
    written, missing_dates = [], []
    perf = PerfService(sf, store)
    with sf() as session:
        state = session.get(InstanceState, instance_id)
        if state is None or state.execution_domain != "live" or state.ledger_mode != "attributed":
            raise ValueError("live performance requires an attributed live instance")
        observation = (state.last_update, float(state.virtual_cash), dict(state.virtual_positions or {}))
    updated = datetime.fromisoformat(observation[0].replace("Z", "+00:00"))
    if updated.tzinfo is None:
        raise ValueError("ledger update must include timezone")
    lower = updated.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    eligible = [d for d in dates[-31:] if d >= lower]
    if not backfill:
        eligible = eligible[-1:]
    positions = {s: int(q) for s, q in observation[2].items() if q}
    # Filesystem I/O must finish before acquiring SQLite's write lock.
    marks = {}
    for symbol in positions:
        frame = store.read("etfs", symbol)
        if frame.empty:
            frame = store.read("stocks", symbol)
        marks[symbol] = {} if frame.empty else {
            str(int(row.trade_date)): float(row.close) for row in frame.itertuples()
        }
    with sf() as session:
        begin_ledger_transaction(session, "live", None)
        current = session.get(InstanceState, instance_id)
        if current is None or current.execution_domain != "live" or current.ledger_mode != "attributed" or (current.last_update, float(current.virtual_cash), dict(current.virtual_positions or {})) != observation:
            return {"written":0, "status":"LEDGER_CHANGED_RETRY"}
        for date in eligible:
            prices = {symbol: marks[symbol].get(date) for symbol in positions}
            if any(p is None or not math.isfinite(p) or p <= 0 for p in prices.values()):
                missing_dates.append(date)
                continue
            cash = observation[1]
            receivable = float(outstanding_dividends(session, instance_id, "live", date))
            nav = round(cash + sum(positions[s] * p for s, p in prices.items()) + receivable, 4)
            if not math.isfinite(nav) or nav <= 0:
                raise ValueError("invalid live NAV")
            existing = session.get(PerfSnapshot, (instance_id, date))
            if existing is not None and existing.execution_domain != "live":
                raise ValueError("snapshot execution domain mismatch")
            daily_return = perf._compute_daily_return(session, instance_id, date, nav, "live")
            session.merge(PerfSnapshot(instance_id=instance_id, date=date, execution_domain="live",
                nav=nav, daily_return=daily_return, positions_snapshot=positions))
            session.merge(PerfValuation(instance_id=instance_id, date=date, execution_domain="live",
                nav=nav, cash=cash, dividend_receivable=receivable))
            session.flush()
            written.append(date)
        session.commit()
    if written:
        DailyRiskSnapshotService(sf, store).rebuild(instance_id=instance_id,
            execution_domain="live", start_date=written[0], end_date=written[-1])
    return {"status": "VALUED" if written else "WAITING_PRICE_OR_LEDGER",
            "written": len(written), "dates": written, "missing_price_dates": missing_dates,
            "earliest_supported_date": lower, "source": "attributed_live_ledger_and_raw_close"}
