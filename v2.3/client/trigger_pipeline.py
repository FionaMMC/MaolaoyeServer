"""
模块六：触发服务器算下一交易日信号

调 POST /admin/run-pipeline（模拟盘）或 /hydra/live/trigger（实盘）让服务器
把 strategies.yaml 里对应执行域的策略实例
（real_A_buy_on_dip_example + paper_v20h_v20h_v1_3 等）跑一遍，
产出的 RawSignal 经过预检 + 归集，落到 orders 表，供 order_query 拉取。

用法:
    python trigger_pipeline.py                  # 自动找下一交易日
    python trigger_pipeline.py --date 20260507  # 强制指定日期
    python trigger_pipeline.py --today          # 用今天（仅供调试，正常生产用下一交易日）
    python trigger_pipeline.py --live           # 实盘专用入口，固定写入 live 域

依赖:
    模拟盘：PUSH_MODE=server，SERVER_BASE_URL + API_KEY 配好
    实  盘：HYDRA_LIVE_SERVER_BASE_URL + HYDRA_LIVE_TRIGGER_API_KEY 配好
    QMT 客户端运行（用 xtdata 查交易日历）
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

import requests
from xtquant import xtdata

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

log = config.setup_logger("trigger_pipeline")


def _live_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def _connection(live: bool) -> tuple[str, str]:
    if live:
        return (
            _live_value("HYDRA_LIVE_SERVER_BASE_URL").rstrip("/"),
            _live_value("HYDRA_LIVE_TRIGGER_API_KEY"),
        )
    return config.SERVER_BASE_URL.rstrip("/"), config.API_KEY


def _userdata_dir(live: bool) -> str:
    return _live_value("HYDRA_LIVE_QMT_USERDATA_DIR") if live else config.QMT_USERDATA_DIR


def _wechat_alert(msg: str) -> None:
    log.error(f"[微信报警] {msg}")
    if not config.notify_wecom(msg, level="alert"):
        log.error(f"[微信报警] 推送失败：{msg}")

def _wechat_notify(msg: str) -> None:
    log.info(f"[微信通知] {msg}")
    if not config.notify_wecom(msg, level="info"):
        log.error(f"[微信通知] 推送失败：{msg}")


def startup_check(live: bool) -> None:
    base_url, api_key = _connection(live)
    userdata_dir = _userdata_dir(live)
    if live:
        if not base_url:
            log.error("HYDRA_LIVE_SERVER_BASE_URL 未配置")
            sys.exit(2)
        if not api_key:
            log.error("HYDRA_LIVE_TRIGGER_API_KEY 未配置")
            sys.exit(2)
        if not os.path.exists(userdata_dir):
            log.error(f"QMT 数据目录不存在: {userdata_dir}")
            sys.exit(2)
        xtdata.data_dir = userdata_dir
        log.info(f"LIVE trigger → {base_url}（仅可触发 live 域）")
        return
    if config.PUSH_MODE != "server":
        log.error(f"PUSH_MODE={config.PUSH_MODE}，本脚本只在 server 模式下有意义；")
        log.error("    检查环境变量 QMT_PIPELINE_PUSH_MODE 或 v2.3/config.py")
        sys.exit(2)
    if not base_url:
        log.error("SERVER_BASE_URL 未配置（环境变量 QMT_PIPELINE_BASE_URL）")
        sys.exit(2)
    if not api_key:
        log.error("API_KEY 未配置（环境变量 QMT_PIPELINE_API_KEY）")
        sys.exit(2)
    if not os.path.exists(userdata_dir):
        log.error(f"QMT 数据目录不存在: {userdata_dir}")
        sys.exit(2)
    xtdata.data_dir = userdata_dir
    log.info(f"PUSH_MODE={config.PUSH_MODE} → {base_url}")


def _get_next_trading_day(today_str: str) -> str | None:
    """用 xtdata 查 SH 交易所日历，返回 today 之后的第一个交易日。"""
    start_str = (datetime.strptime(today_str, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
    end_str   = (datetime.strptime(today_str, "%Y%m%d") + timedelta(days=14)).strftime("%Y%m%d")
    days = xtdata.get_trading_calendar("SH", start_time=start_str, end_time=end_str)
    future = [d for d in days if d > today_str]
    return future[0] if future else None


def trigger(trade_date: str, live: bool = False) -> dict | None:
    base_url, api_key = _connection(live)
    url = base_url + ("/hydra/live/trigger" if live else "/admin/run-pipeline")
    headers = {"Authorization": f"Bearer {api_key}"}
    log.info(f"POST {url}?trade_date={trade_date}")
    try:
        r = requests.post(url, params={"trade_date": trade_date}, headers=headers, timeout=180)
    except Exception as e:
        log.error(f"请求异常: {e}")
        return None

    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
    if r.status_code != 200 or not body or body.get("code") != 0:
        log.error(f"触发失败 status={r.status_code} body={body}")
        return None
    return body["data"]


def trigger_recovery(trade_date, account_group, *, manual=True, wait_seconds=120):
    """POST has a server-derived stable identity; transport retries cannot duplicate it.

    Poll only the job-status endpoint. Never GET /orders for a notification.
    """
    base, key = _connection(False)
    url = base + "/admin/pipeline-jobs"
    headers = {"Authorization": f"Bearer {key}"}
    payload = {"trade_date": int(trade_date), "account_group": account_group,
               "source": "manual" if manual else "automatic"}
    job = None
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            if response.status_code in (429, 502, 503, 504):
                response.raise_for_status()
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise ValueError(f"recovery rejected: {body.get('message')}")
            job = body["data"]
            break
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in (429, 502, 503, 504):
                raise
        except (requests.Timeout, requests.ConnectionError):
            pass
        if attempt < 2:
            time.sleep(attempt + 1)
    if job is None:
        raise RuntimeError("补跑请求结果未知；用相同账户组/日期重试可取回同一任务，不要 force")
    log.info("恢复任务 job_id=%s status=%s", job["job_id"], job["status"])
    deadline = time.monotonic() + wait_seconds
    while job["status"] in {"QUEUED", "RUNNING", "WAITING_INPUT", "RETRY_WAIT"}:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(5, remaining))
        try:
            response = requests.get(url + "/" + job["job_id"], headers=headers, timeout=15)
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise ValueError(body.get("message"))
            job = body["data"]
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError):
            # The accepted durable task is not cancelled when polling fails.
            log.warning("查询中断；任务 %s 仍可继续查询", job["job_id"])
            break
    return job


def report_recovery(job):
    status, identity = job["status"], job["job_id"]
    result = job.get("result") or {}
    if status in {"READY", "REUSED"}:
        counts = job.get("current_order_status_counts", result.get("order_status_counts", {}))
        if job.get("current_order_ids_match") is False or set(counts) - {"PENDING", "FILLED"}:
            _wechat_alert(f"任务 {identity} 已有订单状态变化 {counts}；请核对回报，不重建整批。")
            return 1
        _wechat_notify(f"调仓恢复 {status}：有效待领取 {counts.get('PENDING', 0)} 笔，"
                       f"已成交 {counts.get('FILLED', 0)} 笔；沿用原订单编号。任务 {identity}")
        return 0
    if status == "NO_ORDERS":
        _wechat_notify(f"调仓恢复已运行，本次策略未产生订单；不等于旧月调仓已恢复。任务 {identity}")
        return 0
    if status in {"QUEUED", "RUNNING", "WAITING_INPUT", "RETRY_WAIT"}:
        _wechat_notify(f"调仓恢复已受理，尚未完成：{status}。任务 {identity}；不要强制重建订单。")
        return 3  # accepted/pending, not a completed publication
    _wechat_alert(f"调仓恢复需处理：{status}，任务 {identity}，详情 {result}")
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="trade_date YYYYMMDD（默认下一交易日）")
    parser.add_argument("--today", action="store_true",
                        help="用今天作为 trade_date（仅调试，生产用默认下一交易日）")
    parser.add_argument("--live", action="store_true",
                        help="调用实盘专用 trigger；订单只写入 live 域")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--manual", action="store_true", help="模拟盘人工补跑：持久任务、高优先级、复用原单")
    mode.add_argument("--queued", action="store_true", help="模拟盘普通优先级队列任务（需独立 worker）")
    parser.add_argument("--account-group", help="补跑账户组，例如 paper_v53")
    parser.add_argument("--wait-seconds", type=int, default=120, help="只影响本地等回执时间，不取消任务")
    args = parser.parse_args()
    if args.manual or args.queued:
        if args.live or not args.account_group or not 0 <= args.wait_seconds <= 600:
            parser.error("补跑只支持 paper，需 --account-group，wait-seconds 范围 0–600")

    log.info("=" * 60)
    log.info("trigger_pipeline 启动")
    log.info("=" * 60)
    startup_check(args.live)

    today = datetime.now().strftime("%Y%m%d")
    if args.date:
        trade_date = args.date
        log.info(f"使用 --date 指定的日期: {trade_date}")
    elif args.today:
        trade_date = today
        log.info(f"使用 --today 模式: {trade_date}")
    else:
        trade_date = _get_next_trading_day(today)
        if not trade_date:
            _wechat_alert("trigger_pipeline：14 天内未找到下一个交易日")
            sys.exit(1)
        log.info(f"今天 {today} → 下一交易日 {trade_date}")

    if args.manual or args.queued:
        try:
            job = trigger_recovery(trade_date, args.account_group,
                                   manual=args.manual, wait_seconds=args.wait_seconds)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            _wechat_alert(f"调仓恢复请求未确认：{exc}")
            sys.exit(1)
        sys.exit(report_recovery(job))

    data = trigger(trade_date, live=args.live)
    if data is None:
        _wechat_alert(f"trigger_pipeline 失败 trade_date={trade_date}")
        sys.exit(1)

    if data.get("skipped"):
        reason = data["skipped"]
        if reason in {"already_fetched", "already_settled", "recovery_batch_published"}:
            _wechat_notify(f"trigger_pipeline {trade_date}：保留已有批次（{reason}），"
                           "没有重算；仍有效的原单可由 order_query 再次下载。不要 force。")
            return
        _wechat_alert(f"trigger_pipeline {trade_date} 未生成新批次：{reason}；"
                      f"详情 {data}。可用 --manual --account-group 指定组补跑。")
        sys.exit(1)
    if data.get("waiting_input") or data.get("strategy_errors"):
        _wechat_alert(f"trigger_pipeline {trade_date} 未完全完成：{data}")
        sys.exit(1)

    log.info(
        f"✅ 触发成功 trade_date={data['trade_date']} "
        f"instances={data['instances']} signals={data['signals']} "
        f"passed={data['passed']} orders={data['orders']}"
    )

    if data["orders"] == 0:
        log.warning(
            f"orders=0 — 可能原因：① pred 未覆盖该日期；② 策略 guard 不满足；"
            f"③ V20H 数据未上传到云端"
        )
        log.warning(
            "排查命令: curl -H 'Authorization: Bearer ...' "
            f"{config.SERVER_BASE_URL}/admin/data-status?strategy=v20h_v1_3"
        )
        _wechat_notify(f"trigger_pipeline {trade_date}：本次策略未产生订单；不等于调仓失败或旧月恢复完成")
    else:
        # 拉取订单明细按账户/方向汇总
        # 实盘 trigger token 无订单读取权限；18:00 仍由 order_query 用实盘执行 token 拉取。
        breakdown = data.get("order_breakdown", {})
        if breakdown:
            parts = []
            for acct in sorted(breakdown.keys()):
                b = breakdown[acct]
                parts.append(f"{acct} 买入{b['BUY']}笔，卖出{b['SELL']}笔")
            msg = f"trigger_pipeline {trade_date}：成功生成 {data['orders']} 单，其中 {'；'.join(parts)}"
        else:
            msg = f"trigger_pipeline {trade_date}：成功生成 {data['orders']} 单"
        _wechat_notify(msg)

    log.info("=== trigger_pipeline 完成 ===")


if __name__ == "__main__":
    main()
