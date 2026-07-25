"""APScheduler 包装：单例 BackgroundScheduler + 注册 cron 任务。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def make_scheduler(
    pipeline_run: Callable[[int], dict],
    shadow_run: Callable[[int], dict] | None = None,
    cron_hour: int = 16,
    cron_minute: int = 0,
) -> BackgroundScheduler:
    """构造 BackgroundScheduler，注册每个交易日 16:00 跑 pipeline。

    pipeline_run: 接收 trade_date int，返回摘要 dict。
    """
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    def _job():
        today = int(datetime.now().strftime("%Y%m%d"))
        try:
            summary = pipeline_run(today)
            logger.info("scheduler_pipeline_done %s", summary)
        except Exception as e:
            logger.exception("scheduler_pipeline_error: %s", e)
        if shadow_run is not None:
            try:
                shadow_summary = shadow_run(today)
                logger.info("scheduler_shadow_done %s", shadow_summary)
            except Exception as e:
                logger.exception("scheduler_shadow_error: %s", e)

    scheduler.add_job(
        _job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=cron_hour, minute=cron_minute,
        ),
        id="strategy_pipeline_daily",
        replace_existing=True,
    )
    return scheduler
