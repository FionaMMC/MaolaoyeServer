"""Independent arithmetic audit of remote-only simulated episode details."""
from collections import defaultdict
from pathlib import Path
import json
import math

import pandas as pd


def main():
    summary=json.loads(Path('summary.json').read_text())
    checked=0
    fill_count=0
    for panel in summary['panels']:
        name=f"{panel['scale_minutes']}m_{panel['scenario']['name']}"
        totals=defaultdict(lambda:defaultdict(float))
        original={}
        day_one={}
        for line in Path(f'{name}_episodes.jsonl').read_text().splitlines():
            result=json.loads(line)
            balances=dict(result['starting_positions'])
            cash=result['starting_cash']
            filled=defaultdict(int)
            previous=None
            for fill in result['fills']:
                if previous is not None:
                    assert fill['time']>=previous
                previous=fill['time']
                side=1 if fill['direction']=='BUY' else -1
                filled[fill['symbol']]+=fill['quantity']
                balances[fill['symbol']]=balances.get(fill['symbol'],0)+side*fill['quantity']
                cash-=side*fill['quantity']*fill['price']+fill['fee']
                assert cash>=-1e-6 and balances[fill['symbol']]>=0
                fill_count+=1
            assert math.isclose(cash,result['ending_cash'],abs_tol=1e-7)
            assert balances==result['positions']
            for row in result['details']:
                assert filled[row['symbol']]==row['filled_quantity']
                assert 0<=row['filled_quantity']<=row['requested_quantity']
                assert row['remaining_quantity']==row['requested_quantity']-row['filled_quantity']
                assert math.isclose(row['original_reference_notional'],row['requested_quantity']*row['anchor'])
                identity=(result['reference_date'],row['symbol'],row['direction'])
                if identity in original:
                    assert original[identity]==row['original_reference_notional']
                    assert day_one[identity]==row['day1_quantity']
                else:
                    original[identity]=row['original_reference_notional']
                    day_one[identity]=row['day1_quantity']
                for side in (row['direction'],'BOTH'):
                    key=(result['arm'],side)
                    totals[key]['original']+=row['original_reference_notional']
                    totals[key]['filled']+=row['filled_quantity']*row['anchor']
                    totals[key]['orders']+=1
                    totals[key]['full']+=row['remaining_quantity']==0
            checked+=1
        for arm in panel['arms']:
            for metric in arm['metrics']:
                t=totals[arm['arm'],metric['side']]
                assert math.isclose(metric['original_notional'],t['original'],abs_tol=1e-6)
                assert math.isclose(metric['filled_original_notional'],t['filled'],abs_tol=1e-6)
                assert metric['original_orders']==t['orders']
                assert metric['fully_completed_orders']==t['full']
                if t['original']:
                    assert math.isclose(metric['original_notional_completion'],t['filled']/t['original'])
    orders=pd.read_csv('60m_base_orders.csv')
    refkeys=['reference_date','symbol','direction']
    fixed=orders[orders.arm=='fixed_quantity_3d'].set_index(refkeys)
    new=orders[orders.arm=='replan_3d'].set_index(refkeys)
    differences=[]
    for key in fixed.index:
        delta=float(new.loc[key,'filled_reference_notional']-fixed.loc[key,'filled_reference_notional'])
        if abs(delta)>1e-6:
            differences.append(dict(reference_date=key[0],symbol=key[1],side=key[2],
                                    delta_original_notional=delta,new_last_reason=new.loc[key,'last_reason']))
    receipt=dict(status='PASS',episodes_checked=checked,fills_checked=fill_count,
                 checks=['cash conservation','position conservation','no negative cash or position',
                         'no repeated fills beyond original requested quantity',
                         'same initial denominator and first-day fills across arms',
                         'independent aggregate recomputation'],
                 new_vs_fixed_base_differences=differences)
    Path('audit.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    return receipt


if __name__=='__main__':
    print(json.dumps(main(),ensure_ascii=False))
