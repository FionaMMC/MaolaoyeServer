"""Run only inside a fresh qmt research directory; emit aggregate JSON only."""
from collections import defaultdict
from pathlib import Path
import hashlib
import importlib
import json
import math
import sys

import numpy as np
import pandas as pd

from fill_replay import episode


ROOT = Path('/opt/qmt-server/private/research-runs/etf-final-20260928/reports')
sys.path.insert(0, str(ROOT/'execution_final_20260928'))
prior = importlib.import_module('final_suite')

ARMS = ['one_day','fixed_quantity_3d','replan_3d']


def metrics(rows):
    result = []
    for side in ('BUY','SELL','BOTH'):
        selected = [r for r in rows if side == 'BOTH' or r['direction'] == side]
        intended = sum(r['original_reference_notional'] for r in selected)
        filled = sum(r['filled_reference_notional'] for r in selected)
        first = sum(r['day1_reference_notional'] for r in selected)
        full = sum(r['remaining_quantity'] == 0 for r in selected)
        reasons = defaultdict(float)
        for row in selected:
            if row['remaining_quantity']:
                reasons[row['last_reason']] += row['remaining_quantity'] * row['anchor']
        result.append(dict(side=side, original_orders=len(selected), fully_completed_orders=full,
                    full_order_rate=full/len(selected) if selected else None,
                    original_notional=intended, filled_original_notional=filled,
                    original_notional_completion=filled/intended if intended else None,
                    day1_completion=first/intended if intended else None,
                    unfilled_original_notional=intended-filled, last_unfilled_reasons=dict(reasons)))
    return result


def paired_bootstrap(cycles, left, right):
    a=[c for c in cycles if c['arm']==left]
    b=[c for c in cycles if c['arm']==right]
    assert [c['reference_date'] for c in a] == [c['reference_date'] for c in b]
    original=np.array([c['buy_original'] for c in a])
    difference=np.array([x['buy_filled']-y['buy_filled'] for x,y in zip(a,b)])
    if not original.sum():
        return None
    rng=np.random.default_rng(20261002)
    idx=rng.integers(0,len(a),(2000,len(a)))
    denominators=original[idx].sum(axis=1)
    draws=difference[idx].sum(axis=1)[denominators>0]/denominators[denominators>0]*100
    return dict(delta_buy_completion_pp=float(difference.sum()/original.sum()*100),
                paired_cycle_bootstrap_ci95_pp=np.quantile(draws,[.025,.975]).tolist(),
                bootstrap_assumption='resampled historical cycles; not live execution confidence')


