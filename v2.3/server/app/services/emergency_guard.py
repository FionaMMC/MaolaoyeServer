from sqlalchemy import select

from app.exceptions import APIError, ErrorCode
from app.models import EmergencyExecution


def active_emergency(session, account_alias):
    return session.scalar(select(EmergencyExecution).where(
        EmergencyExecution.account_alias == account_alias,
        EmergencyExecution.status == "ACTIVE",
    ))


def assert_no_emergency(session, domain, account_alias):
    if domain == "live" and active_emergency(session, account_alias):
        raise APIError(ErrorCode.STRATEGY_PENDING,
                       "紧急执行期间普通下单暂停；完成对账并显式恢复后使用新计划", http_status=423)
