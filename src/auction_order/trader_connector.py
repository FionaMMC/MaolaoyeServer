"""XtQuantTrader 连接封装。失败立即抛异常，调用方负责发报警。"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def connect_trader(data_dir: str, account_id: str) -> tuple[Any, Any]:
    if not account_id:
        raise ValueError("account_id 不能为空")

    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount

    session = int(time.time())
    trader = XtQuantTrader(data_dir, session)
    trader.start()

    ret = trader.connect()
    if ret != 0:
        raise RuntimeError(f"XtQuantTrader.connect() 返回 {ret}（非 0 表示连接失败）")
    logger.info("XtQuantTrader connected, session=%d", session)

    account = StockAccount(account_id, "STOCK")
    sub_ret = trader.subscribe(account)
    if sub_ret != 0:
        raise RuntimeError(f"XtQuantTrader.subscribe({account_id}) 返回 {sub_ret}")
    logger.info("XtQuantTrader subscribed account=%s", account_id)

    return trader, account
