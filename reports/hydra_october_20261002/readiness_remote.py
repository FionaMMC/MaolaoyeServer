"""Read-only October readiness summary; no raw account rows leave qmt."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import subprocess


ROOT=Path('/opt/qmt-server/v2.3/server')
c=sqlite3.connect(f'file:{ROOT}/pipeline-server.db?mode=ro',uri=True)
c.row_factory=sqlite3.Row
c.execute('PRAGMA query_only=ON')
out=dict(observed_at=datetime.now(timezone.utc).isoformat(),database_mode='ro/query_only',
         order_writes=0,states=[],plans=[],cycles=[],targets=[],publications=[],order_summary=[])
states=c.execute("select * from instance_state where execution_domain='live' and instance_id like '%hydra%'").fetchall()
accounts=[]
for row in states:
    positions=json.loads(row['virtual_positions'] or '{}')
    strategy=json.loads(row['strategy_state'] or '{}')
    accounts.append(row['account_alias'])
    out['states'].append(dict(instance_id=row['instance_id'],account_alias=row['account_alias'],
        strategy_cash=row['virtual_cash'],held_symbol_count=sum(q>0 for q in positions.values()),
        last_update=row['last_update'],ledger_mode=row['ledger_mode'],strategy_state_keys=sorted(strategy)))
for account in set(accounts):
    for row in c.execute('select * from hydra_execution_plans where execution_domain=? and account_alias=? order by created_at desc limit 5',('live',account)):
        p=json.loads(row['request_payload'])
        out['plans'].append(dict(plan_id=row['plan_id'],account_alias=account,status=row['status'],created_at=row['created_at'],
            as_of_date=p.get('as_of_date'),execution_date=p.get('execution_date'),strategy_version=p.get('strategy_version'),
            cash_buffer_weight=p.get('cash_buffer_weight'),positive_weight_count=sum(x['weight']>0 for x in p.get('weights',[])),
            weight_sum=sum(x['weight'] for x in p.get('weights',[]))))
    for row in c.execute('select * from hydra_monthly_cycles where execution_domain=? and account_alias=? order by as_of_date desc limit 5',('live',account)):
        r=json.loads(row['result'])
        out['cycles'].append(dict(cycle_id=row['cycle_id'],as_of_date=row['as_of_date'],status=row['status'],
            result_status=r.get('status'),result_keys=sorted(r),error=str(r.get('error',''))[:600]))
    for row in c.execute('select * from hydra_targets where execution_domain=? and account_alias=? order by created_at desc limit 5',('live',account)):
        out['targets'].append({k:row[k] for k in ('target_id','as_of_date','decision_date','execution_date','status','cash_buffer_weight','strategy_version')})
    for row in c.execute('select reference_date,observed_at from hydra_execution_publications where account_alias=? order by reference_date desc limit 5',(account,)):
        out['publications'].append(dict(row))
    for row in c.execute("select valid_date,direction,status,count(*) as orders from orders where execution_domain='live' and qmt_account_alias=? group by valid_date,direction,status order by valid_date desc limit 24",(account,)):
        out['order_summary'].append(dict(row))
out['monthly_input_count']=c.execute("select count(*) from hydra_monthly_cycles where execution_domain='live' and as_of_date='20260930'").fetchone()[0]
out['emergency_statuses']=[dict(r) for r in c.execute('select status,count(*) as count from emergency_executions group by status')]
out['latest_manifest_dates']={}
for path in (ROOT/'data/hydra/batches').rglob('manifest.json'):
    manifest=json.loads(path.read_text())
    stream=manifest.get('stream','unknown')
    day=manifest.get('as_of_date','')
    if day>out['latest_manifest_dates'].get(stream,''):
        out['latest_manifest_dates'][stream]=day
allowed={'QMT_HYDRA_MONTHLY_ENABLED','QMT_HYDRA_MONTHLY_START_DATE','QMT_HYDRA_LIVE_RISK_MODE','QMT_HYDRA_ETF_EXECUTION_POLICY','QMT_SCHEDULER_ENABLED'}
out['settings']={}
for line in (ROOT/'.env').read_text().splitlines():
    if '=' in line:
        key,value=line.split('=',1)
        if key.strip() in allowed:
            out['settings'][key.strip()]=value.strip().strip('"\'')
out['services']={}
for name in ('qmt-server.service','qmt-hydra-monthly.service','qmt-hydra-monthly.timer'):
    r=subprocess.run(['systemctl','show',name,'--property=ActiveState,SubState,Result,ExecMainStatus,NextElapseUSecRealtime'],text=True,capture_output=True)
    out['services'][name]=r.stdout.strip()
out['source_hashes']={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
    ('app/services/hydra_monthly.py','app/services/hydra_relay.py','app/services/hydra_execution_policy.py')}
print(json.dumps(out,ensure_ascii=False,indent=2))
