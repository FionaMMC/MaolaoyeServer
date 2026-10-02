"""Aggregate independent paired comparisons; never export raw inputs."""
from pathlib import Path
import json
import hashlib
import numpy as np
import pandas as pd
from final_suite import HERE, data, Daily, Intraday, BASE, MAIN, FOCUS, load_frequency, eligible_snapshots, compare_episodes, clean, save_json


def main():
    assert str(HERE).startswith('/opt/qmt-server/private/research-runs/')
    w,b,c,a,_=data();e=Daily(w,b,c,a)
    _,_,snapshots=e.portfolio(BASE)
    results={'same_sample_daily':[],'gap_distributions':[],'paired_shortfall_intervals':[]}
    for scale in [15,30,60]:
        frames,_=load_frequency(scale,b)
        snaps=eligible_snapshots(e,snapshots,frames)
        if not snaps:continue
        m,s=compare_episodes(e,snaps,MAIN,f'audit_daily_same{scale}')
        results['same_sample_daily'].append(dict(scale=scale,cycles=len(snaps),metrics=m))
    # Unfilled orders enter implementation shortfall; block resampling keeps
    # nearby monthly observations together. Exploratory, no OOS claim.
    for name in ['daily_primary','minute15_primary','minute30_primary','minute60_primary']:
        f=pd.read_csv(HERE/f'{name}_cycle_detail.csv')
        wide=f.pivot(index='signal',columns='policy',values='terminal_shortfall')
        rng=np.random.default_rng(20260928);n=len(wide)
        starts=rng.integers(0,n,size=(5000,(n+2)//3))
        ix=((starts[:,:,None]+np.arange(3))%n).reshape(5000,-1)[:,:n]
        for p in wide:
            delta=(wide[p]-wide[BASE.name]).to_numpy()
            results['paired_shortfall_intervals'].append(dict(panel=name,policy=p,cycles=n,
                delta_mean_yuan=float(delta.mean()),block3_ci95_yuan=np.quantile(delta[ix].mean(axis=1),[.025,.975]).tolist()))
    for symbol in w.columns:
        values=[]
        for i,date in enumerate(c.index[1:],1):
            if date<w.index.min():continue
            bar=b.get((date,symbol));previous=c.index[i-1]
            if bar is None or bar['volume']<=0:continue
            reference=float(c.loc[previous,symbol])
            if not np.isfinite(reference) or reference<=0 or not np.isfinite(bar['open']):continue
            for event in a:
                if event['symbol']==symbol and event['ex_date']==date:reference=reference/event['factor']-event['cash']
            values.append(bar['open']/reference-1)
        v=np.array(values)
        results['gap_distributions'].append(dict(symbol=symbol,days=len(v),mean_abs_gap_bps=float(np.abs(v).mean()*10000),
            abs_gap_p95_bps=float(np.quantile(np.abs(v),.95)*10000),
            open_above_prev50_share=float((v>.005).mean()),open_above_prev100_share=float((v>.01).mean()),
            open_above_prev150_share=float((v>.015).mean())))
    results['file_checks']={}
    for name in ['daily_summary.json','intraday_summary.json','opportunity_summary.json','final_suite.py','test_final_suite.py','audit_results.py']:
        path=HERE/name
        results['file_checks'][name]=dict(bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    save_json(HERE/'audit_summary.json',clean(results))
    print(json.dumps({'audited':len(results['file_checks']),'daily_same_panels':len(results['same_sample_daily'])}))


if __name__=='__main__':main()
