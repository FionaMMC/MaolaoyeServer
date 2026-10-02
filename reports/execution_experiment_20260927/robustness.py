"""Post-primary-result robustness: common seed portfolio and paired month blocks."""
import json
import numpy as np
import pandas as pd
from backtest import HERE, POLICIES, load_inputs, run

w,b,c,a,v=load_inputs()
out=[]
for slip in [5,50]:
    for policy in POLICIES:
        r,h,cycles=run(w,b,c,a,policy,200000.,slip,common_initialization=True)
        out.append(r)
pd.DataFrame(out).to_csv(HERE/'common_initialization_results.csv',index=False)
n=pd.read_csv(HERE/'nav_comparison.csv',index_col=0,parse_dates=True)
months=np.log(n.resample('ME').last()).diff().dropna()
rng=np.random.default_rng(20260927)
result={}
for candidate,baseline in [(p,b) for p in ['cap50_3d','cap100_3d','arrival_1d'] for b in ['frozen_1d_touch','frozen50_1d_touch']]:
    diff=(months[candidate]-months[baseline]).to_numpy()
    # Circular 3-month block bootstrap, paired by month, 10k replicates.
    starts=rng.integers(0,len(diff),size=(10000,int(np.ceil(len(diff)/3))))
    indexes=((starts[:,:,None]+np.arange(3))%len(diff)).reshape(10000,-1)[:,:len(diff)]
    annualized=np.expm1(diff[indexes].mean(axis=1)*12)
    result[candidate+'_vs_'+baseline]={'months':len(diff),'paired_annualized_log_difference_equivalent':float(np.expm1(diff.mean()*12)),
        'block_bootstrap_95pct_interval':np.quantile(annualized,[.025,.975]).tolist(),
        'note':'Exploratory paired execution comparison; excludes initial partial month. Not independent strategy OOS and not adjusted for multiple candidates.'}
(HERE/'robustness.json').write_text(json.dumps({'common_initialization':out,'paired_month_bootstrap':result},indent=2))
print(pd.DataFrame(out).query('slip_bps==5')[['policy','total_return','return_since_2025','modeled_notional_completion']].to_string(index=False))
print(json.dumps(result,indent=2))
