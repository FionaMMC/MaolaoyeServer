"""Read-only analysis on production: output aggregates only, never raw account records."""
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

def regression(y, factors, lags=3):
    """OLS with uncorrected Bartlett/Newey-West covariance (normal 95% CI)."""
    X = np.column_stack([np.ones(len(y)), factors.to_numpy()])
    y = np.asarray(y, dtype=float)
    if len(y) <= X.shape[1] or np.linalg.matrix_rank(X) < X.shape[1]:
        raise ValueError('insufficient observations or rank-deficient design')
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    resid = y-X@beta
    xu = X*resid[:,None]
    meat = xu.T@xu
    for lag in range(1,min(lags,len(y)-1)+1):
        cross = xu[lag:].T@xu[:-lag]
        meat += (1-lag/(lags+1))*(cross+cross.T)
    bread = np.linalg.inv(X.T@X)
    se = np.sqrt(np.maximum(np.diag(bread@meat@bread),0))
    names = ['const']+list(factors.columns)
    tss = np.sum((y-y.mean())**2)
    return {'n':len(y),'r2':float(1-np.sum(resid**2)/tss) if tss else None,
            'coefficients':dict(zip(names,beta.tolist())),
            'ci95':dict(zip(names,np.column_stack([beta-1.95996398454*se,beta+1.95996398454*se]).tolist())),
            'design_condition_number':float(np.linalg.cond(X))},resid

ROOT = Path('/opt/qmt-server/v2.3/server')
ACTIVE = ['paper_v20h_v20h_v1_3','paper_v79_v713_relay','paper_v53_v53','live_hydra_v481_rb']
db = sqlite3.connect(f'file:{ROOT}/pipeline-server.db?mode=ro', uri=True)
db.execute('PRAGMA query_only=ON')
db.execute('BEGIN')
nav = pd.read_sql_query('SELECT instance_id,date,nav FROM perf_snapshots', db)
cash = pd.read_sql_query("SELECT instance_id,event_date,amount FROM cash_flow_journal WHERE status='APPLIED' AND event_type IN ('DEPOSIT','WITHDRAWAL','CAPITAL_ALLOCATION','CAPITAL_DEALLOCATION')", db)
prices = {}
for path in (ROOT/'data'/'market'/'daily'/'indexes').glob('*.parquet'):
    frame = pd.read_parquet(path)
    if 'trade_date' not in frame or 'close' not in frame:
        continue
    frame['date'] = frame.trade_date.astype(str)
    prices[path.stem] = frame.drop_duplicates('date').set_index('date').close.sort_index()
result = {
    'collected_at': datetime.now(timezone.utc).isoformat(),
    'commit': subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
    'benchmarks': {k: {'first':v.index.min(),'last':v.index.max(),'n':len(v)} for k,v in prices.items()},
    'series': {}, 'comparisons': {},
}
series = {i: nav[nav.instance_id == i].set_index('date').nav.sort_index() for i in ACTIVE}
def interval_returns(instance, dates):
    s = series[instance].reindex(dates)
    ret = s.pct_change(fill_method=None)
    flows = cash[cash.instance_id == instance]
    for j in range(1,len(dates)):
        f = flows[(flows.event_date > dates[j-1]) & (flows.event_date <= dates[j])].amount.sum()
        ret.iloc[j] -= f / s.iloc[j-1]
    return ret.iloc[1:]

def stats(ret):
    curve = pd.concat([pd.Series([1.0]), (1+ret).cumprod().reset_index(drop=True)], ignore_index=True)
    return {'intervals':len(ret), 'return':float(curve.iloc[-1]-1),'max_drawdown':float((curve/curve.cummax()-1).min()),'worst_interval':float(ret.min()),'best_interval':float(ret.max())}

for i,s in series.items():
    dates = s.index.tolist()
    result['series'][i] = {'first':dates[0], 'last':dates[-1], 'points':len(s), **stats(interval_returns(i,dates))}

