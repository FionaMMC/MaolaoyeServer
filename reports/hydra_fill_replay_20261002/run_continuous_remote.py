"""Remote summaries only. Continuous raw-price corporate-action accounting."""
from pathlib import Path
import hashlib
import json

import numpy as np

from continuous_replay import next_pair,run
from run_remote import prior


def main():
    weights,daily,close,actions,validation=prior.old.load_inputs()
    valid=[]
    for d in weights.index:
        _,b=next_pair(close.index,d)
        if b is not None and close.index.get_loc(b)+2<len(close):
            valid.append(d)
    weights=weights.loc[valid]
    out=dict(remote_research_dir=str(Path.cwd()),validation=validation,panels=[],
        source_sha256={n:hashlib.sha256(Path(n).read_bytes()).hexdigest() for n in ('continuous_replay.py','run_continuous_remote.py','allocation_code.py','queue_code.py')},
        evaluation=dict(max_cagr_drag=.005,max_cycle_underweight=.02),
        protocol=dict(
            type='Continuous daily-bar SCENARIO model, no monthly resets, NOT actual broker performance or deployed two-phase scheduler',
            baseline='signal_ideal: signal-date official-close fractional shares, zero costs, no borrowing; theoretical same-close benchmark',
            timing_control='pair_ideal: same causal close-sale/next-open timing, frozen fractional targets, zero costs/limits; cash constrained',
            timing='signal available after its dated close; sell strictly later; sell/open pair must be natural adjacent trading days',
            retry='at most 3 exchange sessions from first opening buy; weekends consume no sessions, nonadjacent Monday skips opening buys',
            targets='signal-close NAV and weights frozen; actual target divides by 1.006005 then lot floor; prior filled shares never reversed within cycle',
            fixed='remaining original shares, allocate available cash proportionally at original buy limit before submission',
            replan='source replan_residual at each eligible opening; latest known previous close and actual cash; cannot expand original residual',
            anchors='sell limit signal-close -50bp; opening buy limit first sell-day official close +50bp; neither rolled within cycle',
            prices='sale modeled at official close minus slip, buy at open plus slip, ETF tick rounding, only if within fixed limit',
            finality='base assumes all prior orders reconciled by next eligible open; terminal-delay stress blocks new buy and repeat sell phases',
            liquidity='daily volume participation proxy, not auction capacity; no tick/queue/callback replay; modeled close not guaranteed execution',
            distributions='raw prices + record-date holdings rights + ex-date receivable + scheduled payment cash; actual production payment must be evidenced',
            provenance='frozen reconstructed historical weights; no new claim of PIT signal validity or out-of-sample calibration',
            capital='20万元 and 100万元 TOTAL capital including cash reserve and all idle cash, cash yield assumed zero, zero external flows; NOT current account',
            reserve='0/1/3/5% included in total denominator; main ideal always 0% reserve, pair control has scenario reserve; underweight relative approved cash-aware target, reserve separately disclosed; reserve is spendable within fixed share ceiling, not automatically replenished daily',
        ))
    scenarios=[dict(name='base',capital=200000,slip_bps=5.,participation=.01,terminal_delay=0),
               dict(name='slip25',capital=200000,slip_bps=25.,participation=.01,terminal_delay=0),
               dict(name='capacity01',capital=200000,slip_bps=5.,participation=.001,terminal_delay=0),
               dict(name='terminal_delay1',capital=200000,slip_bps=5.,participation=.01,terminal_delay=1),
               dict(name='capital1m',capital=1000000,slip_bps=5.,participation=.01,terminal_delay=0)]
    scenarios += [dict(name=f'reserve{int(b*100)}pct',capital=200000,slip_bps=5.,participation=.01,terminal_delay=0,cash_buffer=b) for b in (.01,.03,.05)]
    for period,subset in [('full',weights),('recent',weights.loc['2024-10-01':])]:
        for scenario in scenarios:
            args={k:v for k,v in scenario.items() if k!='name'}
            results=[]
            histories={}
            for label in ('signal_ideal','pair_ideal','fixed','replan','reserve_ideal'):
                arm='signal_ideal' if label=='reserve_ideal' else label
                params=dict(args)
                if arm.endswith('ideal'):
                    params['terminal_delay']=0
                if label=='signal_ideal':
                    params['cash_buffer']=0.
                summary,hist,cycles,fills,events=run(subset,daily,close,actions,arm=arm,**params)
                summary['arm']=label
                histories[label]=hist
                stem=f"continuous_{period}_{scenario['name']}_{label}"
                hist.to_csv(stem+'_nav.csv')
                cycles.to_csv(stem+'_cycles.csv',index=False)
                fills.to_csv(stem+'_fills.csv',index=False)
                events.to_csv(stem+'_events.csv',index=False)
                results.append(summary)
            for result in results:
                result['total_return_gap_pp']=(result['total_return']-results[0]['total_return'])*100
                result['cagr_drag_pp']=(results[0]['cagr']-result['cagr'])*100
                result['cagr_drag_vs_pair_pp']=(results[1]['cagr']-result['cagr'])*100
                result['reserve_only_cagr_drag_pp']=(results[0]['cagr']-results[4]['cagr'])*100
                result['implementation_cagr_drag_vs_reserve_pp']=(results[4]['cagr']-result['cagr'])*100
                result['passes_drag']=result['cagr_drag_pp']<=.5
                result['passes_all_cycle_underweight']=result['max_end_underweight']<=.02
                # Monthly tracking-error distribution with persistent drift.
                ratios=(histories[result['arm']].nav/histories['signal_ideal'].nav).resample('ME').last()
                relative=ratios.pct_change().dropna()
                result['worst_month_relative_return']=float(relative.min())
                result['best_month_relative_return']=float(relative.max())
                result['annualized_monthly_tracking_error']=float(relative.std(ddof=1)*np.sqrt(12))
            out['panels'].append(dict(period=period,scenario=scenario,arms=results))
            print(json.dumps({'progress':period+'_'+scenario['name']}),flush=True)
    Path('continuous_summary.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':
    main()
