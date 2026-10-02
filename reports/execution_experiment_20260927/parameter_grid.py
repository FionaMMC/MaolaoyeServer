"""Follow-up requested by user: compare 1/2/3/5 sessions at 0.25/0.5/1/2%.

All policies use the identical target set; the last target is excluded for every
policy when fewer than five execution sessions remain. No short-window censoring.
"""
import json
import pandas as pd
from backtest import HERE, Policy, load_inputs, run

w,b,c,a,v=load_inputs()
eligible=[d for d in w.index if len(c.index[c.index>d])>=5]
w=w.loc[eligible]
rows=[]
for capital in [200000.,1000000.]:
    for slip in [5,25,50]:
        for bp in [25,50,100,200]:
            for days in [1,2,3,5]:
                policy=Policy(f'guard{bp}_{days}d',bp,days,True)
                result,nav,cycles=run(w,b,c,a,policy,capital,slip,common_initialization=True)
                result.update(price_cap_bps=bp,window_sessions=days)
                rows.append(result)
pd.DataFrame(rows).to_csv(HERE/'parameter_grid.csv',index=False)
best=pd.DataFrame(rows).query('capital==200000 and slip_bps==5')
print(best[['price_cap_bps','window_sessions','modeled_notional_completion','total_return','return_since_2025','max_drawdown']].to_string(index=False))
(HERE/'parameter_grid_design.json').write_text(json.dumps({'target_count':len(w),'last_target':str(w.index[-1].date()),'sessions':[1,2,3,5],
    'cap_bps':[25,50,100,200],'capital':[200000,1000000],'slip_bps':[5,25,50],
    'common_initialization':True,'touch_assumption':'optimistic daily strict-through bound',
    'last_cycle_rule':'all policies require five available future sessions, same targets for every run',
    'selection_status':'post-primary-result sensitivity; not independently held-out parameter tuning'},indent=2))