quality = pd.read_sql_query('SELECT instance_id,date,stale_mark_count,missing_mark_count,pricing_coverage FROM daily_risk_snapshots',db)
groups = {'stock_pair':ACTIVE[:2], 'stock_pair_fresh_marks':ACTIVE[:2], 'paper_three':ACTIVE[:3], 'all_four':ACTIVE}
for name, ids in groups.items():
    common = sorted(set.intersection(*(set(series[i].index) for i in ids)))
    # Join benchmark endpoints BEFORE taking returns. No missing-day zero fills.
    if '000852.SH' in prices and '000300.SH' in prices:
        common = sorted(set(common)&set(prices['000852.SH'].index)&set(prices['000300.SH'].index))
    if name == 'stock_pair_fresh_marks':
        for i in ids:
            good = quality[(quality.instance_id == i) & (quality.stale_mark_count == 0) & (quality.missing_mark_count == 0) & (quality.pricing_coverage == 1)]
            common = sorted(set(common)&set(good.date))
    if len(common) < 3:
        continue
    returns = pd.DataFrame({i:interval_returns(i,common) for i in ids})
    out = {'start':common[0], 'end':common[-1], 'points':len(common), 'metrics':{i:stats(returns[i]) for i in ids}, 'correlation':returns.corr().to_dict()}
    if '000852.SH' in prices and '000300.SH' in prices:
        market = prices['000300.SH'].reindex(common).pct_change(fill_method=None).iloc[1:]
        small = prices['000852.SH'].reindex(common).pct_change(fill_method=None).iloc[1:]
        out['benchmarks'] = {'csi300':stats(market),'csi1000':stats(small)}
        out['regression'] = {}
        residuals = {}
        for i in ids:
            X = pd.DataFrame({'market_csi300':market,'size_spread_1000_minus_300':small-market})
            fit,resid = regression(returns[i],X)
            simple,_ = regression(returns[i],pd.DataFrame({'csi1000':small}))
            residuals[i] = resid
            out['regression'][i] = fit | {'csi1000_beta':simple['coefficients']['csi1000'],'csi1000_r2':simple['r2'], 'csi1000_beta_ci95':simple['ci95']['csi1000'], 'csi1000_correlation':float(returns[i].corr(small))}
        if ids[:2] == ACTIVE[:2]:
            difference = returns[ids[0]] - returns[ids[1]]
            fit_difference,_ = regression(difference, pd.DataFrame({'csi1000':small}))
            size_difference,_ = regression(difference, X)
            out['paired_beta_difference_v20h_minus_v713'] = fit_difference
            out['paired_factor_difference_v20h_minus_v713'] = size_difference
            negative = small < 0
            out['csi1000_down_intervals'] = {'n':int(negative.sum()), 'mean_csi1000':float(small[negative].mean()), 'strategies':{i:{'mean_return':float(returns.loc[negative,i].mean()),'negative_fraction':float((returns.loc[negative,i]<0).mean())} for i in ids}}
        out['residual_correlation_after_market_size'] = pd.DataFrame(residuals).corr().to_dict()
    result['comparisons'][name] = out

