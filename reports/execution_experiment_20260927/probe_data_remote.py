"""Fetch public market data with the server's existing provider entitlement.
Never emit the credential or account data.
"""
import json
from pathlib import Path
from urllib.request import Request, urlopen

token = None
for line in Path('/opt/qmt-refresh/.env').read_text().splitlines():
    if line.strip().startswith('TUSHARE_TOKEN='):
        token = line.split('=', 1)[1].strip().strip('"').strip("'")
assert token, 'provider credential unavailable'
out = {}
for name, params in [
    ('etf_mins', {'ts_code': '510300.SH', 'freq': '5min', 'start_date': '2026-09-01 09:00:00', 'end_date': '2026-09-01 15:30:00'}),
    ('stk_mins', {'ts_code': '000001.SZ', 'freq': '5min', 'start_date': '2026-09-01 09:00:00', 'end_date': '2026-09-01 15:30:00'}),
    ('fund_div_510300', {'ts_code': '510300.SH'}),
    ('fund_div_511260', {'ts_code': '511260.SH'}),
]:
    api = 'fund_div' if name.startswith('fund_div') else name
    payload = {'api_name': api, 'token': token, 'params': params, 'fields': ''}
    request = Request('https://api.tushare.pro', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        data = json.loads(urlopen(request, timeout=15).read())
        out[name] = data
    except Exception as e:
        out[name] = {'error': type(e).__name__}
print(json.dumps(out, ensure_ascii=False).replace(token, '[redacted]'))
