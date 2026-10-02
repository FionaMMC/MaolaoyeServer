"""Run in an isolated remote app copy. Production DB/market files are read-only.

Reports aggregate execution-price differences and one-step historical shadow
replays. No account-level records or raw market data leave the remote host.
"""
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import tempfile

import numpy as np
import pandas as pd

from app.db import init_db, make_engine, make_session_factory
from app.models import Order, Trade, ShadowFill, ShadowInstanceState, ShadowTarget
from app.services.shadow_ledger import ShadowLedgerService, TARGET_COLUMNS
from app.storage.parquet import ParquetStore

ROOT = Path('/opt/qmt-server/v2.3/server')
db = sqlite3.connect(f'file:{ROOT}/pipeline-server.db?mode=ro', uri=True)
db.row_factory = sqlite3.Row
db.execute('PRAGMA query_only=ON')
db.execute('BEGIN')
store = ParquetStore(ROOT/'data')
cache = {}


def bar(code, date):
    if code not in cache:
        for category in ('stocks', 'etfs'):
            f = store.read(category, code)
            if not f.empty:
                cache[code] = f.set_index('trade_date')
                break
        else:
            cache[code] = pd.DataFrame()
    f = cache[code]
    return f.loc[int(date)] if int(date) in f.index else None


def compact_metrics(rows):
    if not rows:
        return {'n': 0}
    a = np.array(rows, dtype=float)
    return {'n': len(a), 'mean': float(a.mean()),
            'median': float(np.median(a)), 'max_abs': float(np.abs(a).max())}


result = {'collected_at': datetime.now(timezone.utc).isoformat(),
          'production_read_only': True, 'live_prices': {}, 'shadow_replay': {}}
# execution_quality stores one cumulative observation per order; do not sum
# partial/cumulative Trade callbacks, which could double-count quantities.
rows = db.execute('''SELECT q.order_id,q.execution_domain,q.direction,
    q.symbol,q.filled_quantity,q.fill_vwap,o.valid_date
    FROM execution_quality q JOIN orders o ON o.order_id=q.order_id
    WHERE q.filled_quantity>0 AND q.execution_domain='live' ''').fetchall()
differences, signed_differences, opening_differences = [], [], []
missing, invalid = 0, 0
equal_close = equal_open = 0
weighted_diff = weighted_close = 0.0
for r in rows:
    b = bar(r['symbol'], r['valid_date'])
    if b is None:
        missing += 1
        continue
    fill = float(r['fill_vwap'] or 0)
    closing, opening = float(b['close']), float(b['open'])
    if not all(math.isfinite(v) and v > 0 for v in (fill, closing, opening)):
        invalid += 1
        continue
    sign = 1 if r['direction'] == 'BUY' else -1
    differences.append((fill / closing - 1) * 10000)
    signed_differences.append(sign * (fill / closing - 1) * 10000)
    opening_differences.append((fill / opening - 1) * 10000)
    equal_close += abs(fill - closing) <= 0.00050001
    equal_open += abs(fill - opening) <= 0.00050001
    weighted_diff += sign * (fill - closing) * r['filled_quantity']
    weighted_close += closing * r['filled_quantity']
result['live_prices'] = {
    'filled_order_observations': len(rows), 'missing_same_date_bar': missing,
    'invalid_price_rows': invalid, 'close_equal_within_half_etf_tick': equal_close,
    'open_equal_within_half_etf_tick': equal_open,
    'fill_vs_same_day_close_bps': compact_metrics(differences),
    'signed_fill_vs_close_bps': compact_metrics(signed_differences),
    'fill_vs_same_day_open_bps': compact_metrics(opening_differences),
    'weighted_signed_fill_vs_close_bps': weighted_diff / weighted_close * 10000
        if weighted_close else None,
    'date_basis': 'order valid_date, not independently certified broker fill timestamp',
    'meaning': 'price reconciliation bridge; equality is not expected for intraday fills',
}

