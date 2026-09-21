"""Choose the next executable monthly cycle using an exchange calendar."""
import argparse
import json
import os
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


def plan_cycle(today, open_dates, current_as_of=None):
    dates = sorted(set(open_dates))
    if today not in dates:
        return {"status":"CLOSED"}
    future = [d for d in dates if d > today]
    if not future:
        raise ValueError("calendar has no confirmed next trading session")
    next_session = future[0]
    # The next session's month determines which month has fully closed.
    prior = [d for d in dates if d[:6] < next_session[:6]]
    if not prior:
        raise ValueError("calendar has no completed prior month")
    as_of = prior[-1]
    if as_of > today:
        raise ValueError("month-end data is not yet observable")
    if current_as_of and current_as_of > as_of:
        raise ValueError("published target is ahead of the calendar")
    return {"status":"CURRENT" if current_as_of == as_of else "DUE",
            "market_date":today, "decision_date":next_session, "as_of_date":as_of}


def exchange_dates(today):
    day = datetime.strptime(today,"%Y%m%d")
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise ValueError("TUSHARE_TOKEN required for authoritative exchange calendar")
    payload = {
        "api_name":"trade_cal", "token":token,
        "params":{"exchange":"SSE", "start_date":(day-timedelta(days=65)).strftime("%Y%m%d"),
                  "end_date":(day+timedelta(days=40)).strftime("%Y%m%d"), "is_open":"1"},
        "fields":"cal_date"}
    request = urllib.request.Request("https://api.tushare.pro",
        data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    if body.get("code") != 0:
        raise ValueError("exchange calendar request failed")
    return [str(r[0]) for r in (body.get("data") or {}).get("items",[])]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today",required=True)
    parser.add_argument("--current-target",type=Path,required=True)
    args = parser.parse_args()
    current = None
    if args.current_target.is_file():
        frame = pd.read_parquet(args.current_target,columns=["as_of_date"])
        if frame.as_of_date.nunique() != 1:
            raise ValueError("invalid installed target month")
        current = str(frame.as_of_date.iloc[0])
    print(json.dumps(plan_cycle(args.today,exchange_dates(args.today),current)))
