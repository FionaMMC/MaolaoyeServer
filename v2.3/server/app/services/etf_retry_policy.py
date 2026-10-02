"""Server-owned ETF retry envelope; a price ceiling/floor never rolls forward.

Uses the existing 50 bp client contract. The fresh execution publication values
cash/holdings; execution_reference_price remains the immutable envelope anchor.
No broker state or target allocation is rewritten by this module.
"""
import math
from sqlalchemy import select

from app.schemas.hydra_relay import HydraExecutionWaitResponseData
from app.schemas.trade_result import TradeResult
from app.models import Order, OrderStatusEvidence
from app.services.qmt_expiration import validate_policy_expiration

POLICY = "ETF_FIXED_50BP_3_TRADING_DAYS_V1"
MAX_SESSIONS = 3


def unproven_expiry(session, rebalance_id):
    """Preserve the existing user-approved day expiry only with its evidence.

    A hand-written status string alone must not free an old order for retry.
    """
    for order in session.scalars(select(Order).where(
        Order.rebalance_id==rebalance_id, Order.status=="EXPIRED_BY_POLICY",
    )):
        proven=False
        for evidence in session.scalars(select(OrderStatusEvidence).where(
            OrderStatusEvidence.order_id==order.order_id,
            OrderStatusEvidence.execution_domain==order.execution_domain,
            OrderStatusEvidence.account_alias==order.qmt_account_alias,
        )):
            payload=evidence.payload
            if payload.get('batch_sha256')!=order.batch_sha256 or payload.get('attempt_id')!=order.attempt_id:
                continue
            try:
                observation=TradeResult(**payload.get('observation',{}))
            except ValueError:
                continue
            if observation.status=='EXPIRED_BY_POLICY' and validate_policy_expiration(session,order,observation,order.valid_date) is None:
                proven=True
                break
        if not proven:
            return True
    return False


def envelope(first_trade_date, reference_date, raw_sha, prices):
    return {"policy_id": POLICY, "max_trading_sessions": MAX_SESSIONS,
            "first_trade_date": first_trade_date, "anchor_reference_date": reference_date,
            "anchor_raw_sha256": raw_sha, "anchor_prices": dict(prices),
            "buy_max_bps": 50, "sell_max_bps": 50}


def window_outcome(calendar, first_trade_date, trade_date):
    days = sorted(set(calendar["trade_date"].astype(str).str.replace("-", "", regex=False)))
    if first_trade_date not in days or trade_date not in days:
        return "CALENDAR_INCOMPLETE"
    offset = days.index(trade_date) - days.index(first_trade_date)
    if offset < 0:
        return "BEFORE_WINDOW"
    return "ELIGIBLE" if offset < MAX_SESSIONS else "WINDOW_EXPIRED"


def deferred(domain, rebalance_id, outcome, reason):
    # Existing Windows retry callers already accept this non-order receipt.
    return HydraExecutionWaitResponseData(
        status="WAITING_EXECUTION_DATE", execution_domain=domain,
        rebalance_id=rebalance_id, retry_outcome=outcome, reason=reason,
    )


def guarded_orders(orders, anchors, actual_cash, lot_size):
    """Keep the original 50 bp limits; scale BUY lots to a conservative budget.

    Projected sell proceeds are planning capacity only. The existing client's
    submission queue still requires confirmed own sell fills and actual cash.
    """
    result=[]
    for order in orders:
        row=dict(order)
        anchor=float(anchors[row['symbol']])
        if not math.isfinite(anchor) or anchor<=0:
            raise ValueError("ETF retry anchor must be finite and positive")
        side=1 if row['direction']=='BUY' else -1
        raw=anchor*(1+side*.005)/.001
        row['reference_price']=round(anchor,6)
        row['limit_price']=round((math.floor(raw+1e-9) if side==1 else math.ceil(raw-1e-9))*.001,3)
        result.append(row)
    sell=[o for o in result if o['direction']=='SELL']
    buys=[o for o in result if o['direction']=='BUY']
    def reserve(o):return max(5.,o['quantity']*o['limit_price']*.001)
    budget=max(0.,float(actual_cash)+sum(o['quantity']*o['limit_price']-reserve(o) for o in sell))
    desired=sum(o['quantity']*o['limit_price']+reserve(o) for o in buys)
    scale=min(1.,budget/desired) if desired else 1.
    # Proportional first pass avoids assigning the entire budget alphabetically.
    for o in buys:
        o['quantity']=int(o['quantity']*scale)//lot_size*lot_size
    for o in buys:
        while o['quantity']>0 and o['quantity']*o['limit_price']+reserve(o)>budget+1e-8:
            o['quantity']-=lot_size
        if o['quantity']:
            budget-=o['quantity']*o['limit_price']+reserve(o)
    return sorted([*sell,*[o for o in buys if o['quantity']]],key=lambda o:o['symbol'])