template = ShadowLedgerService(None, store, ROOT/'strategies.yaml')
configs = template.load_instances()
with tempfile.TemporaryDirectory(prefix='shadow-replay-', dir=Path.cwd()) as tmp:
    for cfg in configs:
        sid = cfg['shadow_id']
        snapshots = [dict(r) for r in db.execute(
            'SELECT * FROM shadow_nav_snapshots WHERE shadow_id=? ORDER BY date', (sid,))]
        target_rows = [dict(r) for r in db.execute(
            'SELECT * FROM shadow_targets WHERE shadow_id=?', (sid,))]
        targets = {}
        for r in target_rows:
            targets.setdefault(r['target_hash'], []).append(r)
        counts = Counter()
        reasons = Counter()
        nav_diffs, cost_diffs = [], []
        cash_components, position_components, valuation_components = [], [], []
        mismatch_classes = Counter()
        cfg = dict(cfg, require_sidecar=False, target_file=Path(tmp)/f'{sid}.parquet')
        for prev, current in zip(snapshots, snapshots[1:]):
            counts['candidate_transitions'] += 1
            current_target = targets.get(current['target_hash'])
            previous_target = targets.get(prev['target_hash'])
            if not current_target or not previous_target:
                counts['missing_archived_target'] += 1
                continue
            engine = make_engine('sqlite:///:memory:')
            init_db(engine)
            sf = make_session_factory(engine)
            service = ShadowLedgerService(sf, store, ROOT/'strategies.yaml')
            with sf() as session:
                session.add(ShadowInstanceState(
                    shadow_id=sid, initial_cash=cfg['initial_cash'],
                    virtual_cash=prev['virtual_cash'],
                    virtual_positions=json.loads(prev['positions_snapshot']),
                    status='active', target_hash=prev['target_hash'],
                    decision_date=prev['decision_date'], as_of_date=prev['as_of_date'],
                    source_version=prev['source_version'], input_hash=prev['input_hash'],
                    cumulative_cost=0, last_turnover=0, last_update='historical-seed',
                ))
                for row in previous_target:
                    session.add(ShadowTarget(**row))
                session.commit()
            pd.DataFrame([{k: r[k] for k in TARGET_COLUMNS} for r in current_target]).to_parquet(
                cfg['target_file'], index=False)
            try:
                out = service._run_one(cfg, int(current['date']))
                with sf() as session:
                    state = session.get(ShadowInstanceState, sid)
                    fills = session.query(ShadowFill).all()
                    assert state.virtual_cash >= -1e-6
                    assert session.query(Order).count() == session.query(Trade).count() == 0
                    signed_cash = sum((1 if f.direction == 'SELL' else -1)
                                      * f.quantity * f.price - f.fee for f in fills)
                    assert abs(prev['virtual_cash'] + signed_cash - state.virtual_cash) < 1e-4
                    assert abs(sum(f.fee for f in fills) - out['transaction_cost']) < 1e-4
                    for f in fills:
                        b = bar(f.code, f.trade_date)
                        assert b is not None and abs(f.price - float(b['close'])) < 1e-10
                        assert f.price_basis == 'OFFICIAL_DAILY_CLOSE'
                    counts['verified_close_fills'] += len(fills)
                    counts['replayed_transitions'] += 1
                    counts['rebalances'] += bool(fills)
                    nav_diffs.append((out['nav'] / current['nav'] - 1) * 10000)
                    cost_diffs.append((out['transaction_cost'] - current['transaction_cost'])
                                      / current['nav'] * 10000)
                    counts['historical_holdings_equal'] += (
                        state.virtual_positions == json.loads(current['positions_snapshot']))
                    recorded_positions = json.loads(current['positions_snapshot'])
                    marks = service._prices(set(recorded_positions) | set(state.virtual_positions),
                                            int(current['date']), cfg['max_price_staleness_days'])
                    denominator = current['nav'] / 10000
                    cash_component = (state.virtual_cash - current['virtual_cash']) / denominator
                    position_component = sum((state.virtual_positions.get(s, 0)
                                              - recorded_positions.get(s, 0)) * marks[s]
                                             for s in marks) / denominator
                    valuation_component = (current['virtual_cash']
                        + sum(q * marks[s] for s, q in recorded_positions.items())
                        - current['nav']) / denominator
                    assert abs(cash_component + position_component + valuation_component
                               - nav_diffs[-1]) < 1e-6
                    cash_components.append(cash_component)
                    position_components.append(position_component)
                    valuation_components.append(valuation_component)
                    if abs(nav_diffs[-1]) > 1e-6:
                        mismatch_classes['recorded_nav_vs_current_marks' if abs(valuation_component) > 1e-6
                                         else 'cash_or_position_transition'] += 1
                    if recorded_positions != state.virtual_positions:
                        mismatch_classes['holdings_changed_without_economic_target_change' if
                            not fills else 'different_rebalance_outcome'] += 1
            except ValueError as exc:
                counts['blocked_by_new_validation'] += 1
                # Aggregate reason classes only, never symbol/account rows.
                reasons[str(exc).split(' for ')[0].split(':')[0]] += 1
            finally:
                engine.dispose()
        result['shadow_replay'][sid] = {
            'snapshot_count': len(snapshots),
            'start': snapshots[0]['date'] if snapshots else None,
            'end': snapshots[-1]['date'] if snapshots else None,
            'counts': dict(counts), 'blocked_reason_counts': dict(reasons),
            'nav_difference_vs_recorded_bps': compact_metrics(nav_diffs),
            'fee_difference_vs_recorded_nav_bps': compact_metrics(cost_diffs),
            'difference_decomposition_bps': {
                'cash': compact_metrics(cash_components),
                'holdings_at_common_marks': compact_metrics(position_components),
                'recorded_nav_vs_current_marks': compact_metrics(valuation_components),
            },
            'difference_classes': dict(mismatch_classes),
        }
db.rollback()
db.close()
result['limitations'] = [
    'One-step historical transition replay seeded from prior snapshots, not continuous reconstruction.',
    'Uses presently configured costs/cadence; historic config archives are not certified.',
    'Producer sidecar certification is excluded; saved target contents are the replay evidence.',
    'Shadow close fills are theoretical; auction accessibility and market impact are not modeled.',
    'Corporate-action accounting is not newly reconstructed by this shadow service.',
]
print(json.dumps(result, ensure_ascii=False, allow_nan=False))
