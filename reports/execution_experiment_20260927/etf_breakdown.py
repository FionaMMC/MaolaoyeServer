"""Attribute the existing portfolio replay without changing its execution rules.

In-memory instrumentation records each cycle's reference-notional by asset/side.
Assertions reconcile attribution to the unmodified engine and saved grid.
"""
import inspect
import json
import numpy as np
import pandas as pd
import backtest as bt

HERE = bt.HERE
FOCUS = {'518880.SH', '513100.SH', '513500.SH'}


def instrument():
    src = inspect.getsource(bt.run)
    replacements = [
        ('qty={s:0 for s in symbols};cash=float(capital);pending=None',
         'qty={s:0 for s in symbols};cash=float(capital);pending=None; details=[]'),
        ("pending['filled']+=n*pending['original_ref'][s]",
         "pending['filled']+=n*pending['original_ref'][s]\n                        pending['detail'][s]['filled']+=n*pending['original_ref'][s]"),
        ("cycles.append({k:pending[k] for k in ['date','intended','filled']});pending=None",
         "details.extend(pending['detail'].values());cycles.append({k:pending[k] for k in ['date','intended','filled']});pending=None"),
        ("'intended':intended,'filled':0.,'daily_used':{}}",
         "'intended':intended,'filled':0.,'daily_used':{}, 'detail':{s:{'date':date,'symbol':s,'side':'BUY' if target[s]>qty[s] else 'SELL','intended':abs(target[s]-qty[s])*ref[s],'filled':0.} for s in symbols if target[s]!=qty[s]}}"),
        ("if pending:cycles.append({k:pending[k] for k in ['date','intended','filled']})",
         "if pending:details.extend(pending['detail'].values());cycles.append({k:pending[k] for k in ['date','intended','filled']})"),
        ("},hist,c\n", "},hist,c,pd.DataFrame(details)\n"),
    ]
    for old, new in replacements:
        assert src.count(old) == 1, old
        src = src.replace(old, new)
    namespace = vars(bt).copy()
    exec(compile(src, '<attribution-only-run>', 'exec'), namespace)
    return namespace['run']


