"""Reconstruct every continuous NAV from fills and corporate actions."""
from collections import defaultdict
from pathlib import Path
import json
import math

import pandas as pd

from run_remote import prior


def main():
    weights,daily,close,actions,_=prior.old.load_inputs()
    summary=json.loads(Path('continuous_summary.json').read_text())
    ex,record=defaultdict(list),defaultdict(list)
    for action in actions:
        ex[action['ex_date']].append(action)
        if action['record_date'] is not None:
            record[action['record_date']].append(action)
    checked=0
    fill_count=0
    days=0
    worst=[]
    for panel in summary['panels']:
        for result in panel['arms']:
            stem=f"continuous_{panel['period']}_{panel['scenario']['name']}_{result['arm']}"
            history=pd.read_csv(stem+'_nav.csv',parse_dates=['date']).set_index('date')
            fills=pd.read_csv(stem+'_fills.csv')
            cyc=pd.read_csv(stem+'_cycles.csv')
            by_day=defaultdict(list)
            for f in fills.to_dict('records'):
                by_day[pd.Timestamp(f['date'])].append(f)
            qty=defaultdict(float)
            cash=result['capital']
            rights={}
            receivables=[]
            total_fees=0.
            ideal=result['arm'].endswith('ideal')
            for date,row in history.iterrows():
                for a in ex[date]:
                    q=qty[a['symbol']]*a['factor']
                    qty[a['symbol']]=q if ideal else round(q)
                    if a['cash']:
                        receivables.append([a['pay_date'],rights.get((a['symbol'],a['ex_date']),0)*a['cash']])
                for r in receivables:
                    if r[0]<=date:
                        cash+=r[1]
                        r[1]=0.
                for f in by_day[date]:
                    sign=1 if f['direction']=='BUY' else -1
                    q=f['quantity']
                    assert q>0
                    if not ideal:
                        assert abs(q/100-round(q/100))<1e-7
                    qty[f['symbol']]+=sign*q
                    cash-=sign*q*f['price']+f['fee']
                    total_fees+=f['fee']
                    assert cash>=-1e-5 and qty[f['symbol']]>=-1e-5
                    fill_count+=1
                for a in record[date]:
                    rights[a['symbol'],a['ex_date']]=qty[a['symbol']]
                nav=cash+sum(q*close.loc[date,s] for s,q in qty.items() if q)+sum(r[1] for r in receivables)
                assert math.isclose(cash,row.cash,abs_tol=1e-5),(stem,date,'cash')
                assert math.isclose(nav,row.nav,abs_tol=1e-5),(stem,date,'nav')
                assert math.isclose(total_fees,row.fees,abs_tol=1e-6)
                days+=1
            assert math.isclose(history.nav.iloc[-1]/result['capital']-1,result['total_return'],abs_tol=1e-10)
            assert (cyc.buy_filled<=cyc.buy_intended+1e-5).all()
            assert (cyc.sell_filled<=cyc.sell_intended+1e-5).all()
            assert math.isclose(cyc.underweight.max(),result['max_end_underweight'],abs_tol=1e-10)
            checked+=1
            if panel['scenario']['name']=='base' and result['arm']=='replan':
                worst.append(dict(period=panel['period'],largest_underweight_cycles=cyc.nlargest(5,'underweight').to_dict('records'),
                    max_excluding_initial=float(cyc.iloc[1:].underweight.max()),
                    failures_excluding_initial=int((cyc.iloc[1:].underweight>.02).sum())))
    receipt=dict(status='PASS',portfolios_checked=checked,valuation_days_checked=days,fills_checked=fill_count,
        checks=['independent cash/position/dividend reconstruction','all daily NAV reconciled','no negative cash/position',
                'total-capital return denominator','whole lots in modeled executions','no fills above original cycle amount',
                'aggregate underweight independently recomputed'],worst_cycles=worst)
    Path('continuous_audit.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(receipt,ensure_ascii=False))


if __name__=='__main__':
    main()
