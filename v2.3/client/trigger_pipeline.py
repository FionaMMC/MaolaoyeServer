"""
模块六：触发服务器算下一交易日信号

调 POST /admin/run-pipeline 让服务器把 strategies.yaml 里所有策略实例
（real_A_buy_on_dip_example + paper_v20h_v20h_v1_3 等）跑一遍，
产出的 RawSignal 经过预检 + 归集，落到 orders 表，供 order_query 拉取。

用法:
    python trigger_pipeline.py                  # 自动找下一交易日
    python trigger_pipeline.py --date 20260507  # 强制指定日期
    python trigger_pipeline.py --today          # 用今天（仅供调试，正常生产用下一交易日）

依赖:
    PUSH_MODE=server，SERVER_BASE_URL + API_KEY 配好
    QMT 客户端运行（用 xtdata 查交易日历）
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

import requests
from xtquant import xtdata

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

xtdata.data_dir = config.QMT_USERDATA_DIR
log = config.setup_logger("trigger_pipeline")


def _wechat_alert(msg: str) -> None:  log.error(f"[微信报警（占位）] {msg}")
def _wechat_notify(msg: str) -> None: log.info(f"[微信通知（占位）] {msg}")


def startup_check() -> None:
    if config.PUSH_MODE != "server":
        log.error(f"PUSH_MODE={config.PUSH_MODE}，本脚本只在 server 模式下有意义；")
        log.error("    检查环境变量 QMT_PIPELINE_PUSH_MODE 或 v2.3/config.py")
        sys.exit(2)
    if not config.SERVER_BASE_URL:
        log.error("SERVER_BASE_URL 未配置（环境变量 QMT_PIPELINE_BASE_URL）")
        sys.exit(2)
    if not config.API_KEY:
        log.error("API_KEY 未配置（环境变量 QMT_PIPELINE_API_KEY）")
        sys.exit(2)
    if not os.path.exists(config.QMT_USERDATA_DIR):
        log.error(f"QMT 数据目录不存在: {config.QMT_USERDATA_DIR}")
        sys.exit(2)
    log.info(f"PUSH_MODE={config.PUSH_MODE} → {config.SERVER_BASE_URL}")


def _get_next_trading_day(today_str: str) -> str | None:
    """用 xtdata 查 SH 交易所日历，返回 today 之后的第一个交易日。"""
    start_str = (datetime.strptime(today_str, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
    end_str   = (datetime.strptime(today_str, "%Y%m%d") + timedelta(days=14)).strftime("%Y%m%d")
    days = xtdata.get_trading_calendar("SH", start_time=start_str, end_time=end_str)
    future = [d for d in days if d > today_str]
    return future[0] if future else None


def trigger(trade_date: str) -> dict | None:
    url = config.SERVER_BASE_URL.rstrip("/") + "/admin/run-pipeline"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="trade_date YYYYMMDD（默认下一交易日）")
    parser.add_argument("--today", action="store_true",
                        help="用今天作为 trade_date（仅调试，生产用默认下一交易日）")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("trigger_pipeline 启动")
    log.info("=" * 60)
    startup_check()

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

    data = trigger(trade_date)
    if data is None:
        _wechat_alert(f"trigger_pipeline 失败 trade_date={trade_date}")
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
        _wechat_notify(f"trigger_pipeline {trade_date}：信号 0，请检查上游 pred")
    else:
        _wechat_notify(
            f"trigger_pipeline {trade_date}：成功生成 {data['orders']} 单"
        )

    log.info("=== trigger_pipeline 完成 ===")


if __name__ == "__main__":
    main()