def main():
    weights, bars, close, actions, _ = bt.load_inputs()
    weights = weights.loc[[d for d in weights.index if len(close.index[close.index > d]) >= 5]]
    attributed = instrument()
    baseline = pd.read_csv(HERE/'parameter_grid.csv')
    rows, validations = [], []
    for bp, days in [(50, 1), (50, 3), (100, 3), (150, 3)]:
        policy = bt.Policy(f'guard{bp}_{days}d', bp, days, True)
        result, nav, cycles, detail = attributed(weights, bars, close, actions, policy, common_initialization=True)
        original, original_nav, original_cycles = bt.run(weights, bars, close, actions, policy, common_initialization=True)
        assert result == original
        pd.testing.assert_frame_equal(nav, original_nav)
        pd.testing.assert_frame_equal(cycles, original_cycles)
        assert np.isclose(detail.intended.sum(), cycles.intended.sum())
        assert np.isclose(detail.filled.sum(), cycles.filled.sum())
        assert (detail.filled <= detail.intended + 1e-6).all()
        saved = baseline.query('capital == 200000 and slip_bps == 5 and price_cap_bps == @bp and window_sessions == @days')
        if len(saved):
            for key in ['modeled_notional_completion', 'total_return']:
                assert np.isclose(result[key], saved.iloc[0][key], atol=1e-12)
        validations.append({'policy': policy.name, 'engine_exact_match': True, 'saved_grid_match': bool(len(saved)), 'portfolio_return':result['total_return']})
        for sample, data in [('all_81_targets', detail), ('exclude_common_initialization', detail[detail.date != weights.index.min()])]:
            groups = {s: data[data.symbol == s] for s in weights.columns}
            groups.update(focus3=data[data.symbol.isin(FOCUS)], other6=data[~data.symbol.isin(FOCUS)], all9=data)
            for group, group_data in groups.items():
                for side in ['BOTH', 'BUY', 'SELL']:
                    d = group_data if side == 'BOTH' else group_data[group_data.side == side]
                    intended, filled = d.intended.sum(), d.filled.sum()
                    rows.append(dict(policy=policy.name, sample=sample, group=group, side=side,
                                     orders=len(d), intended=intended, filled=filled,
                                     completion=filled/intended if intended else None,
                                     fully_completed_order_share=float((d.filled >= d.intended-1e-6).mean())))
    output = pd.DataFrame(rows)
    output.to_csv(HERE/'etf_breakdown.csv', index=False)
    # Separate all-date price accessibility diagnostic; no portfolio/cash model.
    raw = pd.read_parquet(HERE/'private/hydra_raw.parquet')
    raw['date'] = pd.to_datetime(raw.trade_date.astype(str))
    raw = raw.sort_values(['symbol','date'])
    raw['previous_close'] = raw.groupby('symbol').close.shift()
    raw['previous_date'] = raw.groupby('symbol').date.shift()
    sample = raw[(raw.date.between('2020-01-17','2026-08-28')) & ((raw.date-raw.previous_date).dt.days == 1)].copy()
    sample['gap_bps'] = (sample.open/sample.previous_close-1)*10000
    # Exclude split/ex-dividend sessions to isolate overnight market movement.
    event_dates = {(a['symbol'], a['ex_date']) for a in actions}
    sample = sample[[ (r.symbol,r.date) not in event_dates for r in sample.itertuples() ]].copy()
    sample['buy_limit'] = np.floor(sample.previous_close*1.005/.001+1e-8)*.001
    sample['buy_open'] = sample.open <= sample.buy_limit + 1e-9
    sample['buy_touch'] = sample.low <= sample.buy_limit + 1e-9
    gap_rows = []
    for symbol, d in sample.groupby('symbol'):
        gap_rows.append(dict(symbol=symbol, observations=len(d), mean_absolute_gap_bps=d.gap_bps.abs().mean(),
                             p95_absolute_gap_bps=d.gap_bps.abs().quantile(.95), buy_open=d.buy_open.mean(), buy_touch=d.buy_touch.mean()))
    pd.DataFrame(gap_rows).to_csv(HERE/'etf_gap_diagnostic.csv',index=False)
    # Date-paired group difference, moving-block bootstrap retains local dependence.
    touch = sample.pivot(index='date',columns='symbol',values='buy_touch').dropna().astype(float)
    diff = (touch[list(FOCUS)].mean(axis=1)-touch[[s for s in touch if s not in FOCUS]].mean(axis=1)).to_numpy()
    rng = np.random.default_rng(20260927)
    draws = []
    for _ in range(5000):
        starts = rng.integers(0,len(diff),size=(len(diff)+9)//10)
        indexes = ((starts[:,None]+np.arange(10))%len(diff)).ravel()[:len(diff)]
        draws.append(diff[indexes].mean())
    evidence = {'validations':validations, 'gap_sample':'2020-01-17 through 2026-08-28, adjacent calendar dates, exclude known corporate actions',
                'paired_group_buy_touch':{'dates':len(diff),'focus_minus_other':float(diff.mean()),'ci95_block10':np.quantile(draws,[.025,.975]).tolist(), 'bootstrap_samples':5000},
                'limitations':['Daily touches are optimistic accessibility proxies, not queue fills.', 'Portfolio scenarios have endogenous turnover denominators.', 'Original report uses rolling price anchors; this replay fixes the original anchor.', 'Daily replay does not enforce the production adjacent-calendar-day submission restriction.']}
    (HERE/'etf_breakdown_validation.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    print(output.query("sample == 'all_81_targets' and group in ['518880.SH','513100.SH','513500.SH','focus3','other6','all9']")[['policy','group','side','orders','completion']].to_string(index=False))
    print(json.dumps(evidence,ensure_ascii=False,indent=2))
    print(pd.DataFrame(gap_rows).to_string(index=False))


if __name__ == '__main__':
    main()
