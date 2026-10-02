"""Server-only unauthenticated downloads and chronological intraday replay.

Raw market data remains under /opt/qmt-server/private/research-runs.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import hashlib
import json
import math
import sys

import pandas as pd
import numpy as np
from compare_policies import Replay, POLICIES, FOCUS, limit_reference, old

HERE = Path(__file__).resolve().parent
OUT = HERE/'server_fetched_minutes'


def download(job):
    symbol, frequency = job
    sina = frequency in {'5m','sina1m'}
    if sina:
        market = 'sh' if symbol.endswith('.SH') else 'sz'
        url = 'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData?'+urlencode(dict(symbol=market+symbol[:6],scale=5 if frequency=='5m' else 1,ma='no',datalen=1970))
    else:
        url = 'https://push2his.eastmoney.com/api/qt/stock/trends2/get?'+urlencode(dict(secid='1.'+symbol[:6],fields1='f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13',fields2='f51,f52,f53,f54,f55,f56,f57,f58',ndays=5,iscr=0))
    receipt = dict(symbol=symbol,frequency='1m' if frequency=='sina1m' else frequency,provider='sina' if sina else 'eastmoney',url=url,fetched_at=datetime.now(timezone.utc).isoformat(),fetch_host='qmt')
    try:
        raw = urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=20).read().decode('utf-8')
        payload = json.loads(raw.split('=(',1)[1].rsplit(');',1)[0]) if sina else json.loads(raw)
        rows = payload if sina else (payload.get('data') or {}).get('trends',[])
        assert rows,'empty provider response'
        path=OUT/f'{symbol}_{frequency}.json'
        path.write_text(json.dumps(payload,ensure_ascii=False))
        first,last=(rows[0]['day'],rows[-1]['day']) if sina else (rows[0].split(',')[0],rows[-1].split(',')[0])
        receipt.update(rows=len(rows),first=first,last=last,file=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    except Exception as exc:
        receipt.update(error=type(exc).__name__,message=str(exc)[:140])
    print(json.dumps(receipt,ensure_ascii=False),flush=True)
    return receipt


def load_minutes(receipts):
    result = {}
    for receipt in receipts:
        if receipt['frequency']!='5m' or 'error' in receipt:continue
        path=OUT/receipt['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==receipt['sha256']
        frame=pd.DataFrame(json.loads(path.read_text()))
        frame['time']=pd.to_datetime(frame.day)
        for col in ['open','high','low','close','volume']:frame[col]=pd.to_numeric(frame[col])
        assert not frame.time.duplicated().any()
        assert ((frame.low<=frame[['open','close']].min(axis=1))&(frame.high>=frame[['open','close']].max(axis=1))).all()
        frame['date']=frame.time.dt.normalize()
        frame=frame[frame.date>frame.date.min()].copy()
        # The download can run during the trading session; never call today's
        # partial bars a complete backtest day.
        last_date=frame.date.max()
        if frame.loc[frame.date==last_date,'time'].max().strftime('%H:%M')<'15:00':
            frame=frame[frame.date<last_date].copy()
        result[receipt['symbol']]=frame
    return result


class MinuteReplay(Replay):
    """Bar-chronological shared cash; intrabar ambiguity remains conservative.

    Cash generated inside a bar is unavailable to buys until the next bar.
    Fees aggregate per symbol/day rather than per partial fill.
    """
    def prepare(self,minutes,cutoff='14:55'):
        self.intraday={}
        self.minute_cutoff=cutoff
        self.execution_events=[]
        for s,frame in minutes.items():
            for date,day in frame.groupby('date'):
                self.intraday[date,s]=day

    def execute(self,state,date,pending,policy,touch=True,reverse_symbols=False):
        pending['age']+=1
        previous_date=self.close.index[self.location[date]-1]
        if pending['age']>policy.sessions or (self.adjacent and (date-previous_date).days!=1):return
        pending['attempts']+=1
        symbols=list(reversed(self.symbols)) if reverse_symbols else self.symbols
        bars={}
        for s in symbols:
            if pending['target'][s]!=state['qty'][s]:
                assert (date,s) in self.intraday, f'missing minute day {date} {s}'
                for row in self.intraday[date,s].itertuples():
                    if row.time.strftime('%H:%M')<self.minute_cutoff and row.volume>0:
                        bars[row.time,s]=row
        daily_notional={}
        for time in sorted({t for t,s in bars}):
            # All cash at this bar start is known; even sell-open cash is deferred
            # to the next bar to avoid assuming the cross-asset event order.
            spendable=max(0.,state['cash'])
            used={}
            for phase in (['open','touch'] if touch else ['open']):
                for side in [-1,1]:
                    for s in symbols:
                        r=bars.get((time,s))
                        delta=pending['target'][s]-state['qty'][s]
                        if r is None or not delta or int(np.sign(delta))!=side:continue
                        ref,bp=limit_reference(pending,s,side,policy,self.close.loc[previous_date],self.ex.get(date,[]))
                        limit=ref*(1+side*bp/10000)
                        limit=(math.floor(limit/.001+1e-8) if side==1 else math.ceil(limit/.001-1e-8))*.001
                        if phase=='open':
                            price=r.open*(1+side*self.slip/10000)
                            price=(math.ceil(price/.001-1e-8) if side==1 else math.floor(price/.001+1e-8))*.001
                            if side*(price-limit)>1e-8:continue
                        else:
                            if not (r.low<limit-.0005 if side==1 else r.high>limit+.0005):continue
                            price=limit
                        cap=max(0,int(r.volume*.01)//100*100-used.get(s,0))
                        n=min(abs(delta),cap)//100*100
                        previous=daily_notional.get(s,0.)
                        prior_fee=old.fee(previous)
                        if side==1:
                            n=min(n,max(0,int(spendable/price))//100*100)
                            while n>0 and n*price+old.fee(previous+n*price)-prior_fee>spendable+1e-8:n-=100
                        if n<=0:continue
                        fee=old.fee(previous+n*price)-prior_fee
                        state['cash']-=side*n*price+fee
                        state['qty'][s]+=side*n
                        state['fees']+=fee
                        if side==1:spendable-=n*price+fee
                        else:spendable=max(0.,spendable-fee)
                        assert state['cash']>=-1e-6 and state['qty'][s]>=0
                        used[s]=used.get(s,0)+n
                        daily_notional[s]=previous+n*price
                        detail=pending['detail'][s]
                        notional=n*pending['original_ref'][s]
                        detail['filled']+=notional
                        detail['fees']+=fee
                        detail['price_cost']+=side*n*(price-pending['original_ref'][s])
                        detail['delay_notional']+=(pending['age']-1)*notional
                        if pending['age']==1:detail['first_day_filled']+=notional
                        assert detail['filled']<=detail['intended']+1e-6
                        self.execution_events.append(dict(time=str(time),symbol=s,side=side,quantity=n,price=price,fee=fee))


def run_portfolio(minutes):
    weights,bars,close,actions,_=old.load_inputs()
    weights=weights.loc[[d for d in weights.index if len(close.index[close.index>d])>=3]]
    baseline=Replay(weights,bars,close,actions)
    _,_,snapshots=baseline.portfolio(POLICIES[1])
    eligible=[]
    for snapshot in snapshots[1:]:
        p=snapshot[1];first=baseline.location[p['date']]+1
        days=close.index[first:first+3]
        if all(all(date in set(minutes[s].date) for date in days) for s in baseline.symbols):eligible.append(snapshot)
    assert eligible,'no complete strategy windows covered by fresh downloads'
    start=eligible[0][1]['date'];end=close.index[-1]
    results=[]
    for policy in [POLICIES[1],POLICIES[2],POLICIES[4]]:
        engine=MinuteReplay(weights,bars,close,actions)
        engine.prepare(minutes)
        state,pending=deepcopy(eligible[0]);target_weights=pending['weights'].copy()
        start_nav=engine.mark(state,start)['nav']
        history=[engine.mark(state,start,target_weights)];details=[];cycles=0
        for date in close.index[(close.index>start)&(close.index<=end)]:
            engine.apply_actions(state,date,pending)
            if pending:
                engine.execute(state,date,pending,policy)
                if pending['age']>=3:
                    details.extend(deepcopy(list(pending['detail'].values())));cycles+=1;pending=None
            engine.record_rights(state,date)
            if date in engine.schedule:
                assert pending is None
                signal,target_weights,_=engine.schedule[date]
                pending=engine.make_pending(state,date,signal,target_weights)
            history.append(engine.mark(state,date,target_weights))
        assert pending is None
        d=pd.DataFrame(details);h=pd.DataFrame(history)
        focus=d[d.symbol.isin(FOCUS)]
        summary=dict(policy=policy.name,start=str(start.date()),end=str(end.date()),target_cycles=cycles,
            start_nav=start_nav,total_return=h.nav.iloc[-1]/start_nav-1,completion=d.filled.sum()/d.intended.sum(),
            all_orders=len(d),focus_buy_orders=int((focus.side=='BUY').sum()),focus_sell_orders=int((focus.side=='SELL').sum()),
            focus_completion=focus.filled.sum()/focus.intended.sum() if focus.intended.sum() else None,
            minimum_cash=h.cash.min(),fills=len(engine.execution_events),fees=sum(x['fee'] for x in engine.execution_events))
        results.append(summary)
        h.to_csv(HERE/f'server_minute_nav_{policy.name}.csv',index=False)
        d.to_csv(HERE/f'server_minute_orders_{policy.name}.csv',index=False)
        pd.DataFrame(engine.execution_events).to_csv(HERE/f'server_minute_fills_{policy.name}.csv',index=False)
    return results


def main():
    assert str(HERE).startswith('/opt/qmt-server/private/research-runs/'), 'Run only in server research workspace; no local raw downloads.'
    OUT.mkdir(mode=0o700,exist_ok=True)
    symbols=list(old.SYMBOLS.values())
    jobs=[(s,'5m') for s in symbols]+[(s,'1m') for s in FOCUS]
    with ThreadPoolExecutor(max_workers=2) as pool:receipts=list(pool.map(download,jobs))
    fallback=[(r['symbol'],'sina1m') for r in receipts if r['frequency']=='1m' and 'error' in r]
    if fallback:
        with ThreadPoolExecutor(max_workers=2) as pool:receipts.extend(pool.map(download,fallback))
    (HERE/'server_download_receipts.json').write_text(json.dumps(receipts,ensure_ascii=False,indent=2))
    minutes=load_minutes(receipts)
    assert set(minutes)==set(symbols),'incomplete fresh five-minute downloads; do not reuse old archive'
    results=run_portfolio(minutes)
    summary=dict(download_host='qmt',raw_data_kept_remote=True,download_successes=sum('rows' in r for r in receipts),
        downloaded_rows=sum(r.get('rows',0) for r in receipts),five_minute_symbols=len(minutes),
        common_days=len(set.intersection(*[set(x.date) for x in minutes.values()])),portfolio_results=results,
        limits=['Recent public minute history, not seven-year coverage.',
        'Daily seed holdings are reconstructed, not a verified live account.',
        'Minute fills are a model, not a broker-queue replay.',
        'No automatic high-premium gate is tested without historical fair-value/announcement states.',
        'No future bar cash, no zero-volume fills, no fills at/after 14:55.'])
    (HERE/'SERVER_INTRADAY_RESULT.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print('RESULT '+json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
