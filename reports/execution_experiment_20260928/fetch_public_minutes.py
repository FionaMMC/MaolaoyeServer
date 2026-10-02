"""Public, unauthenticated ETF minute endpoints; no account data or credentials."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import hashlib
import json

HERE = Path(__file__).resolve().parent
OUT = HERE/'public_minutes'
SYMBOLS = ['518880','513100','513500']


def fetch(job):
    provider,symbol,period,end = job
    if provider == 'eastmoney':
        endpoint = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
        params = dict(secid='1.'+symbol,fields1='f1,f2,f3,f4,f5,f6',fields2='f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',klt=period,fqt=0,beg=0,end=end,lmt=10000)
    elif provider == 'eastmoney_1m':
        endpoint = 'https://push2his.eastmoney.com/api/qt/stock/trends2/get'
        params = dict(secid='1.'+symbol,fields1='f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13',fields2='f51,f52,f53,f54,f55,f56,f57,f58',ndays=5,iscr=0)
    else:
        endpoint = 'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData'
        params = dict(symbol='sh'+symbol,scale=period,ma='no',datalen=1970)
    url = endpoint+'?'+urlencode(params)
    record = dict(provider=provider,symbol=symbol+'.SH',period=period,end=end,url=url,fetched_at=datetime.now(timezone.utc).isoformat())
    try:
        raw = urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://finance.sina.com.cn/' if provider=='sina' else 'https://quote.eastmoney.com/'}),timeout=20).read()
        text = raw.decode('utf-8')
        if provider == 'sina':
            payload = json.loads(text.split('=(',1)[1].rsplit(');',1)[0])
            rows = payload or []
            first,last = (rows[0].get('day'),rows[-1].get('day')) if rows else (None,None)
        else:
            payload = json.loads(text)
            rows = (payload.get('data') or {}).get('trends' if provider=='eastmoney_1m' else 'klines',[]) or []
            first,last = (rows[0].split(',')[0],rows[-1].split(',')[0]) if rows else (None,None)
        file = OUT/f'{provider}_{symbol}_{period}_{end}.json'
        file.write_text(json.dumps(payload,ensure_ascii=False))
        record.update(rows=len(rows),first=first,last=last,file=file.name,sha256=hashlib.sha256(file.read_bytes()).hexdigest())
    except Exception as e:
        record.update(error=type(e).__name__,message=str(e)[:180])
    print(json.dumps(record,ensure_ascii=False),flush=True)
    return record


if __name__ == '__main__':
    OUT.mkdir(exist_ok=True)
    jobs = [('eastmoney',s,5,20500101) for s in SYMBOLS]
    jobs += [('sina',s,5,20500101) for s in SYMBOLS]
    jobs += [('eastmoney_1m',s,1,20500101) for s in SYMBOLS]
    jobs += [('eastmoney','513100',5,20260813)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(fetch,jobs))
    (HERE/'public_minute_inventory.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
