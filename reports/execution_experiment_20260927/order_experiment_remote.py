"""Read-only account analysis. Only aggregate results leave the server.

Public minute data is fetched into memory; no production files are modified.
Order-policy counterfactuals are independent, not a cash-constrained NAV replay.
"""
import concurrent.futures as cf
import hashlib
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

ROOT = Path('/opt/qmt-server/v2.3/server')
db = sqlite3.connect(f'file:{ROOT}/pipeline-server.db?mode=ro', uri=True)
db.execute('PRAGMA query_only=ON')
orders = pd.read_sql_query("""SELECT o.*, COALESCE(t.filled,0) actual_filled
 FROM orders o LEFT JOIN (SELECT order_id,MAX(filled_quantity) filled FROM trades GROUP BY order_id) t
 ON o.order_id=t.order_id WHERE valid_date BETWEEN '20260701' AND '20260924'""", db)
db.close()
token = None
for line in Path('/opt/qmt-refresh/.env').read_text().splitlines():
    if line.strip().startswith('TUSHARE_TOKEN='):
        token = line.split('=',1)[1].strip().strip('"').strip("'")
assert token
inventory = {}
for group, frame in orders.groupby('account_group'):
    inventory[group] = {'orders': len(frame), 'statuses': dict(Counter(frame.status)),
        'fetched_orders': int(frame.fetched_at.notna().sum()),
        'any_actual_fill_orders': int((frame.actual_filled>0).sum()),
        'full_actual_fill_orders': int((frame.actual_filled>=frame.quantity).sum()),
        'actual_quantity_completion': float(np.minimum(frame.actual_filled,frame.quantity).sum()/frame.quantity.sum())}
# Predefined deterministic symbol sample for the broad V20H universe; all V713/ETF symbols.
v20 = sorted(orders[orders.account_group=='paper_v20h'].symbol.unique(),key=lambda s:hashlib.sha256(s.encode()).hexdigest())[:64]
selected = orders[(orders.account_group!='paper_v20h')|orders.symbol.isin(v20)].copy()
symbols = sorted(selected.symbol.unique())

