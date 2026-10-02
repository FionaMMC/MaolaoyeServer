"""Read-only account analysis. Only aggregate results leave the server.

Public daily data is fetched into memory; no production files are modified.
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
v20 = sorted(orders[orders.account_group=='paper_v20h'].symbol.unique())
selected = orders[(orders.account_group!='paper_v20h')|orders.symbol.isin(v20)].copy()
symbols = sorted(selected.symbol.unique())

def fetch(symbol):
    etf=symbol.startswith(('15','51','56','58'))
    payload={'api_name':'fund_daily' if etf else 'daily','token':token,'params':{'ts_code':symbol,'start_date':'20260624','end_date':'20260924'},'fields':''}
    req=Request('https://api.tushare.pro',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    try:
        obj=json.loads(urlopen(req,timeout=20).read())
        if obj.get('code')!=0:return symbol,None,{'api_code':obj.get('code'),'message':str(obj.get('msg','')).replace(token,'[redacted]')}
        d=obj.get('data') or {};f=pd.DataFrame(d.get('items',[]),columns=d.get('fields',[]))
        if f.empty:return symbol,None,{'error':'empty'}
        f=f.sort_values('trade_date').drop_duplicates('trade_date')
        for c in ['open','high','low','close','vol','amount','pre_close']:f[c]=pd.to_numeric(f[c])
        return symbol,f,None
    except Exception as e:return symbol,None,{'error':type(e).__name__}

market={};errors=[]
with cf.ThreadPoolExecutor(max_workers=2) as pool:
    for symbol,frame,error in pool.map(fetch,symbols):
        if error:errors.append(error)
        else:market[symbol]=frame
records=[];coverage=Counter();diagnostics=defaultdict(Counter);limit_offsets=defaultdict(list)
policies=[('frozen_open',0,False),('frozen_touch_bound',0,True),('cap50',50,False),('cap100',100,False),('cap200',200,False),('arrival',float('inf'),False)]
for order in selected.itertuples():
    f=market.get(order.symbol)
    if f is None:coverage['no_daily_symbol']+=1;continue
    day=f[f.trade_date==order.valid_date]
    if day.empty:coverage['missing_day']+=1;continue
    created=pd.Timestamp(order.created_at)
    if created.tzinfo is None:created=created.tz_localize('UTC')
    if created.tz_convert('Asia/Shanghai')>pd.Timestamp(order.valid_date+' 09:25',tz='Asia/Shanghai'):
        coverage['not_created_before_open']+=1;continue
    b=day.iloc[0];reference=float(b.pre_close)
    if reference<=0 or order.quantity<=0:continue
    if not .7<order.limit_price/reference<1.3:coverage['reference_scale_mismatch']+=1;continue
    coverage['eligible']+=1;diagnostics[order.account_group]['eligible']+=1
    sign=1 if order.direction=='BUY' else -1
    limit_offsets[order.account_group].append(sign*(order.limit_price/reference-1)*10000)
    if order.actual_filled<=0:
        diagnostics[order.account_group]['zero_reported_fill']+=1
        diagnostics[order.account_group]['zero_fill_status_'+order.status]+=1
        if (b.low>order.limit_price if sign==1 else b.high<order.limit_price):
            diagnostics[order.account_group]['zero_fill_and_limit_never_touched']+=1
    if order.quantity>float(b.vol)*100*.01:diagnostics[order.account_group]['requested_above_1pct_daily_volume']+=1
    if sign*(b.open-order.limit_price)>0:diagnostics[order.account_group]['old_limit_misses_open']+=1
    if (b.low>order.limit_price if sign==1 else b.high<order.limit_price):diagnostics[order.account_group]['old_limit_never_touched']+=1
    if b.high<=b.low:diagnostics[order.account_group]['locked_one_price_day']+=1
    future=f[f.trade_date>order.valid_date].head(5)
    future_price=float(b.close*np.prod(future.close/future.pre_close)) if len(future)==5 else None
    etf=order.symbol.startswith(('15','51','56','58'));tick=.001 if etf else .01
    for slip in [5,25,50]:
        for policy,cap,touch_allowed in policies:
            limit=float(order.limit_price) if policy.startswith('frozen') else reference*(1+sign*cap/10000) if math.isfinite(cap) else (float('inf') if sign==1 else 0.)
            arrival=float(b.open)*(1+sign*slip/10000)
            arrival=(math.ceil(arrival/tick-1e-9) if sign==1 else math.floor(arrival/tick+1e-9))*tick
            price=None;touch=False
            if b.vol>0 and b.high>b.low:
                if sign*(arrival-limit)<=1e-9:price=arrival
                elif touch_allowed and (b.low<limit-tick/2 if sign==1 else b.high>limit+tick/2):price=limit;touch=True
            qty=min(order.quantity,int(float(b.vol)*100*.01)//100*100) if price is not None else 0
            n=qty*(price or 0)
            costs=max(5,n*(.0001 if etf else .0003)) if qty else 0
            if not etf:costs+=n*(.00001+(.0005 if sign==-1 else 0))
            payoff=sign*(qty*float(b.close)-n)-costs
            payoff5=sign*(qty*future_price-n)-costs if future_price is not None else None
            records.append({'group':order.account_group,'policy':policy,'slip_bps':slip,'intended':order.quantity*reference,
                'filled_ref':qty*reference,'full':int(qty==order.quantity),'any':int(qty>0),'payoff':payoff,'payoff5':payoff5,
                'touch_ref':qty*reference if touch else 0,'side':order.direction,'fetched':order.fetched_at is not None})
out={'inventory_since_july':inventory,'sampling':{'v20h_symbol_sample':'ALL','requested_symbols':len(symbols),'raw_daily_symbols':len(market),'selected_orders':len(selected),'coverage':dict(coverage),'provider_errors':dict(Counter(json.dumps(x,ensure_ascii=False,sort_keys=True) for x in errors))},'diagnostics':dict(diagnostics),'existing_limit_adverse_offset_bps_quantiles':{g:{str(q):float(np.quantile(v,q)) for q in [.1,.5,.9]} for g,v in limit_offsets.items()},'results':[],
'limitations':['Order opportunity replay, not a cash-constrained portfolio. Includes repeated intents and blocked/rejected orders.','Prices are provider unadjusted daily OHLC; pre_close is ex-rights reference.','Opening fills assume full capacity up to 1% of daily shares; inaccessible queues, spread and intraday volume timing are unknown.','Frozen touch is an optimistic bound. Five-day price markout chains close/pre_close to neutralize ex-rights jumps, not a cash dividend ledger.','Net markout is signed fill-to-horizon price change per total intended notional, with zero for no fill. It is not strategy NAV return.']}
if records:
    data=pd.DataFrame(records)
    for keys,f in data.groupby(['group','policy','slip_bps']):
        f5=f[f.payoff5.notna()]
        out['results'].append({'group':keys[0],'policy':keys[1],'slip_bps':int(keys[2]),'orders':len(f),'five_day_orders':len(f5),
        'modeled_notional_completion':float(f.filled_ref.sum()/f.intended.sum()),'modeled_full_order_rate':float(f.full.mean()),
        'modeled_any_fill_rate':float(f['any'].mean()),'net_eod_markout_bps_per_intended_notional':float(f.payoff.sum()/f.intended.sum()*10000),
        'net_5day_markout_bps_per_intended_notional':float(f5.payoff5.sum()/f5.intended.sum()*10000) if len(f5) else None,
        'touch_share_of_filled_notional':float(f.touch_ref.sum()/f.filled_ref.sum()) if f.filled_ref.sum() else 0})
print(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
