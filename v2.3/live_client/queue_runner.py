"""Bounded offline resumption of known-unsent orders, never blanket retries."""

from datetime import datetime, timedelta, timezone
import time

CHINA = timezone(timedelta(hours=8))


def run_queue_passes(
    submit_pass, trade_date, *, now=None, wait=None, interval_seconds=30
):
    """Release the account lock between passes; exceptions stop this runner.

    14:55 precedes the existing cancel-open phase. 15:00 remains the policy
    expiry, not permission to create new orders after cancellation started.
    """
    if not 1 <= interval_seconds <= 60:
        raise ValueError("queue interval must be between 1 and 60 seconds")
    now = now or (lambda: datetime.now(CHINA))
    wait = wait or time.sleep
    start = datetime.strptime(trade_date, "%Y%m%d").replace(
        hour=9, minute=10, tzinfo=CHINA
    )
    deadline = start.replace(hour=14, minute=55)
    passes = 0
    last = None
    while True:
        current = now().astimezone(CHINA)
        if current < start:
            return {
                "status": "WAITING_EXECUTION_WINDOW",
                "trade_date": trade_date,
                "next_check_at": start.isoformat(),
            }
        if current >= deadline:
            return {
                "status": "QUEUE_WINDOW_ENDED",
                "trade_date": trade_date,
                "passes": passes,
                "last_pass": last,
            }
        last = submit_pass()
        passes += 1
        if last.get("status") != "WAITING_FOR_CASH":
            return {
                "status": last.get("status", "SUBMIT_QUEUE_COMPLETE"),
                "passes": passes,
                "last_pass": last,
            }
        # Never catch submission exceptions: a failed/ambiguous broker call is
        # not a reason to rerun a trading command automatically.
        remaining = (deadline - now().astimezone(CHINA)).total_seconds()
        if remaining > 0:
            wait(min(interval_seconds, remaining))