def fetch(symbol):
    payload={'api_name':'stk_mins','token':token,'params':{'ts_code':symbol,'freq':'5min','start_date':'2026-06-24 09:00:00','end_date':'2026-09-24 15:30:00'},'fields':''}
    req=Request('https://api.tushare.pro',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    for attempt in range(2):
        try:
            obj=json.loads(urlopen(req,timeout=20).read())
            if obj.get('code')!=0:
                return symbol,None,{'api_code':obj.get('code')}
            d=obj.get('data') or {}; f=pd.DataFrame(d.get('items',[]),columns=d.get('fields',[]))
            if len(f)>=8000: return symbol,None,{'error':'truncation'}
            if f.empty:return symbol,None,{'error':'empty'}
            f['trade_time']=pd.to_datetime(f.trade_time)
            f=f.sort_values('trade_time').drop_duplicates('trade_time')
            f['date']=f.trade_time.dt.strftime('%Y%m%d');f['clock']=f.trade_time.dt.strftime('%H:%M')
            for c in ['open','high','low','close','vol','amount']: f[c]=pd.to_numeric(f[c])
            return symbol,f,None
        except Exception as e:
            if attempt: return symbol,None,{'error':type(e).__name__}
    raise AssertionError

market={};errors=[]
with cf.ThreadPoolExecutor(max_workers=3) as pool:
    for symbol, frame, error in pool.map(fetch,symbols):
        if error: errors.append(error)
        else: market[symbol]=frame

# Run at 09:35, using the next 5-minute bar open as an arrival approximation.
# This avoids using the first five-minute closing price before it is observed.
# 1% of each realized bar volume is an ex-post capacity assumption, never proof of a fill.
records=[];coverage=Counter();ratios=[]
policies=[('frozen',None),('cap50',50),('cap100',100),('cap200',200),('arrival',float('inf'))]
for order in selected.itertuples():
    f=market.get(order.symbol)
    if f is None: coverage['no_minute_symbol']+=1;continue
    day=f[(f.date==order.valid_date)&(f.clock>='09:40')&(f.clock<='10:30')].copy()
    prev=f[(f.date<order.valid_date)&(f.clock>='14:55')]
    full_day=f[f.date==order.valid_date]
    if len(day)<6 or prev.empty or full_day.empty:coverage['no_day_or_previous_quote']+=1;continue
    reference=float(prev.iloc[-1].close)
    eod=float(full_day.iloc[-1].close)
    sign=1 if order.direction=='BUY' else -1
    if reference<=0 or order.quantity<=0:continue
    ratios.append(order.limit_price/reference)
    # Only orders with a same-scale limit; retain ordinary overnight gaps.
    if not .7<order.limit_price/reference<1.3: coverage['reference_scale_mismatch']+=1;continue
    coverage['eligible']+=1
    etf=order.symbol.startswith(('15','51','56','58'))
    tick=.001 if etf else .01
    for slip in [5,25,50]:
        for policy,cap in policies:
            remaining=int(order.quantity);filled=0;notional=0.;fees=0.;touch_qty=0
            if policy=='frozen': limit=float(order.limit_price)
            else:
                limit=reference*(1+sign*cap/10000) if math.isfinite(cap) else (float('inf') if sign==1 else 0.)
            for bar in day.itertuples():
                if not remaining:break
                # Locked five-minute bars are conservatively skipped; queue priority unknown.
                if bar.vol<=0 or bar.high<=bar.low:continue
                arrival=float(bar.open)*(1+sign*slip/10000)
                arrival=math.ceil(arrival/tick-1e-9)*tick if sign==1 else math.floor(arrival/tick+1e-9)*tick
                touch=False
                if sign*(arrival-limit)<=1e-9:
                    price=arrival
                elif policy=='frozen' and (bar.low<limit-tick/2 if sign==1 else bar.high>limit+tick/2):
                    price=limit;touch=True
                else:continue
                # The API's minute vol is shares. Keep 100-share lots and 1% participation.
                qty=min(remaining,int(bar.vol*.01)//100*100)
                if qty<=0:continue
                filled+=qty;remaining-=qty;notional+=qty*price
                fees+=max(5.,qty*price*.0001 if etf else qty*price*.0003)
                if not etf:
                    fees+=qty*price*.00001  # transfer cost assumption
                    if sign==-1:fees+=qty*price*.0005
                if touch:touch_qty+=qty
            payoff=sign*(filled*eod-notional)-fees
            records.append({'group':order.account_group,'policy':policy,'slip_bps':slip,
                'intended':order.quantity*reference,'filled_ref':filled*reference,
                'filled':filled,'quantity':order.quantity,'full':int(filled==order.quantity),'any':int(filled>0),
                'payoff_eod':payoff,'touch_ref':touch_qty*reference,
                'fetched':order.fetched_at is not None,'side':order.direction})

result={'as_of':datetime.now(timezone.utc).isoformat(),'inventory_since_july':inventory,
        'sampling':{'v20h_symbol_rule':'first 64 SHA256(symbol), among Jul-Sep orders','selected_symbols':len(symbols),'minute_symbols':len(market),'selected_orders':len(selected),'coverage':dict(coverage),'provider_error_counts':dict(Counter(json.dumps(x,sort_keys=True) for x in errors))},
        'minute_volume_implied_shares_multiplier':{},'results':[],
        'limitations':['Independent order opportunities, not cash-constrained portfolio returns; retries may repeat economic intents.',
            'Daily profit is signed fill-to-same-day-close markout net of modeled costs; unfilled orders contribute zero.',
            'Five-minute open plus cost is an execution approximation. Frozen intrabar touch is optimistic; no queue, cash or live quote evidence.',
            'All candidate rules use same per-bar 1% realized volume capacity and minimum commission per child fill; this is ex-post capacity, not an order-time predictor.',
            'Only 09:35-10:30 execution; no intraday signal change, no retry outside window.']}
if market:
    ratios_volume=[]
    for f in market.values():
        v=f[(f.vol>0)&(f.close>0)]
        ratios_volume.extend((v.amount/v.vol/v.close).tolist())
    result['minute_volume_implied_shares_multiplier']={str(q):float(np.quantile(ratios_volume,q)) for q in [.1,.5,.9]}
if records:
    data=pd.DataFrame(records)
    for keys, f in data.groupby(['group','policy','slip_bps']):
        result['results'].append({'group':keys[0],'policy':keys[1],'slip_bps':int(keys[2]),'orders':len(f),
            'modeled_notional_completion':float(f.filled_ref.sum()/f.intended.sum()),
            'modeled_full_order_rate':float(f.full.mean()),'modeled_any_fill_rate':float(f['any'].mean()),
            'net_eod_markout_bps_per_intended_notional':float(f.payoff_eod.sum()/f.intended.sum()*10000),
            'touch_share_of_filled_notional':float(f.touch_ref.sum()/f.filled_ref.sum()) if f.filled_ref.sum() else 0})
print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
