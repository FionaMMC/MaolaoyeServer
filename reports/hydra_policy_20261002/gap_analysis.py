"""Read-only: what the signal-day -> sell-day gap costs, by gap type (colleague question, 10/8).

Runs inside the hydra-policy research dir. Frictionless arms isolate pure waiting:
signal_ideal trades the frozen target at the signal close, pair_ideal trades the same
target at the sell-day close / next-day open. C3 per-cycle results come from grid 3.
"""
import json
import math
from pathlib import Path

import pandas as pd

import run_policy_grid as base
from policy_replay import next_pair, run

assert str(Path.cwd()).startswith('/opt/qmt-server/private/research-runs/hydra-policy-')
weights, daily, close, actions, _ = base.source.load_inputs()
common = base.horizon_filter(weights, close, 5)
_, sig_hist, _, _, _ = run(common, daily, close, actions, arm='signal_ideal', capital=200000)
_, pair_hist, _, _, _ = run(common, daily, close, actions, arm='pair_ideal', capital=200000)
c3 = pd.read_csv('g3_full_base_C3_50_200_cycles.csv').set_index('signal')
dates, ratio, signals = close.index, pair_hist.nav / sig_hist.nav, list(common.index)
tradable = ['510300.SH', '159915.SZ', '511260.SH', '518880.SH', '159981.SZ', '159985.SZ', '159930.SZ',
            '513500.SH', '513100.SH']
overseas = {'518880.SH', '513500.SH', '513100.SH'}

rows, drifts = [], []
for i, t in enumerate(signals):
    s, b = next_pair(dates, t)
    later = signals[i + 1] if i + 1 < len(signals) else ratio.index[-1]
    trading_gap, calendar_gap = dates.get_loc(s) - dates.get_loc(t), (s - t).days
    kind = 'holiday' if calendar_gap >= 5 else ('pushed' if trading_gap >= 2 else 'next_day')
    key = str(t.date())
    r = c3.loc[key] if key in c3.index else None
    rows.append(dict(
        signal=key, sell=str(s.date()), buy=str(b.date()), trading_gap=int(trading_gap),
        calendar_gap=int(calendar_gap), kind=kind,
        wait_bp=math.log(ratio.loc[later] / ratio.loc[t]) * 1e4,       # + means waiting helped
        c3_exec_uw=None if r is None else float(r.exec_underweight),
        c3_sell_guard=None if r is None else float(r.get('cause_SELL_PRICE_GUARD', 0) or 0),
        c3_sell_fill=None if r is None or not r.sell_intended else float(r.sell_filled / r.sell_intended)))
    for sym in tradable:
        p0, p1 = close.loc[t].get(sym), close.loc[s].get(sym)
        if p0 and p1 and math.isfinite(p0) and math.isfinite(p1):
            drifts.append(dict(kind=kind, overseas=sym in overseas, move=p1 / p0 - 1))

df, dr = pd.DataFrame(rows), pd.DataFrame(drifts)
years = (ratio.index[-1] - ratio.index[0]).days / 365.25
groups = {}
for kind, g in df.groupby('kind'):
    d = dr[dr.kind == kind]
    groups[kind] = dict(
        cycles=len(g), wait_mean_bp=g.wait_bp.mean(), wait_std_bp=g.wait_bp.std(), wait_worst_bp=g.wait_bp.min(),
        wait_best_bp=g.wait_bp.max(), wait_pp_per_year=g.wait_bp.sum() / 100 / years,
        c3_exec_uw_mean=g.c3_exec_uw.mean(), c3_cycles_uw_over_2pct=int((g.c3_exec_uw > .02).sum()),
        c3_cycles_sell_guard_hit=int((g.c3_sell_guard > 1e-9).sum()),
        share_below_guard_domestic=float((d[~d.overseas].move < -.005).mean()),
        share_below_guard_overseas=float((d[d.overseas].move < -.005).mean()),
        mean_abs_move_overseas=float(d[d.overseas].move.abs().mean()),
        mean_abs_move_domestic=float(d[~d.overseas].move.abs().mean()))
# 2019-12 .. 2020-01 cycles were limited by ETF capacity (initial build, thin volume), not by waiting.
settled = df[df.signal >= '2020-03-01']
for kind, g in settled.groupby('kind'):
    groups[kind].update(cycles_from_2020_03=len(g), c3_exec_uw_mean_from_2020_03=g.c3_exec_uw.mean(),
                        c3_exec_uw_max_from_2020_03=g.c3_exec_uw.max(),
                        c3_cycles_uw_over_2pct_from_2020_03=int((g.c3_exec_uw > .02).sum()))
out = dict(years=years, groups=groups, holiday_cycles=df[df.kind == 'holiday'].to_dict('records'),
           september_cycles=df[df.signal.str[5:7] == '09'].to_dict('records'), all_cycles=df.to_dict('records'))
Path('gap_analysis_20261008.json').write_text(json.dumps(out, indent=2, default=float))
print(json.dumps(out, indent=1, default=lambda x: round(float(x), 5)))
