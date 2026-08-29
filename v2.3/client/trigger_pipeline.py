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
from collections import defaultdict
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


def _fetch_order_breakdown(trade_date: str) -> dict[str, dict[str, int]]:
    """调 GET /orders 并按 (account_group, direction) 汇总。"""
    url = config.SERVER_BASE_URL.rstrip("/") + "/orders"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    try:
        resp = requests.get(url, params={"date": trade_date}, headers=headers, timeout=30)
        body = resp.json()
        if body.get("code") != 0:
            log.warning(f"GET /orders 返回 code={body.get('code')}，无法获取订单明细")
            return {}
        orders = body.get("data", {}).get("orders") or []
    except Exception as e:
        log.warning(f"GET /orders 请求异常：{e}")
        return {}
    by_acct: dict[str, dict[str, int]] = defaultdict(lambda: {"BUY": 0, "SELL": 0})
    for o in orders:
        d = o.get("direction", "")
        if d in ("BUY", "SELL"):
            by_acct[o["account_group"]][d] += 1
    return dict(by_acct)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="trade_date YYYYMMDD（默认下一交易日）")
    parser.add_argument("--today", action="store_true",
                        help="用今天作为 trade_date（仅调试，生产用默认下一交易日）")
    parser.add_argument("--live", action="store_true",
                        help="调用实盘专用 trigger；订单只写入 live 域")
    args = parser.parse_args()

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

    data = trigger(trade_date, live=args.live)
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
        # 拉取订单明细按账户/方向汇总
        # 实盘 trigger token 无订单读取权限；18:00 仍由 order_query 用实盘执行 token 拉取。
        breakdown = {} if args.live else _fetch_order_breakdown(trade_date)
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
