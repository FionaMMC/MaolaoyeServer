"""Collect fresh QMT facts locally. Does not submit, cancel, settle or upload.
Private config fields: userdata_dir, account_id, expected_account_sha256, session_id.
Historical orders may be absent in QMT queries; absence never proves no trade.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path

FIELDS={
 'orders':('account_id','stock_code','order_id','order_sysid','order_time','order_type','order_volume','price','traded_volume','traded_price','order_status','status_msg','strategy_name','order_remark'),
 'trades':('account_id','stock_code','order_id','order_sysid','traded_id','traded_time','traded_volume','traded_price','traded_amount','strategy_name','order_remark'),
 'positions':('account_id','stock_code','volume','can_use_volume','market_value'),
}


def collect(trader,account,expected_id):
    start=datetime.now(timezone.utc).isoformat()
    asset=trader.query_stock_asset(account)
    if asset is None or str(getattr(asset,'account_id',''))!=expected_id:
        raise ValueError('asset response missing or account identity mismatch')
    cash=float(asset.cash)
    total=float(asset.total_asset)
    if not math.isfinite(cash) or not math.isfinite(total) or cash<0 or total<=0:
        raise ValueError('invalid asset numbers')
    payload={'started_at':start,'available_cash':cash,'total_asset':total}
    calls={'orders':trader.query_stock_orders,'trades':trader.query_stock_trades,'positions':trader.query_stock_positions}
    for kind,query in calls.items():
        rows=query(account)
        if rows is None:
            raise ValueError(kind+' query unavailable; not an empty result')
        packed=[]
        for row in rows:
            if str(getattr(row,'account_id',''))!=expected_id:
                raise ValueError(kind+' account identity mismatch')
            packed.append({f:getattr(row,f,None) for f in FIELDS[kind]})
        payload[kind]=packed
    payload.update(completed_at=datetime.now(timezone.utc).isoformat(),
        account_sha256=hashlib.sha256(expected_id.encode()).hexdigest(),
        historical_completeness='UNPROVEN; obtain historical broker statement and local state separately',
        mutations=0)
    return payload


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    cfg=json.loads(args.config.read_text(encoding='utf-8-sig'))
    account_id=str(cfg['account_id'])
    if hashlib.sha256(account_id.encode()).hexdigest()!=cfg['expected_account_sha256']:
        raise ValueError('configured account SHA mismatch')
    if not Path(cfg['userdata_dir']).is_dir() or int(cfg['session_id'])<=0:
        raise ValueError('userdata/session invalid')
    # Reserve a fresh file before connecting. Failed collection leaves explicit
    # ERROR evidence, never a stale successful snapshot or overwritten evidence.
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as output:
        trader=None
        try:
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
            trader=XtQuantTrader(cfg['userdata_dir'],int(cfg['session_id']))
            trader.start()
            if trader.connect()!=0:
                raise RuntimeError('QMT connection failed')
            account=StockAccount(account_id)
            if trader.subscribe(account)!=0:
                raise RuntimeError('QMT account subscription failed')
            payload=collect(trader,account,account_id)
            output.write(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False))
        except Exception as exc:
            output.seek(0);output.truncate()
            output.write(json.dumps({'status':'ERROR','type':type(exc).__name__,'mutations':0}))
            raise
        finally:
            if trader is not None:
                trader.stop()
    digest=hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(json.dumps({'status':'READ_ONLY_CAPTURED','sha256':digest,'account_verified':True,
        'order_rows':len(payload['orders']),'trade_rows':len(payload['trades']),
        'position_rows':len(payload['positions']),'mutations':0,
        'historical_completeness':'UNPROVEN','not_a_reconciliation_or_trading_approval':True}))


if __name__=='__main__':
    main()
