"""Evaluate signed-off evidence summaries offline. Never grants order permission."""
import argparse
from datetime import datetime,timedelta
import json
from pathlib import Path


def evaluate(e):
    blocks=[]
    for key in ('monthly_inputs_verified','broker_history_reconciled','cash_ownership_reconciled',
                'target_frozen_and_reviewed','windows_native_acceptance','two_phase_native_acceptance',
                'no_conflicting_tasks','phase_order_ids_disjoint','late_fill_restart_test'):
        if e.get(key) is not True:
            blocks.append(key)
    if e.get('model_as_of')!='20260930':
        blocks.append('MODEL_AS_OF_NOT_20260930')
    if e.get('unresolved_submissions')!=0:
        blocks.append('UNRESOLVED_SUBMISSIONS')
    if e.get('qmt_vs_strategy_position_differences')!=0:
        blocks.append('POSITION_RECONCILIATION_REQUIRED')
    dates=[datetime.strptime(d,'%Y%m%d') for d in e.get('calendar',[])]
    if dates!=sorted(set(dates)) or datetime(2026,10,10) in dates:
        blocks.append('INVALID_OR_WEEKEND_CALENDAR')
    signal=datetime.strptime(e.get('signal_date','20260930'),'%Y%m%d')
    pairs=[(a,b) for a,b in zip(dates,dates[1:]) if a>signal and b-a==timedelta(days=1)]
    pair=[d.strftime('%Y%m%d') for d in pairs[0]] if pairs else None
    if pair!=['20261008','20261009']:
        blocks.append('OCTOBER_PAIR_NOT_VERIFIED')
    metrics=e.get('evaluated_plan',{})
    for field,cap in [('cagr_drag_pp',.5),('end_underweight',.02)]:
        val=metrics.get(field)
        if not isinstance(val,(int,float)) or not (-1e10<float(val)<1e10) or val>cap:
            blocks.append('TOLERANCE_'+field)
    return {'status':'REVIEW_REQUIRED' if blocks else 'EVIDENCE_CHECKS_PASSED',
            'blocking_items':blocks,'first_pair':pair,'does_not_authorize_or_submit_orders':True}


def main():
    p=argparse.ArgumentParser();p.add_argument('evidence',type=Path);args=p.parse_args()
    result=evaluate(json.loads(args.evidence.read_text(encoding='utf-8-sig')))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(2 if result['blocking_items'] else 0)


if __name__=='__main__':
    main()
