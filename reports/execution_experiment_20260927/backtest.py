"""Fixed-weight execution sensitivity study. No production code dependency.

Daily touch scenarios are optimistic accessibility bounds, not realized fills.
Prices are raw; explicit corporate actions maintain portfolio accounting.
"""
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import json
import math

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
PRIOR = HERE.parent/'hydra_performance_20260918/private'
SYMBOLS = {'hs300':'510300.SH','cyb':'159915.SZ','bond':'511260.SH','gold':'518880.SH',
           'commodity2':'159981.SZ','commodity3':'159985.SZ','crude_oil':'159930.SZ',
           'sp500':'513500.SH','nasdaq':'513100.SH'}

@dataclass(frozen=True)
class Policy:
    name: str
    gap_bps: float
    sessions: int
    touch: bool = False

POLICIES = [Policy('frozen_1d_touch',0,1,True), Policy('frozen_3d_touch',0,3,True),
            Policy('frozen50_1d_touch',50,1,True), Policy('frozen50_3d_touch',50,3,True),
            Policy('frozen_1d_open_only',0,1),
            *[Policy(f'cap{bp}_{n}d',bp,n) for bp in [50,100,200] for n in [1,3]],
            Policy('arrival_1d',float('inf'),1)]

def fee(notional):
    return max(5.,notional*.0001) if notional>0 else 0.

def price_for_fill(bar, reference, side, policy, slip_bps, phase):
    """Only current open and pre-existing limit determine open fills."""
    if bar is None or bar['volume']<=0 or bar.get('suspendFlag',0) or bar['high']<=bar['low']:
        return None
    limit=reference*(1+side*policy.gap_bps/10000) if math.isfinite(policy.gap_bps) else (math.inf if side==1 else 0)
    tick=.001
    limit=math.floor(limit/tick+1e-8)*tick if side==1 and math.isfinite(limit) else (math.ceil(limit/tick-1e-8)*tick if side==-1 else limit)
    if phase=='open':
        arrival=bar['open']*(1+side*slip_bps/10000)
        arrival=(math.ceil(arrival/tick-1e-8) if side==1 else math.floor(arrival/tick+1e-8))*tick
        return arrival if side*(arrival-limit)<=1e-8 else None
    if policy.touch and (bar['low']<limit-tick/2 if side==1 else bar['high']>limit+tick/2):
        return limit
    return None

def load_inputs():
    raw=pd.read_parquet(HERE/'private/hydra_raw.parquet')
    raw['date']=pd.to_datetime(raw.trade_date.astype(str))
    assert not raw.duplicated(['symbol','date']).any()
    for row in raw.itertuples():
        assert row.low<=min(row.open,row.close)<=max(row.open,row.close)<=row.high
    bars={(r.date,r.symbol):r._asdict() for r in raw.itertuples(index=False)}
    close=raw.pivot(index='date',columns='symbol',values='close').sort_index().ffill()
    weights=pd.read_parquet(PRIOR/'reconstructed_weights.parquet').rename(columns=SYMBOLS)
    weights.index=pd.to_datetime(weights.index)
    weights=weights.loc[weights.index<close.index.max()] # last partial target cannot execute
    assert np.allclose(weights.sum(axis=1),1)
    probe=json.loads((HERE/'private/provider_probe.json').read_text())
    actions=[]
    for key in ['fund_div_510300','fund_div_511260']:
        d=probe[key]['data'];f=pd.DataFrame(d['items'],columns=d['fields'])
        f=f[f.div_proc=='实施'].drop_duplicates(['ts_code','ex_date','div_cash'])
        for r in f.itertuples():
            if pd.to_datetime(r.ex_date)>close.index.max(): continue
            actions.append({'symbol':r.ts_code,'ex_date':pd.to_datetime(r.ex_date),'record_date':pd.to_datetime(r.record_date),
                            'pay_date':pd.to_datetime(r.pay_date),'cash':float(r.div_cash),'factor':1.})
    for symbol,date,factor in [('513100.SH','2022-01-14',5.),('513500.SH','2022-03-30',2.)]:
        actions.append({'symbol':symbol,'ex_date':pd.Timestamp(date),'record_date':None,'pay_date':None,'cash':0.,'factor':factor})
    # Independent reconciliation: cumulative cash offsets and share factors must
    # reproduce ALL raw-to-HFQ closes. This detects missing events and wrong ex-dates.
    validation={}
    for symbol in SYMBOLS.values():
        back=pd.read_parquet(PRIOR/'flat/etf_back'/f'{symbol}.parquet')
        back.index=pd.to_datetime(back.trade_date.astype(str))
        r=raw[raw.symbol==symbol].set_index('date').close
        dates=r.index.intersection(back.index)
        factor=pd.Series(1.,index=dates);offset=pd.Series(0.,index=dates)
        for a in sorted([a for a in actions if a['symbol']==symbol],key=lambda a:a['ex_date']):
            if a['factor']!=1: factor.loc[factor.index>=a['ex_date']]*=a['factor']
            if a['cash']: offset.loc[offset.index>=a['ex_date']]+=a['cash'] # dividend symbols never split here
        diff=(r.loc[dates]*factor+offset-back.loc[dates,'close']).abs()
        validation[symbol]={'rows':len(diff),'max_abs_raw_action_vs_hfq':float(diff.max())}
        assert diff.max()<.002, (symbol,diff.nlargest(3))
    return weights,bars,close,actions,validation

