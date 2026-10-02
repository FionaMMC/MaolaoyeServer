"""Remote-only extended history and matched execution policy research.

Never import this as an order router. Raw inputs and detailed results stay on qmt.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import hashlib
import json
import sys

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'execution_experiment_20260928'))
from compare_policies import Replay, Policy, FOCUS, grouped, old
from server_intraday import MinuteReplay

BASE = Policy('fixed50_3d')
DIFF = Policy('gold100_us150_3d', (100,150,150))
ROLL = Policy('rolling50_3d', rolling_focus_buy=True)
MAIN = [BASE, ROLL, Policy('fixed100_3d',(100,100,100)), DIFF,
        Policy('gold100_only_3d',(100,50,50))]
GRID = [Policy(f'fixed{b}_3d',(b,b,b)) for b in [0,25,50,75,100,125,150,200]]
GRID += [ROLL, DIFF, MAIN[-1], Policy('gold75_us125_3d',(75,125,125)),
         Policy('gold100_us125_3d',(100,125,125)),Policy('gold75_us150_3d',(75,150,150))]
GRID += [replace(p, name=p.name.replace('3d',f'{n}d'),sessions=n)
         for p in [BASE,ROLL,DIFF] for n in [1,2,5]]


@dataclass(frozen=True)
class FocusWindow(Policy):
    focus_sessions: int = 3


GRID += [FocusWindow(p.name.replace('3d',f'focus{n}d'),p.buy_caps,p.rolling_focus_buy,max(3,n),n)
         for p in [BASE,ROLL,DIFF] for n in [1,2,5]]


def save_json(path, obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False))


def fetch(job):
    symbol,scale=job
    url='https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData?'+urlencode(
        dict(symbol=('sh' if symbol.endswith('.SH') else 'sz')+symbol[:6],scale=scale,ma='no',datalen=1970))
    receipt=dict(symbol=symbol,scale=scale,url=url,utc=datetime.now(timezone.utc).isoformat())
    try:
        raw=urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=25).read().decode()
        rows=json.loads(raw.split('=(',1)[1].rsplit(');',1)[0])
        assert rows
        p=HERE/'raw'/f'{symbol}_{scale}m.json'
        save_json(p,rows)
        receipt.update(rows=len(rows),first=rows[0]['day'],last=rows[-1]['day'],file=p.name,
                       sha256=hashlib.sha256(p.read_bytes()).hexdigest())
    except Exception as e:
        receipt.update(error=type(e).__name__,message=str(e)[:180])
    print('download',json.dumps(receipt,ensure_ascii=False),flush=True)
    return receipt


def download_all():
    (HERE/'raw').mkdir(mode=0o700,exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts=list(pool.map(fetch,[(s,n) for n in [15,30,60] for s in old.SYMBOLS.values()]))
    save_json(HERE/'receipts.json',receipts)


def load_frequency(scale, bars):
    if scale in [1,5]:
        origin=HERE.parent/'execution_experiment_20260928'
        receipts=json.loads((origin/'server_download_receipts.json').read_text())
        receipts=[dict(r,scale=scale) for r in receipts if r['frequency']==f'{scale}m' and 'error' not in r]
        root=origin/'server_fetched_minutes'
    else:
        receipts=[r for r in json.loads((HERE/'receipts.json').read_text()) if r['scale']==scale and 'error' not in r]
        root=HERE/'raw'
    frames,quality={},[]
    for r in receipts:
        path=root/r['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==r['sha256']
        f=pd.DataFrame(json.loads(path.read_text()))
        f['time']=pd.to_datetime(f.day)
        f['date']=f.time.dt.normalize()
        for c in ['open','high','low','close','volume']:f[c]=pd.to_numeric(f[c])
        assert not f.time.duplicated().any()
        assert (f.low<=f[['open','close']].min(axis=1)).all()
        assert (f.high>=f[['open','close']].max(axis=1)).all()
        f=f[f.date>f.date.min()].copy()
        last=f.date.max()
        if f.loc[f.date==last,'time'].max().strftime('%H:%M')<'15:00':f=f[f.date<last].copy()
        mismatches=[];errors=[];ratios=[];ohl_errors=[];close_errors=[]
        for date,d in f.groupby('date'):
            q=bars.get((date,r['symbol']))
            if q is None:continue
            daily={'open':d.open.iloc[0],'high':d.high.max(),'low':d.low.min(),'close':d.close.iloc[-1]}
            error=max(abs(daily[c]-q[c]) for c in daily)
            errors.append(error)
            ohl_error=max(abs(daily[c]-q[c]) for c in ['open','high','low'])
            ohl_errors.append(ohl_error);close_errors.append(abs(daily['close']-q['close']))
            if q['volume']>0:ratios.append(float(d.volume.sum()/(q['volume']*100)))
            # Shanghai fund official close can be a weighted price, distinct
            # from the final trade in a minute candle. Execution uses O/H/L
            # only, while ALL strategy references and NAV use QMT daily close.
            # Never replace the official reference with the minute last trade.
            ratio=float(d.volume.sum()/(q['volume']*100)) if q['volume']>0 else 1.
            if ohl_error>.00101 or not .95<=ratio<=1.05:mismatches.append(str(date.date()))
        # Do not backfill bad days with daily bars, which would erase chronology.
        if mismatches:f=f[~f.date.isin(pd.to_datetime(mismatches))].copy()
        frames[r['symbol']]=f
        quality.append(dict(symbol=r['symbol'],scale=scale,rows=r['rows'],first=r['first'],last=r['last'],
            usable_days=int(f.date.nunique()),daily_overlap=len(errors),max_ohlc_error=max(errors) if errors else None,
            max_ohl_error=max(ohl_errors) if ohl_errors else None,max_last_vs_official_close_error=max(close_errors) if close_errors else None,
            volume_ratio_median=float(np.median(ratios)) if ratios else None,volume_ratio_min=min(ratios) if ratios else None,
            excluded_mismatch_days=mismatches))
    return frames,quality


class Windows:
    """Use a common FIVE-session evaluation horizon for 1/2/3/5-day policies."""
    def execute(self,state,date,pending,policy,touch=True,reverse_symbols=False):
        if not isinstance(policy,FocusWindow):
            return super().execute(state,date,pending,policy,touch,reverse_symbols)
        original=pending['target'].copy()
        next_age=pending['age']+1
        try:
            for s,target in original.items():
                is_focus_buy=s in FOCUS and target>state['qty'][s]
                allowed=policy.focus_sessions if is_focus_buy else 3
                if next_age>allowed:pending['target'][s]=state['qty'][s]
            return super().execute(state,date,pending,policy,touch,reverse_symbols)
        finally:
            pending['target']=original

    def portfolio(self, policy, touch=True, reverse_symbols=False):
        state=self.initial_state();pending=None;target=None
        history=[];details=[];snapshots=[]
        for date in self.dates:
            self.apply_actions(state,date,pending)
            if pending:
                self.execute(state,date,pending,policy,touch,reverse_symbols)
                if pending['age']>=5:
                    details.extend(deepcopy(list(pending['detail'].values())));pending=None
            self.record_rights(state,date)
            if date in self.schedule:
                assert pending is None
                signal,target,_=self.schedule[date]
                pending=self.make_pending(state,date,signal,target)
                snapshots.append((deepcopy(state),deepcopy(pending)))
            history.append(self.mark(state,date,target))
        assert pending is None
        return pd.DataFrame(history).set_index('date'),pd.DataFrame(details),snapshots

    def episode(self,snapshot,policy,touch=True,reverse_symbols=False):
        state,pending=deepcopy(snapshot)
        ideal=deepcopy(state)
        deltas={s:pending['target'][s]-ideal['qty'][s] for s in self.symbols}
        for s,n in deltas.items():
            if n<0:ideal['cash']-=n*pending['original_ref'][s];ideal['qty'][s]+=n
        need=sum(max(0,n)*pending['original_ref'][s] for s,n in deltas.items())
        ratio=min(1.,ideal['cash']/need) if need else 1.
        for s,n in deltas.items():
            if n>0:
                n=n if ratio>=1 else int(n*ratio)//100*100
                ideal['cash']-=n*pending['original_ref'][s];ideal['qty'][s]+=n
        self.record_rights(ideal,pending['date'])
        first=self.location[pending['date']]+1
        hist=[]
        for date in self.close.index[first:first+5]:
            self.apply_actions(state,date,pending);self.apply_actions(ideal,date)
            self.execute(state,date,pending,policy,touch,reverse_symbols)
            self.record_rights(state,date);self.record_rights(ideal,date)
            hist.append(self.mark(state,date,pending['weights']))
        summary=dict(signal=str(pending['signal'].date()),reference_date=str(pending['date'].date()),
            initial_nav=self.mark(snapshot[0],snapshot[1]['date'])['nav'],
            terminal_shortfall=self.mark(ideal,date)['nav']-self.mark(state,date)['nav'],
            allocation_distance=float(np.mean([x['allocation_distance'] for x in hist])),
            focus_underweight=float(np.mean([x['focus_underweight'] for x in hist])),attempts=pending['attempts'])
        return list(pending['detail'].values()),summary


class Daily(Windows,Replay):pass
class Intraday(Windows,MinuteReplay):pass


def data():
    w,b,c,a,v=old.load_inputs()
    w=w.loc[[d for d in w.index if len(c.index[c.index>d])>=7]]
    return w,b,c,a,v


def execution_metrics(d):
    rows=grouped(d)
    for period,sub in [('pre2025',d[d.signal<'2025-01-01']),('since2025',d[d.signal>='2025-01-01'])]:
        rows.extend(dict(x,group=x['group']+'_'+period) for x in grouped(sub))
    return rows


def compare_episodes(engine,snapshots,policies,tag,touch=True,reverse=False):
    metrics=[];summaries=[];all_details=[];denom=None
    for p in policies:
        detail=[]
        for snap in snapshots:
            d,s=engine.episode(snap,p,touch,reverse)
            detail.extend(d);summaries.append(dict(policy=p.name,**s))
        d=pd.DataFrame(detail)
        key=d[['signal','symbol','side','intended']].reset_index(drop=True)
        if denom is None:denom=key
        else:pd.testing.assert_frame_equal(key,denom)
        metrics.extend(dict(policy=p.name,**x) for x in execution_metrics(d))
        all_details.extend(dict(policy=p.name,**x) for x in detail)
    pd.DataFrame(all_details).to_csv(HERE/f'{tag}_order_detail.csv',index=False)
    pd.DataFrame(summaries).to_csv(HERE/f'{tag}_cycle_detail.csv',index=False)
    return metrics,summaries


def return_metrics(h,capital):
    nav=h.nav
    prior=nav[nav.index<'2025-01-01']
    return dict(start=str(nav.index[0].date()),end=str(nav.index[-1].date()),
        total_return=float(nav.iloc[-1]/capital-1),max_drawdown=float((nav/nav.cummax()-1).min()),
        return_since2025=float(nav.iloc[-1]/prior.iloc[-1]-1) if len(prior) else None,
        min_cash=float(h.cash.min()))


def bootstrap(nav,capital):
    monthly=nav.resample('ME').last()
    logs=np.log(monthly/monthly.shift().fillna(capital))
    rng=np.random.default_rng(20260928);n=len(logs)
    starts=rng.integers(0,n,(5000,(n+2)//3))
    idx=((starts[:,:,None]+np.arange(3))%n).reshape(5000,-1)[:,:n]
    base=logs[BASE.name].to_numpy()
    return {p:dict(delta_pp=float((nav[p].iloc[-1]-nav[BASE.name].iloc[-1])/capital*100),
        block3_ci95_pp=(np.quantile(np.exp(logs[p].to_numpy()[idx].sum(axis=1))-np.exp(base[idx].sum(axis=1)),[.025,.975])*100).tolist()) for p in nav}


def run_daily():
    w,b,c,a,v=data();results=[];metrics=[];episodes=[];navs={}
    scenarios=[('primary',200000.,5.,True,True,False,GRID),
        ('slip25',200000.,25.,True,True,False,MAIN),('slip50',200000.,50.,True,True,False,MAIN),
        ('capital1m',1000000.,5.,True,True,False,MAIN),('all_days',200000.,5.,False,True,False,MAIN),
        ('open_only',200000.,5.,True,False,False,MAIN),('reverse',200000.,5.,True,True,True,MAIN)]
    for tag,capital,slip,adj,touch,reverse,policies in scenarios:
        e=Daily(w,b,c,a,capital,slip,adj)
        base,bd,snaps=e.portfolio(BASE,touch,reverse)
        # Horizon extension must not alter existing 3-day baseline.
        legacy,ld,_=Replay(w,b,c,a,capital,slip,adj).portfolio(BASE,touch,reverse)
        assert np.allclose(base.nav,legacy.nav,rtol=0,atol=1e-7)
        m,s=compare_episodes(e,snaps[1:],policies,'daily_'+tag,touch,reverse)
        metrics.extend(dict(scenario=tag,**x) for x in m)
        episodes.extend(dict(scenario=tag,**x) for x in s)
        for p in policies:
            h,d,_=e.portfolio(p,touch,reverse)
            results.append(dict(scenario=tag,policy=p.name,**return_metrics(h,capital)))
            if tag=='primary':navs[p.name]=h.nav
        print('daily_done',tag,len(policies),len(snaps)-1,flush=True)
    nav=pd.DataFrame(navs);nav.to_csv(HERE/'daily_nav.csv')
    out=dict(portfolios=results,metrics=metrics,bootstrap=bootstrap(nav,200000.),
        portfolio_runs=len(results),episode_runs=len(episodes),raw_validation=v,
        mean_cycle_shortfall=pd.DataFrame(episodes).groupby(['scenario','policy']).terminal_shortfall.mean().reset_index().to_dict('records'))
    # Missing groups produce NaN; represent them as null in exported summaries.
    save_json(HERE/'daily_summary.json',clean(out))


def eligible_snapshots(e,snaps,frames):
    date_sets={s:set(d.date) for s,d in frames.items()}
    good=[]
    for snapshot in snaps[1:]:
        first=e.location[snapshot[1]['date']]+1
        dates=e.close.index[first:first+5]
        if len(dates)==5 and all(all(d in date_sets.get(s,set()) for d in dates) for s in e.symbols):good.append(snapshot)
    return good


def continuous(e,snapshot,end,policy):
    state,pending=deepcopy(snapshot);start=pending['date'];target=pending['weights']
    capital=e.mark(state,start)['nav'];hist=[e.mark(state,start,target)];detail=[]
    for date in e.close.index[(e.close.index>start)&(e.close.index<=end)]:
        e.apply_actions(state,date,pending)
        if pending:
            e.execute(state,date,pending,policy)
            if pending['age']>=5:detail.extend(deepcopy(list(pending['detail'].values())));pending=None
        e.record_rights(state,date)
        if date in e.schedule:
            assert pending is None
            signal,target,_=e.schedule[date];pending=e.make_pending(state,date,signal,target)
        hist.append(e.mark(state,date,target))
    assert pending is None
    h=pd.DataFrame(hist).set_index('date')
    return h,pd.DataFrame(detail),capital


def scaled_frames(frames,participation):
    result={s:f.copy() for s,f in frames.items()}
    for f in result.values():f['volume']*=participation/.01
    return result


def run_intraday():
    w,b,c,a,_=data();base=Daily(w,b,c,a)
    _,_,snaps=base.portfolio(BASE)
    out={'quality':[],'panels':[],'cross_resolution':[]}
    frames_by_scale={}
    for scale in [5,15,30,60]:
        frames,q=load_frequency(scale,b);out['quality'].extend(q)
        assert set(frames)==set(w.columns),(scale,set(frames))
        frames_by_scale[scale]=frames
        eligible=eligible_snapshots(base,snaps,frames)
        print('intraday_eligible',scale,len(eligible),flush=True)
        if not eligible:continue
        variants=[('primary',200000.,5.,.01,True,False,'14:55',GRID)]
        if scale in [5,60]:
            variants += [('slip25',200000.,25.,.01,True,False,'14:55',MAIN),
                ('slip50',200000.,50.,.01,True,False,'14:55',MAIN),
                ('capital1m',1000000.,5.,.01,True,False,'14:55',MAIN),
                ('capacity01',200000.,5.,.001,True,False,'14:55',MAIN),
                ('capacity5',200000.,5.,.05,True,False,'14:55',MAIN),
                ('open_only',200000.,5.,.01,False,False,'14:55',MAIN),
                ('reverse',200000.,5.,.01,True,True,'14:55',MAIN),
                ('close_included',200000.,5.,.01,True,False,'15:01',MAIN)]
        for tag,capital,slip,part,touch,reverse,cutoff,policies in variants:
            case_eligible=eligible
            if capital!=200000.:
                capital_base=Daily(w,b,c,a,capital)
                _,_,capital_snaps=capital_base.portfolio(BASE)
                case_eligible=eligible_snapshots(capital_base,capital_snaps,frames)
            e=Intraday(w,b,c,a,capital,slip)
            e.prepare(scaled_frames(frames,part),cutoff)
            m,s=compare_episodes(e,case_eligible,policies,f'minute{scale}_{tag}',touch,reverse)
            sums=pd.DataFrame(s).groupby('policy').agg(mean_shortfall=('terminal_shortfall','mean'),mean_underweight=('focus_underweight','mean')).reset_index().to_dict('records')
            panel=dict(scale=scale,scenario=tag,cycles=len(eligible),
                first_reference=str(eligible[0][1]['date'].date()),last_reference=str(eligible[-1][1]['date'].date()),
                metrics=m,cycle_summary=sums)
            if tag=='primary':
                returns=[];navs={}
                # Continuous series may only be called when every later target
                # is covered, never quietly fill holes with daily candles.
                later=[x for x in snaps[1:] if x[1]['date']>=eligible[0][1]['date']]
                if len(later)==len(eligible):
                    for p in policies:
                        e.execution_events=[]
                        h,d,capital=continuous(e,eligible[0],c.index[-1],p)
                        h.to_csv(HERE/f'minute{scale}_nav_{p.name}.csv')
                        returns.append(dict(policy=p.name,**return_metrics(h,capital)))
                        navs[p.name]=h.nav
                    panel['portfolios']=returns
                    panel['bootstrap']=bootstrap(pd.DataFrame(navs),capital)
            out['panels'].append(panel)
            save_json(HERE/'intraday_summary.json',clean(out))
            print('intraday_done',scale,tag,len(s),flush=True)
    # Same snapshots and an exactly common end-of-bar cutoff isolate the effect
    # of coarser candles. 14:01 includes the 14:00 ending bar at all frequencies.
    for fine,coarse in [(5,15),(15,30),(30,60)]:
        eligible=eligible_snapshots(base,snaps,frames_by_scale[fine])
        coarse_dates={x[1]['date'] for x in eligible_snapshots(base,snaps,frames_by_scale[coarse])}
        eligible=[x for x in eligible if x[1]['date'] in coarse_dates]
        if not eligible:continue
        for scale in [fine,coarse]:
            e=Intraday(w,b,c,a);e.prepare(frames_by_scale[scale],'14:01')
            m,s=compare_episodes(e,eligible,MAIN,f'cross{fine}_{coarse}_{scale}')
            out['cross_resolution'].append(dict(fine=fine,coarse=coarse,scale=scale,cycles=len(eligible),metrics=m,
                mean_shortfall=pd.DataFrame(s).groupby('policy').terminal_shortfall.mean().to_dict()))
    save_json(HERE/'intraday_summary.json',clean(out))


def run_opportunities():
    """Standardized hypothetical buys, kept distinct from strategy demands."""
    _,bars,_,_,_=data()
    frames5,_=load_frequency(5,bars);frames1,quality1=load_frequency(1,bars)
    frames5={s:frames5[s] for s in FOCUS}
    days5=sorted(set.intersection(*[set(f.date) for f in frames5.values()]))
    days1=sorted(set.intersection(*[set(f.date) for f in frames1.values()]))
    output={'one_minute_quality':quality1,'panels':[]}
    for name,frames,dates,policies in [('recent5m',frames5,days5,GRID),
            ('same_dates1m',frames1,sorted(set(days1)&set(days5)),MAIN),
            ('same_dates5m',frames5,sorted(set(days1)&set(days5)),MAIN)]:
        details=[];shortfalls=[]
        for symbol,f in frames.items():
            close=f.groupby('date').close.last().reindex(dates).to_frame(symbol)
            weights=pd.DataFrame({symbol:[1.]},index=close.index[:1])
            e=Intraday(weights,{},close,[],10000.,5.,True);e.prepare({symbol:f})
            for i in range(1,len(dates)-4):
                first=dates[i];reference_date=dates[i-1]
                if (first-reference_date).days!=1:continue
                ref=float(close.loc[reference_date,symbol]);qty=int((10000.-5)/(ref*1.025))//100*100
                if qty<=0:continue
                state=e.initial_state()
                pending=e.make_pending(state,reference_date,first,weights.iloc[0])
                pending['target'][symbol]=qty
                pending['detail'][symbol]['intended']=qty*ref
                pending['detail'][symbol]['initialization']=False
                terminal=float(close.iloc[i+4,0]/ref-1)
                for p in policies:
                    d,s=e.episode((state,pending),p)
                    details.extend(dict(policy=p.name,forward_return=terminal,**x) for x in d)
                    shortfalls.append(dict(policy=p.name,symbol=symbol,**s))
        d=pd.DataFrame(details)
        d.to_csv(HERE/f'{name}_hypothetical_orders.csv',index=False)
        m=[]
        for policy,sub in d.groupby('policy'):
            m.extend(dict(policy=policy,**x) for x in grouped(sub))
            for regime,part in [('up',sub[sub.forward_return>0]),('down_or_flat',sub[sub.forward_return<=0])]:
                m.extend(dict(policy=policy,**x,regime=regime) for x in grouped(part))
        sf=pd.DataFrame(shortfalls)
        output['panels'].append(dict(name=name,first=str(dates[0].date()),last=str(dates[-1].date()),
            metrics=m,mean_shortfall=sf.groupby(['policy','symbol']).terminal_shortfall.mean().reset_index().to_dict('records')))
        print('opportunities_done',name,len(details),flush=True)
    save_json(HERE/'opportunity_summary.json',clean(output))


def clean(value):
    if isinstance(value,dict):return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,list):return [clean(v) for v in value]
    if isinstance(value,(float,np.floating)):return float(value) if np.isfinite(value) else None
    if isinstance(value,np.integer):return int(value)
    return value


if __name__=='__main__':
    assert str(HERE).startswith('/opt/qmt-server/private/research-runs/'), 'Server only: never download local raw data.'
    stage=sys.argv[1]
    {'fetch':download_all,'daily':run_daily,'intraday':run_intraday,'opportunities':run_opportunities}[stage]()