# Nested regressions use identical endpoints; R-squared is variance explained,
# never a percentage of profits. Raw returns and holdings stay on the server.
result['nested_attribution'] = {}
for group in ('stock_pair', 'stock_pair_fresh_marks'):
    common = sorted(set(series[ACTIVE[0]].index) & set(series[ACTIVE[1]].index)
                    & set(prices['000852.SH'].index) & set(prices['000300.SH'].index))
    if group.endswith('fresh_marks'):
        for instance in ACTIVE[:2]:
            good = quality[(quality.instance_id == instance)
                           & (quality.stale_mark_count == 0)
                           & (quality.missing_mark_count == 0)
                           & (quality.pricing_coverage == 1)]
            common = sorted(set(common) & set(good.date))
    if len(common) < 6:
        result['nested_attribution'][group] = {'status': 'insufficient_endpoints'}
        continue
    y = interval_returns(ACTIVE[0], common)
    v713 = interval_returns(ACTIVE[1], common)
    market = prices['000300.SH'].reindex(common).pct_change(fill_method=None).iloc[1:]
    small = prices['000852.SH'].reindex(common).pct_change(fill_method=None).iloc[1:]
    factors = pd.DataFrame({'market_csi300': market,
                            'size_spread_1000_minus_300': small - market,
                            'v713_whole_book': v713})
    if not np.isfinite(np.column_stack([y, factors])).all():
        raise ValueError('nonfinite aligned returns; refusing imputation')
    models = {}
    for model, columns in {
        'market_only': ['market_csi300'],
        'market_size': ['market_csi300', 'size_spread_1000_minus_300'],
        'v713_only': ['v713_whole_book'],
        'market_size_v713': list(factors.columns),
    }.items():
        fit, residual = regression(y, factors[columns])
        fit['mean_interval_return_components'] = {
            key: float(fit['coefficients'][key] * factors[key].mean()) for key in columns
        } | {'intercept': fit['coefficients']['const'],
             'mean_residual': float(residual.mean()), 'observed_mean': float(y.mean())}
        assert np.isclose(sum(v for k, v in fit['mean_interval_return_components'].items()
                              if k != 'observed_mean'), y.mean(), atol=1e-12)
        models[model] = fit
    _, ry = regression(y, factors.iloc[:, :2])
    _, rv = regression(v713, factors.iloc[:, :2])
    total_cov = float(np.cov(y, v713, ddof=1)[0, 1])
    shared_cov = float(np.cov(y.to_numpy() - ry, v713.to_numpy() - rv, ddof=1)[0, 1])
    residual_cov = float(np.cov(ry, rv, ddof=1)[0, 1])
    assert np.isclose(total_cov, shared_cov + residual_cov, atol=1e-14)
    r2joint = models['market_size_v713']['r2']
    r2base = models['market_size']['r2']
    r2v = models['v713_only']['r2']
    result['nested_attribution'][group] = {
        'start': common[0], 'end': common[-1], 'intervals': len(y),
        'models': models,
        'incremental_r2_v713_after_market_size': r2joint - r2base,
        'incremental_r2_market_size_after_v713': r2joint - r2v,
        'shared_explained_variance_commonality': r2base + r2v - r2joint,
        'unexplained_variance_fraction': 1 - r2joint,
        'v20h_v713_covariance_decomposition': {
            'total': total_cov, 'market_size_shared': shared_cov,
            'residual': residual_cov,
            'market_size_fraction': shared_cov / total_cov if total_cov else None,
        },
        'interval_calendar_days': {
            'min': min((pd.Timestamp(b) - pd.Timestamp(a)).days
                       for a, b in zip(common, common[1:])),
            'max': max((pd.Timestamp(b) - pd.Timestamp(a)).days
                       for a, b in zip(common, common[1:])),
        },
    }

import hashlib
source_root = Path('/opt/qmt-refresh/releases/small_cap_peer-88c2cb1-roe-pit-nullfix-20260809')
result['source_hashes_only'] = {}
for source in [source_root/'common.py', source_root/'round4/v7.13/baseline.py',
               ROOT/'plugins/v20h_adapter.py', ROOT/'plugins/v20h/strategy.py',
               ROOT/'app/services/shadow_ledger.py',
               ROOT/'app/services/hydra_execution_policy.py']:
    result['source_hashes_only'][str(source)] = (
        hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None)
result['method'] = {'cash_flow_adjustment': 'observed external flows treated as end-of-interval',
                    'hac': 'Newey-West Bartlett 3 lags; normal 95% CI; exploratory short sample',
                    'size_proxy': 'CSI1000 price return minus CSI300 price return; not pure SMB',
                    'data_policy': 'production SQLite mode=ro/query_only; no raw return export',
                    'alpha': 'per-observation intercept; no annualization or causal claim'}
db.rollback()
db.close()
print(json.dumps(result, ensure_ascii=False, allow_nan=False, default=str))