def run(weights,bars,close,actions,policy,capital=200000.,slip=5.,common_initialization=False):
    dates=close.loc[weights.index.min():].index
    symbols=list(weights.columns)
    qty={s:0 for s in symbols};cash=float(capital);pending=None
    rights={};receivables=[];history=[];cycles=[];fees_total=0.;turnover=0.;touch_notional=0.;cash_short=0
    by_ex=defaultdict(list);by_record=defaultdict(list)
    for a in actions:
        by_ex[a['ex_date']].append(a)
        if a['record_date'] is not None:by_record[a['record_date']].append(a)
    for date in dates:
        for a in by_ex[date]:
            s=a['symbol']
            if a['factor']!=1:
                qty[s]=round(qty[s]*a['factor'])
                if pending:
                    pending['target'][s]=round(pending['target'][s]*a['factor'])
                    pending['ref'][s]/=a['factor']
                    pending['original_ref'][s]/=a['factor']
            if a['cash']:
                entitlement=rights.get((s,a['ex_date']),0)*a['cash']
                receivables.append([a['pay_date'],entitlement])
                if pending:pending['ref'][s]-=a['cash']
        for r in receivables:
            if r[0]<=date:cash+=r[1];r[1]=0.
        if pending:
            pending['age']+=1
            active_policy=Policy('common_initialization',math.inf,1) if common_initialization and pending['date']==weights.index.min() else policy
            # Open sells cannot borrow proceeds from later intraday touch sells.
            for phase in ['open','touch']:
                for side in [-1,1]:
                    for s in symbols:
                        delta=pending['target'][s]-qty[s]
                        if not delta or int(np.sign(delta))!=side:continue
                        bar=bars.get((date,s))
                        price=price_for_fill(bar,pending['ref'][s],side,active_policy,slip,phase)
                        if price is None:continue
                        desired=abs(delta)
                        # 1% realized daily turnover capacity: ex-post daily scenario,
                        # NOT an order-time prediction and not queue evidence.
                        cap=int(float(bar['volume'])*100*.01)//100*100
                        cap=max(0,cap-pending['daily_used'].get((date,s),0))
                        n=min(desired,cap)
                        n=n//100*100
                        if side==1:
                            original=n
                            n=min(n,max(0,int((cash-5)/price)//100*100))
                            while n>0 and n*price+fee(n*price)>cash+1e-8:n-=100
                            if n<original:cash_short+=1
                        if n<=0:continue
                        cost=fee(n*price);cash-=side*n*price+cost;qty[s]+=side*n
                        assert cash>=-1e-6 and qty[s]>=0
                        pending['daily_used'][(date,s)]=pending['daily_used'].get((date,s),0)+n
                        fees_total+=cost;turnover+=n*price
                        pending['filled']+=n*pending['original_ref'][s]
                        if phase=='touch':touch_notional+=n*price
            if pending['age']>=active_policy.sessions:
                cycles.append({k:pending[k] for k in ['date','intended','filled']});pending=None
        marks=close.loc[date]
        value=sum(qty[s]*marks[s] for s in symbols if qty[s])
        assert math.isfinite(value)
        nav=cash+value+sum(r[1] for r in receivables)
        history.append({'date':date,'nav':nav,'cash_fraction':cash/nav,'cash':cash,
                        'receivable':sum(r[1] for r in receivables),'marked_holdings':value})
        for a in by_record[date]:rights[(a['symbol'],a['ex_date'])]=qty[a['symbol']]
        if date in weights.index:
            assert pending is None,'overlapping target windows'
            ref={s:float(marks[s]) for s in symbols}
            target={s:int(nav*.99*weights.loc[date,s]/ref[s])//100*100 if weights.loc[date,s]>0 else 0 for s in symbols}
            intended=sum(abs(target[s]-qty[s])*ref[s] for s in symbols if math.isfinite(ref[s]))
            pending={'date':date,'age':0,'target':target,'ref':ref.copy(),'original_ref':ref.copy(),
                     'intended':intended,'filled':0.,'daily_used':{}}
    if pending:cycles.append({k:pending[k] for k in ['date','intended','filled']})
    hist=pd.DataFrame(history).set_index('date');c=pd.DataFrame(cycles)
    nav=hist.nav
    prior=nav[nav.index<pd.Timestamp('2025-01-01')].iloc[-1]
    recent=nav[nav.index>=pd.Timestamp('2025-01-01')]
    cy_recent=c[c.date>=pd.Timestamp('2025-01-01')]
    def completion(x):return float(x.filled.sum()/x.intended.sum()) if x.intended.sum() else 1.
    return {'policy':policy.name,'capital':capital,'slip_bps':slip,'common_initialization':common_initialization,'start':str(nav.index[0].date()),'end':str(nav.index[-1].date()),
        'sessions':len(nav),'cycles':len(c),'total_return':float(nav.iloc[-1]/capital-1),
        'cagr':float((nav.iloc[-1]/capital)**(365.25/(nav.index[-1]-nav.index[0]).days)-1),
        'max_drawdown':float((nav/nav.cummax()-1).min()),
        'return_since_2025':float(recent.iloc[-1]/prior-1),
        'mdd_since_2025':float((pd.concat([pd.Series([prior]),recent.reset_index(drop=True)])/pd.concat([pd.Series([prior]),recent.reset_index(drop=True)]).cummax()-1).min()),
        'modeled_notional_completion':completion(c),'completion_since_2025':completion(cy_recent),
        'average_cash_fraction':float(hist.cash_fraction.mean()),'fees_pct_initial':fees_total/capital,
        'two_way_turnover_over_initial':turnover/capital,'touch_share_of_turnover':touch_notional/turnover if turnover else 0,
        'cash_clipped_fills':cash_short},hist,c

def main():
    weights,bars,close,actions,validation=load_inputs()
    results=[];navs={}
    for capital in [200000.,1000000.]:
        for slip in [5,25,50]:
            for policy in POLICIES:
                result,nav,cycles=run(weights,bars,close,actions,policy,capital,slip)
                results.append(result)
                if capital==200000. and slip==5:
                    navs[policy.name]=nav.nav/capital
                    cycles.to_csv(HERE/'private'/f'cycles_{policy.name}.csv',index=False)
    pd.DataFrame(navs).to_csv(HERE/'nav_comparison.csv')
    pd.DataFrame(results).to_csv(HERE/'hydra_results.csv',index=False)
    out={'validation':validation,'target_count':len(weights),'results':results,
         'cash_events':[dict(a,ex_date=str(a['ex_date'].date()),record_date=str(a['record_date'].date()) if a['record_date'] is not None else None,pay_date=str(a['pay_date'].date()) if a['pay_date'] is not None else None) for a in actions]}
    (HERE/'hydra_results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    print(pd.DataFrame(results).query('capital == 200000 and slip_bps == 5')[['policy','total_return','return_since_2025','max_drawdown','modeled_notional_completion','touch_share_of_turnover']].to_string(index=False))

if __name__=='__main__':main()
