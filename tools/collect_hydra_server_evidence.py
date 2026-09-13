"""Read-only, narrow production evidence collection through configured SSH.

Writes only the requested local file. Never reads env files or mutates remote
code, tasks, account state or orders. Keep output private (not in public Git).
"""

import argparse
import json
from pathlib import Path
import subprocess

REMOTE = r"""
import sqlite3,json,subprocess,datetime,gzip
from pathlib import Path
c=sqlite3.connect('file:/opt/qmt-server/v2.3/server/pipeline-server.db?mode=ro',uri=True)
c.execute('PRAGMA query_only=ON')
c.row_factory=sqlite3.Row
c.execute('BEGIN')
queries={
 'state': "select instance_id,virtual_cash,virtual_positions,last_update,ledger_mode from instance_state where instance_id='live_hydra_v481_rb'",
 'rebalances': "select rebalance_id,baseline_cash,baseline_positions,target_shares,status from hydra_rebalances where execution_domain='live'",
 'v53_signals': "select signal_id,valid_date,precheck_status,precheck_reason,signal_time,symbol,direction,quantity from raw_signals where instance_id='paper_v53_v53' and valid_date between '20260831' and '20260903'",
 'v53_orders': "select distinct o.order_id,o.valid_date,o.status,o.created_at,o.fetched_at from orders o join order_signal_map m on m.order_id=o.order_id join raw_signals s on s.signal_id=m.signal_id where s.instance_id='paper_v53_v53' and o.valid_date between '20260831' and '20260903'",
 'live_orders': "select order_id,valid_date,status,symbol,direction,quantity from orders where execution_domain='live' and valid_date between '20260903' and '20260908'",
 'live_trade_count': "select count(*) n from trades where execution_domain='live'",
}
result={'collected_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'server_commit':subprocess.check_output(['git','-C','/opt/qmt-server','rev-parse','HEAD'],text=True).strip(),
 'service':subprocess.check_output(['systemctl','show','qmt-server','-p','ActiveState','-p','SubState','-p','NRestarts','-p','ActiveEnterTimestamp'],text=True).splitlines()}
for k,sql in queries.items():
 try: result[k]=[dict(r) for r in c.execute(sql)]
 except sqlite3.Error as e: result[k]={'error':str(e)}
c.rollback();c.close()
# Restrict to the V53 incident dates and relevant workflow messages. Stored
# locally only; do not print these log lines into the user-visible transcript.
logs=[]
for path in sorted(Path('/var/log/qmt-server').glob('server.log*')):
 opener=gzip.open if path.suffix=='.gz' else open
 with opener(path,'rt',encoding='utf-8',errors='replace') as f:
  for line in f:
   if ('2026-08-31' in line or '2026-09-01' in line) and any(t in line for t in ['pipeline_','V53','GET /orders','POST /trigger','expired']):
    logs.append({'file':path.name,'line':line.rstrip()})
result['incident_logs']=logs[-500:]
print(json.dumps(result,ensure_ascii=False))
"""


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    run = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "qmt", "python3 -"],
        input=REMOTE,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    data = json.loads(run.stdout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output.chmod(0o600)
    print(
        json.dumps(
            {
                "saved_private_evidence": str(args.output),
                "server_commit": data["server_commit"],
                "incident_log_count": len(data["incident_logs"]),
            }
        )
    )
