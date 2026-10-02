"""Matched-order episodes plus continuous Hydra portfolio execution scenarios.

Offline only. Daily strict-through fills are optimistic, not actual QMT fills.
Uses original monthly signals; no policy is selected by out-of-sample claims.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import math
import sys

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'execution_experiment_20260927'))
import backtest as old

FOCUS = ('518880.SH', '513100.SH', '513500.SH')


@dataclass(frozen=True)
class Policy:
    name: str
    buy_caps: tuple = (50, 50, 50)  # gold, Nasdaq, S&P500
    rolling_focus_buy: bool = False
    sessions: int = 3


POLICIES = [
    Policy('fixed50_1d', sessions=1),
    Policy('fixed50_3d'),
    Policy('rolling50_focus_buy_3d', rolling_focus_buy=True),
    Policy('focus_buy100_3d', buy_caps=(100,100,100)),
    Policy('gold100_us150_3d', buy_caps=(100,150,150)),
]


def limit_reference(pending, symbol, side, policy, previous_close, events):
    ref = pending['ref'][symbol]
    cap = 50
    if side == 1 and symbol in FOCUS:
        cap = policy.buy_caps[FOCUS.index(symbol)]
        if policy.rolling_focus_buy:
            ref = float(previous_close[symbol])
            for a in events:
                if a['symbol'] == symbol:
                    ref = ref/a['factor']-a['cash']
    return ref, cap


class Replay:
    def __init__(self, weights, bars, close, actions, capital=200000., slip=5., adjacent=True):
        self.weights, self.bars, self.close, self.actions = weights, bars, close, actions
        self.symbols = list(weights.columns)
        self.capital, self.slip, self.adjacent = capital, slip, adjacent
        self.dates = close.loc[weights.index.min():].index
        self.location = {d:i for i,d in enumerate(close.index)}
        self.ex, self.record = {}, {}
        for a in actions:
            self.ex.setdefault(a['ex_date'], []).append(a)
            if a['record_date'] is not None:
                self.record.setdefault(a['record_date'], []).append(a)
        self.schedule = {}
        for signal, w in weights.iterrows():
            first = self.location[signal]+1
            while adjacent and (close.index[first]-close.index[first-1]).days != 1:
                first += 1
            assert first+2 < len(close)
            reference_date = close.index[first-1]
            assert reference_date not in self.schedule
            self.schedule[reference_date] = (signal, w, close.index[first])

    def initial_state(self):
        return {'qty':dict.fromkeys(self.symbols,0), 'cash':float(self.capital),
                'rights':{}, 'receivables':[], 'fees':0.}

    def apply_actions(self, state, date, pending=None):
        for a in self.ex.get(date, []):
            s = a['symbol']
            if a['factor'] != 1:
                state['qty'][s] = round(state['qty'][s]*a['factor'])
                if pending:
                    pending['target'][s] = round(pending['target'][s]*a['factor'])
                    pending['ref'][s] /= a['factor']
                    pending['original_ref'][s] /= a['factor']
            if a['cash']:
                entitlement = state['rights'].get((s,a['ex_date']),0)*a['cash']
                state['receivables'].append([a['pay_date'],entitlement])
                if pending:
                    pending['ref'][s] -= a['cash']
        for r in state['receivables']:
            if r[0] <= date:
                state['cash'] += r[1]
                r[1] = 0.

    def record_rights(self, state, date):
        for a in self.record.get(date, []):
            state['rights'][(a['symbol'],a['ex_date'])] = state['qty'][a['symbol']]

    def mark(self, state, date, weights=None):
        values = {s:state['qty'][s]*self.close.loc[date,s] if state['qty'][s] else 0. for s in self.symbols}
        cash_value = state['cash']+sum(r[1] for r in state['receivables'])
        nav = sum(values.values())+cash_value
        assert math.isfinite(nav) and state['cash'] >= -1e-6
        result = {'date':date, 'nav':nav, 'cash':state['cash']}
        if weights is not None:
            result['allocation_distance'] = .5*(sum(abs(values[s]/nav-.99*weights[s]) for s in self.symbols)+abs(cash_value/nav-.01))
            result['focus_underweight'] = sum(max(0.,.99*weights[s]-values[s]/nav) for s in FOCUS if s in weights)
        else:
            result.update(allocation_distance=0.,focus_underweight=0.)
        return result

    def make_pending(self, state, date, signal, weights):
        nav = self.mark(state,date)['nav']
        refs = {s:float(self.close.loc[date,s]) for s in self.symbols}
        target = {s:int(nav*.99*weights[s]/refs[s])//100*100 if weights[s] > 0 else 0 for s in self.symbols}
        detail = {}
        for s in self.symbols:
            delta = target[s]-state['qty'][s]
            if delta:
                detail[s] = {'signal':str(signal.date()), 'reference_date':str(date.date()), 'symbol':s,
                    'side':'BUY' if delta > 0 else 'SELL', 'intended':abs(delta)*refs[s], 'filled':0.,
                    'first_day_filled':0., 'price_cost':0., 'fees':0., 'delay_notional':0.,
                    'initialization':signal == self.weights.index.min()}
        return {'signal':signal, 'date':date, 'age':0, 'target':target, 'ref':refs.copy(),
                'original_ref':refs.copy(), 'detail':detail, 'weights':weights.copy(), 'attempts':0}

    def execute(self, state, date, pending, policy, touch=True, reverse_symbols=False):
        pending['age'] += 1
        initialization = pending['signal'] == self.weights.index.min()
        window = 1 if initialization else policy.sessions
        previous_date = self.close.index[self.location[date]-1]
        permitted = not self.adjacent or (date-previous_date).days == 1
        if pending['age'] > window or not permitted:
            return
        pending['attempts'] += 1
        daily_used = {}
        symbols = list(reversed(self.symbols)) if reverse_symbols else self.symbols
        for phase in (['open','touch'] if touch else ['open']):
            for side in [-1,1]:
                for s in symbols:
                    delta = pending['target'][s]-state['qty'][s]
                    if not delta or int(np.sign(delta)) != side:
                        continue
                    ref, cap_bps = limit_reference(pending,s,side,policy,self.close.loc[previous_date],self.ex.get(date,[]))
                    fill_policy = old.Policy('phase', math.inf if initialization else cap_bps, window, touch and not initialization)
                    bar = self.bars.get((date,s))
                    price = old.price_for_fill(bar,ref,side,fill_policy,self.slip,phase)
                    if price is None:
                        continue
                    cap = int(float(bar['volume'])*100*.01)//100*100
                    n = min(abs(delta),max(0,cap-daily_used.get(s,0)))//100*100
                    if side == 1:
                        n = min(n,max(0,int((state['cash']-5)/price)//100*100))
                        while n > 0 and n*price+old.fee(n*price) > state['cash']+1e-8:
                            n -= 100
                    if n <= 0:
                        continue
                    fee = old.fee(n*price)
                    state['cash'] -= side*n*price+fee
                    state['qty'][s] += side*n
                    state['fees'] += fee
                    assert state['cash'] >= -1e-6 and state['qty'][s] >= 0
                    daily_used[s] = daily_used.get(s,0)+n
                    d = pending['detail'][s]
                    notional = n*pending['original_ref'][s]
                    d['filled'] += notional
                    d['price_cost'] += side*n*(price-pending['original_ref'][s])
                    d['fees'] += fee
                    d['delay_notional'] += (pending['age']-1)*notional
                    if pending['age'] == 1:
                        d['first_day_filled'] += notional
                    assert d['filled'] <= d['intended']+1e-6

    def portfolio(self, policy, touch=True, reverse_symbols=False):
        state = self.initial_state()
        pending = None
        target_weights = None
        snapshots, details, history = [], [], []
        for date in self.dates:
            self.apply_actions(state,date,pending)
            if pending:
                self.execute(state,date,pending,policy,touch,reverse_symbols)
                # Keep expired one-day orders until common day 3 for paired horizons.
                if pending['age'] >= 3:
                    details.extend(deepcopy(list(pending['detail'].values())))
                    pending = None
            self.record_rights(state,date)
            if date in self.schedule:
                assert pending is None
                signal, target_weights, _ = self.schedule[date]
                pending = self.make_pending(state,date,signal,target_weights)
                snapshots.append((deepcopy(state),deepcopy(pending)))
            history.append(self.mark(state,date,target_weights))
        assert pending is None
        return pd.DataFrame(history).set_index('date'), pd.DataFrame(details), snapshots

    def episode(self, snapshot, policy, touch=True, reverse_symbols=False):
        state, pending = deepcopy(snapshot)
        # Immediate reference-price trades, with the same available cash and no
        # fees, form a common opportunity-cost benchmark. Dividend receivables
        # cannot fund trades before payment; scale buys if cash is insufficient.
        ideal = deepcopy(state)
        deltas = {s:pending['target'][s]-ideal['qty'][s] for s in self.symbols}
        for s, delta in deltas.items():
            if delta < 0:
                ideal['cash'] -= delta*pending['original_ref'][s]
                ideal['qty'][s] += delta
        buy_need = sum(max(0,delta)*pending['original_ref'][s] for s,delta in deltas.items())
        ratio = min(1.,ideal['cash']/buy_need) if buy_need else 1.
        for s,delta in deltas.items():
            if delta > 0:
                n = delta if ratio >= 1 else math.floor(delta*ratio/100)*100
                ideal['cash'] -= n*pending['original_ref'][s]
                ideal['qty'][s] += n
        self.record_rights(ideal,pending['date'])
        first = self.location[pending['date']]+1
        history = []
        for date in self.close.index[first:first+3]:
            self.apply_actions(state,date,pending)
            self.apply_actions(ideal,date)
            self.execute(state,date,pending,policy,touch,reverse_symbols)
            self.record_rights(state,date)
            self.record_rights(ideal,date)
            history.append(self.mark(state,date,pending['weights']))
        summary = dict(signal=str(pending['signal'].date()), reference_date=str(pending['date'].date()),
                       initial_nav=self.mark(snapshot[0],snapshot[1]['date'])['nav'],
                       terminal_shortfall=self.mark(ideal,date)['nav']-self.mark(state,date)['nav'],
                       allocation_distance=float(np.mean([r['allocation_distance'] for r in history])),
                       focus_underweight=float(np.mean([r['focus_underweight'] for r in history])),
                       attempts=pending['attempts'],benchmark_buy_scaling=ratio)
        return list(pending['detail'].values()), summary


def grouped(details):
    details = details[~details.initialization]
    rows = []
    groups = {s:details[details.symbol == s] for s in FOCUS}
    groups.update(focus3=details[details.symbol.isin(FOCUS)],other6=details[~details.symbol.isin(FOCUS)],all9=details)
    for group, data in groups.items():
        for side in ['BUY','SELL','BOTH']:
            d = data if side == 'BOTH' else data[data.side == side]
            intended, filled = d.intended.sum(), d.filled.sum()
            rows.append(dict(group=group,side=side,orders=len(d),intended=intended,filled=filled,
                completion=filled/intended if intended else None,
                first_day_completion=d.first_day_filled.sum()/intended if intended else None,
                full_order_share=float((d.filled >= d.intended-1e-6).mean()),
                conditional_price_cost_bps=d.price_cost.sum()/filled*10000 if filled else None,
                conditional_price_and_fee_bps=(d.price_cost.sum()+d.fees.sum())/filled*10000 if filled else None,
                conditional_delay_sessions=d.delay_notional.sum()/filled if filled else None))
    return rows


def run_all():
    weights,bars,close,actions,raw_validation = old.load_inputs()
    weights = weights.loc[[d for d in weights.index if len(close.index[close.index>d]) >= 5]]
    portfolio_rows, metric_rows, episode_rows, nav_rows = [], [], [], []
    # Primary production-date constraints + an all-trading-days comparability check.
    scenarios = [(capital,slip,adjacent,True,False) for capital in [200000.,1000000.] for slip in [5.,25.,50.] for adjacent in [True,False]]
    # Bounding sensitivity for daily touch assumptions and deterministic cash allocation.
    scenarios += [(200000.,5.,True,False,False),(200000.,5.,True,True,True)]
    for capital,slip,adjacent,touch,reverse_symbols in scenarios:
        meta = dict(capital=capital,slip_bps=slip,adjacent=adjacent,touch=touch,reverse_symbols=reverse_symbols)
        engine = Replay(weights,bars,close,actions,capital,slip,adjacent)
        baseline_history,baseline_details,snapshots = engine.portfolio(POLICIES[1],touch,reverse_symbols)
        if not adjacent:
            old_result,old_history,_ = old.run(weights,bars,close,actions,old.Policy('fixed',50,3,True),capital,slip,common_initialization=True)
            assert np.allclose(baseline_history.nav,old_history.nav,rtol=0,atol=1e-7)
            assert np.isclose(baseline_details.filled.sum()/baseline_details.intended.sum(),old_result['modeled_notional_completion'],atol=1e-12)
        episode_denominators = None
        for policy in POLICIES:
            h,d,_ = engine.portfolio(policy,touch,reverse_symbols)
            prior = h.loc[h.index<'2025-01-01','nav'].iloc[-1]
            portfolio_rows.append(dict(**meta,policy=policy.name,total_return=h.nav.iloc[-1]/capital-1,
                return_since_2025=h.nav.iloc[-1]/prior-1,max_drawdown=(h.nav/h.nav.cummax()-1).min(),
                focus_underweight=h.focus_underweight.mean(),allocation_distance=h.allocation_distance.mean(),
                all_completion_including_initialization=d.filled.sum()/d.intended.sum()))
            metric_rows.extend(dict(**meta,policy=policy.name,mode='continuous',**r) for r in grouped(d))
            paired = []
            for snapshot in snapshots[1:]:
                detail,summary = engine.episode(snapshot,policy,touch,reverse_symbols)
                paired.extend(detail)
                episode_rows.append(dict(**meta,policy=policy.name,**summary))
            paired = pd.DataFrame(paired)
            denom = paired[['signal','symbol','side','intended']].reset_index(drop=True)
            if episode_denominators is None:
                episode_denominators = denom
            else:
                pd.testing.assert_frame_equal(denom,episode_denominators)
            metric_rows.extend(dict(**meta,policy=policy.name,mode='matched',**r) for r in grouped(paired))
            if capital == 200000 and slip == 5 and adjacent and touch and not reverse_symbols:
                for date,r in h.iterrows():
                    nav_rows.append(dict(date=date,policy=policy.name,nav=r.nav))
        print('finished',meta,flush=True)
    pd.DataFrame(portfolio_rows).to_csv(HERE/'portfolio_results.csv',index=False)
    pd.DataFrame(metric_rows).to_csv(HERE/'execution_metrics.csv',index=False)
    episodes = pd.DataFrame(episode_rows)
    episodes.to_csv(HERE/'matched_episodes.csv',index=False)
    pd.DataFrame(nav_rows).pivot(index='date',columns='policy',values='nav').to_csv(HERE/'primary_nav.csv')
    # Paired cycle bootstrap for implementation shortfall difference.
    primary = episodes[(episodes.capital==200000)&(episodes.slip_bps==5)&episodes.adjacent&episodes.touch&~episodes.reverse_symbols]
    costs = primary.pivot(index='signal',columns='policy',values='terminal_shortfall')
    rng = np.random.default_rng(20260928)
    evidence = {}
    for name in costs.columns:
        diff = (costs[name]-costs['fixed50_3d']).to_numpy()
        indexes = rng.integers(0,len(diff),size=(5000,len(diff)))
        evidence[name] = {'mean_shortfall_delta_yuan':float(diff.mean()),'paired_cycle_iid_bootstrap_ci95':np.quantile(diff[indexes].mean(axis=1),[.025,.975]).tolist()}
    hashes = {str(p.relative_to(HERE.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [old.HERE/'backtest.py',old.HERE/'private/hydra_raw.parquet',old.PRIOR/'reconstructed_weights.parquet',Path(__file__)]}
    (HERE/'validation.json').write_text(json.dumps(dict(input_hashes=hashes,raw_validation=raw_validation,
        target_count=len(weights),matched_cycles=len(snapshots)-1,portfolio_runs=len(portfolio_rows),
        matched_episode_runs=len(episode_rows),baseline_reproduced=True,matched_denominators_identical=True,
        primary_shortfall_comparison=evidence,notes=['Matched snapshots come from fixed50_3d baseline; selection depends on that path.',
        'IID monthly-cycle bootstrap is exploratory, not a serial-dependence-robust significance claim.',
        'Daily strict-through fills and same-day sell funding are optimistic.',
        'No minute/queue/IOPV evidence. Initial build retained but excluded from execution statistics.',
        'Rolling reference and widened limits apply only to focus ETF buys; all sells and other ETF limits remain fixed50.']),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    run_all()
