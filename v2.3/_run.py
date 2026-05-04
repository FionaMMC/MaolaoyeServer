"""Wrapper to run data_collector with correct Python path (xtquant + akshare).

Usage:
  python _run.py --full --date 20260430
  python _run.py --type market_ohlcv --date 20260430
  python _run.py --stats
"""
import sys
# Add QMT xtquant site-packages AFTER standard paths to avoid pandas conflict
_qmt_sp = r"C:\parttime\平安证券量盈QMT策略交易平台\bin.x64\Lib\site-packages"
if _qmt_sp not in sys.path:
    sys.path.append(_qmt_sp)

import data_collector
data_collector.main()
