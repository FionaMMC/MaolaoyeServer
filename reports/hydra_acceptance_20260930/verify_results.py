"""Independent estimator checks and evidence identities, using summaries only."""
import ast
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import statsmodels.api as sm

HERE = Path(__file__).resolve().parent
tree = ast.parse((HERE/'attribution_remote.py').read_text())
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'regression')
scope = {'np': np}
exec(compile(ast.Module(body=[fn], type_ignores=[]), '<estimator>', 'exec'), scope)
rng = np.random.default_rng(20260930)
for n in (21, 43, 120):
    x = pd.DataFrame(rng.normal(0, .01, (n, 3)), columns=['market', 'size', '713'])
    y = .0002 + x @ np.array([.5, .4, .2]) + rng.normal(0, .004, n)
    actual, residual = scope['regression'](y, x)
    expected = sm.OLS(y, sm.add_constant(x)).fit(cov_type='HAC', cov_kwds={'maxlags': 3})
    np.testing.assert_allclose(list(actual['coefficients'].values()), expected.params, atol=1e-12)
    np.testing.assert_allclose(list(actual['ci95'].values()), expected.conf_int(), atol=1e-11)
    np.testing.assert_allclose(residual, expected.resid, atol=1e-12)
    assert abs(actual['r2'] - expected.rsquared) < 1e-12
p = json.loads((HERE/'attribution_results.json').read_text())
for cohort in p['nested_attribution'].values():
    parts = [cohort[k] for k in ('incremental_r2_v713_after_market_size',
             'incremental_r2_market_size_after_v713', 'shared_explained_variance_commonality',
             'unexplained_variance_fraction')]
    assert abs(sum(parts) - 1) < 1e-12
    for model in cohort['models'].values():
        c = model['mean_interval_return_components']
        assert abs(sum(v for k, v in c.items() if k != 'observed_mean') - c['observed_mean']) < 1e-12
    covariance = cohort['v20h_v713_covariance_decomposition']
    assert abs(covariance['market_size_shared'] + covariance['residual'] - covariance['total']) < 1e-12
history = json.loads((HERE/'history_audit_summary.json').read_text())
assert history['production_read_only']
for path in ('app/models/shadow.py', 'app/services/shadow_ledger.py'):
    assert hashlib.sha256((HERE.parents[1]/'v2.3/server'/path).read_bytes()).hexdigest() == history['tested_source_sha256'][path]
replay = json.loads((HERE/'two_phase_summary.json').read_text())
assert len(replay['results']) == 12
for r in replay['results']:
    assert r['minimum_cash'] >= 0
    assert r['completed_windows'] == 82 and r['unexecuted_windows'] == 0
    assert 0 <= r['modeled_notional_completion'] <= 1
    assert -1 <= r['max_drawdown'] <= 0
tests = {}
for filename in ('server_tests.xml', 'client_tests.xml', 'research_tests.xml'):
    suites = list(ET.parse(HERE/filename).getroot().iter('testsuite'))
    assert all(int(s.attrib['failures']) == int(s.attrib['errors']) == 0 for s in suites)
    tests[filename] = sum(int(s.attrib['tests']) for s in suites)
receipt = {'status': 'PASS', 'tests': tests, 'total_tests': sum(tests.values()),
           'estimator': 'OLS/HAC independently matches statsmodels at n=21/43/120',
           'checks': ['nested R2 partition', 'return component identity', 'covariance partition',
                      'remote tested source hashes match local', '12 replay scenarios with nonnegative cash']}
(HERE/'verification.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
print(json.dumps(receipt, ensure_ascii=False))
