import math

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