def main():
    assert str(Path.cwd()).startswith('/opt/qmt-server/private/research-runs/hydra-fill-')
    weights,daily,close,actions,validation=prior.old.load_inputs()
    def has_three_sessions(signal):
        first=close.index.get_loc(signal)+1
        while first<len(close) and (close.index[first]-close.index[first-1]).days!=1:
            first+=1
        return first+2<len(close)
    weights=weights.loc[[d for d in weights.index if has_three_sessions(d)]]
    source_hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [
        ROOT/'execution_experiment_20260927/backtest.py',
        ROOT/'execution_experiment_20260927/private/hydra_raw.parquet',
        ROOT/'hydra_performance_20260918/private/reconstructed_weights.parquet',
        ROOT/'execution_final_20260928/final_suite.py']}
    out=dict(remote_research_dir=str(Path.cwd()), data_source_hashes=source_hashes,
             replay_source_hashes={n:hashlib.sha256(Path(n).read_bytes()).hexdigest()
                                   for n in ('fill_replay.py','run_remote.py','audit_remote.py')},
             cash_buffer_weight=0.0, code_provenance=json.loads(Path('code_provenance.json').read_text()),
             raw_action_hfq_validation=validation, panels=[], quality=[])
    bases={}
    for capital in (200000,1000000):
        base=prior.Replay(weights,daily,close,actions,capital)
        _,_,snaps=base.portfolio(prior.BASE)
        bases[capital]=(base,snaps)
    for scale in (60,30,5):
        frames,quality=prior.load_frequency(scale,daily)
        out['quality'].extend(quality)
        date_sets={s:set(f['date']) for s,f in frames.items()}
        intraday=defaultdict(lambda:defaultdict(dict))
        for symbol,frame in frames.items():
            for row in frame.itertuples():
                if row.time.strftime('%H:%M') >= '14:55':
                    continue
                intraday[row.date][row.time][symbol]={k:float(getattr(row,k)) for k in ('open','high','low','close','volume')}
        scenarios=[dict(name='base',capital=200000,lag_bars=1,participation=.01,slip_bps=5.,touch=True),
                   dict(name='capital_1m',capital=1000000,lag_bars=1,participation=.01,slip_bps=5.,touch=True),
                   dict(name='lag_2bars',capital=200000,lag_bars=2,participation=.01,slip_bps=5.,touch=True),
                   dict(name='capacity_0_1pct',capital=200000,lag_bars=1,participation=.001,slip_bps=5.,touch=True),
                   dict(name='slip_25bp',capital=200000,lag_bars=1,participation=.01,slip_bps=25.,touch=True),
                   dict(name='bar_open_only',capital=200000,lag_bars=1,participation=.01,slip_bps=5.,touch=False)]
        if scale==30:
            scenarios=[s for s in scenarios if s['name'] in ('base','bar_open_only')]
        for scenario in scenarios:
            base,snaps=bases[scenario['capital']]
            selected=[]
            excluded=defaultdict(int)
            for state,pending in snaps[1:]:  # exclude artificial first investment
                first=base.location[pending['date']]+1
                dates=close.index[first:first+3]
                if len(dates)!=3 or not all(all(d in date_sets.get(s,set()) for d in dates) for s in base.symbols):
                    excluded['incomplete_minute_coverage']+=1
                    continue
                if any(a['ex_date'] in dates or a.get('pay_date') in dates for a in actions):
                    excluded['corporate_action_or_payment_inside_window']+=1
                    continue
                prices={s:float(p) for s,p in close.loc[pending['date']].items() if math.isfinite(p) and p>0}
                positions={s:int(q) for s,q in state['qty'].items() if q}
                nav=state['cash']+sum(q*prices[s] for s,q in positions.items())
                target={s:(math.floor(nav*float(w)/(prices[s]*1.006005)/100)*100 if w>0 else 0)
                        for s,w in pending['weights'].items()}
                snapshot=dict(cash=state['cash'],positions=positions,target=target,anchors=prices,
                              weights={s:float(w) for s,w in pending['weights'].items()})
                selected.append((pending,snapshot,dates))
            assert selected, (scale,scenario)
            summaries=[]
            all_rows=[]
            cycles=[]
            fingerprint=None
            name=f"{scale}m_{scenario['name']}"
            with Path(f'{name}_episodes.jsonl').open('w') as detail_file:
                for arm in ARMS:
                    rows=[]
                    for pending,snapshot,dates in selected:
                        result=episode(snapshot,dates,close,daily,intraday,arm=arm,
                                       **{k:scenario[k] for k in ('lag_bars','participation','slip_bps','touch')})
                        identity=dict(arm=arm,signal_date=str(pending['signal'].date()),reference_date=str(pending['date'].date()))
                        detail_file.write(json.dumps(dict(**identity,**result),allow_nan=False)+'\n')
                        details=[dict(**identity,**r) for r in result['details']]
                        rows.extend(details)
                        buys=[r for r in details if r['direction']=='BUY']
                        cycles.append(dict(**identity,minimum_cash=result['minimum_cash'],
                                      allocation_distance=result['allocation_distance'],ending_cash=result['ending_cash'],
                                      buy_original=sum(r['original_reference_notional'] for r in buys),
                                      buy_filled=sum(r['filled_reference_notional'] for r in buys),
                                      all_original_orders_complete=all(not r['remaining_quantity'] for r in details)))
                    ids=[(r['reference_date'],r['symbol'],r['direction'],r['requested_quantity'],r['anchor']) for r in rows]
                    if fingerprint is None:
                        fingerprint=ids
                    else:
                        assert ids==fingerprint, 'Comparison changed original demand denominator'
                    all_rows.extend(rows)
                    arm_cycles=[c for c in cycles if c['arm']==arm]
                    summaries.append(dict(arm=arm,metrics=metrics(rows),
                        minimum_cash=min(c['minimum_cash'] for c in arm_cycles),
                        full_cycles=sum(c['all_original_orders_complete'] for c in arm_cycles),
                        mean_target_weight_distance=float(np.mean([c['allocation_distance'] for c in arm_cycles])),
                        per_symbol={s:metrics([r for r in rows if r['symbol']==s]) for s in sorted({r['symbol'] for r in rows})}))
            pd.DataFrame(all_rows).to_csv(f'{name}_orders.csv',index=False)
            pd.DataFrame(cycles).to_csv(f'{name}_cycles.csv',index=False)
            panel=dict(scale_minutes=scale,scenario=scenario,cycles=len(selected),
                       first_reference=str(selected[0][0]['date'].date()),
                       last_execution=str(selected[-1][2][-1].date()),excluded_cycles=dict(excluded),
                       original_demand_sha256=hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest(),
                       arms=summaries,paired_new_vs_one_day=paired_bootstrap(cycles,'replan_3d','one_day'),
                       paired_new_vs_fixed_3d=paired_bootstrap(cycles,'replan_3d','fixed_quantity_3d'))
            out['panels'].append(panel)
            print(json.dumps({'progress':name,'cycles':len(selected)}),file=sys.stderr,flush=True)
    out['protocol']={
        'scope':'same-day frozen buy/sell client queue with day-end residual replanning; not undeployed close-sell/next-open scheduler',
        'horizon':'three exchange sessions including first day; adjacent-natural-day reference requirement remains',
        'cash':'whole buy must be affordable before submission; no projected proceeds; source queue cash_readiness function',
        'finality':'all residual active orders assumed broker-confirmed cancelled at each day close before retries',
        'references':'QMT official daily close for planning; original 50bp price envelope fixed',
        'replanning':'source replan_residual function; only original residuals; cash buffer 0% (production monthly target); original share ceiling',
        'fill_model':'adverse open slippage or strict-through limit; per-bar volume cap; never proof of exchange queue fill',
        'cutoff':'bar end strictly before 14:55; coarse final bars excluded conservatively',
        'latency':'sell proceeds observable and spendable next bar, or two bars in stress; NOT a 30-second latency estimate',
        'fees':'actual modeled 1bp minimum CNY5 per symbol/day; submission reserve 10bp minimum CNY5',
        'sample':'frozen reconstructed historical weights; common starts from previous daily baseline model; not actual account or out-of-sample',
        'exclusions':'initial investment, incomplete minute windows, corporate ex/payment events within three-day windows',
        'denominator':'same original requested quantity times original reference price across arms; never reduced after scaling',
        'uncertainty':'no tick trades, order-book queues, broker report timestamps or actual finality latency',
    }
    Path('summary.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps(out,ensure_ascii=False,allow_nan=False))


if __name__=='__main__':
    main()
