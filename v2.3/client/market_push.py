"""
模块一：行情下载与推送（v2.1）
mock_qmt 模式：跳过 QMT 下载，直接读取 mock_data/market_payload_mock.json 并推送。
"""

import os
import time
from datetime import datetime

import requests
from xtquant import xtdata

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

xtdata.data_dir = config.QMT_USERDATA_DIR
log = config.setup_logger("market_push")


def _wechat_alert(msg: str) -> None:
    log.error(f"[微信报警] {msg}")
    if not config.notify_wecom(msg, level="alert"):
        log.error(f"[微信报警] 推送失败：{msg}")

def _wechat_notify(msg: str) -> None:
    log.info(f"[微信通知] {msg}")
    if not config.notify_wecom(msg, level="info"):
        log.error(f"[微信通知] 推送失败：{msg}")


def startup_check() -> tuple[list, list, list]:
    assert os.path.exists(config.QMT_USERDATA_DIR), \
        f"QMT 数据目录不存在: {config.QMT_USERDATA_DIR}"
    if config.PUSH_MODE == "server":
        assert config.SERVER_BASE_URL, "SERVER_BASE_URL 未配置"
        assert config.API_KEY,         "API_KEY 未配置"

    if config.PUSH_MODE == "mock_qmt":
        log.info("mock_qmt 模式：跳过 QMT 板块查询，使用 market_payload_mock.json")
        return [], [], []

    stock_list = xtdata.get_stock_list_in_sector(config.STOCK_SECTOR)
    log.info(f"板块 '{config.STOCK_SECTOR}'：{len(stock_list)} 只股票")
    assert len(stock_list) >= 100, (
        f"STOCK_SECTOR='{config.STOCK_SECTOR}' 仅返回 {len(stock_list)} 只，疑似板块名错误"
    )
    etf_set = set()
    for sector in config.ETF_SECTORS:
        members = xtdata.get_stock_list_in_sector(sector)
        log.info(f"板块 '{sector}'：{len(members)} 只 ETF")
        etf_set.update(members)
    # v2.2：指数使用硬编码代码列表（QMT 无纯市场指数板块）
    index_list = list(getattr(config, "INDEX_CODES", []))
    log.info(f"指数代码（硬编码）：{len(index_list)} 只（{', '.join(index_list)}）")
    return stock_list, list(etf_set), index_list


def is_trading_day(date_str: str) -> bool:
    days = xtdata.get_trading_calendar("SH", start_time=date_str, end_time=date_str)
    return len(days) > 0


def _download_all(all_codes: list) -> None:
    log.info(f"开始 download_history_data2，共 {len(all_codes)} 个标的（增量模式）...")
    xtdata.download_history_data2(all_codes, period="1d", start_time="", end_time="")
    log.info("download_history_data2 返回，等待 3 秒后读取...")
    time.sleep(3)


def _read_klines(codes: list, trade_date: str, dividend_type: str) -> dict:
    if not codes:
        return {}
    raw = xtdata.get_market_data_ex(
        field_list=["open", "high", "low", "close", "volume", "amount", "suspendFlag"],
        stock_list=codes,
        period="1d",
        start_time=trade_date,
        end_time=trade_date,
        count=-1,
        dividend_type=dividend_type,
        fill_data=False,
    )
    return raw or {}


def _clean_stock_or_etf(symbol: str, df) -> dict | None:
    if df is None or df.empty:
        return None
    row = df.iloc[-1]
    if row.isna().all():
        return None
    return {
        "symbol":       symbol,
        "open":         float(row.get("open", 0) or 0),
        "high":         float(row.get("high", 0) or 0),
        "low":          float(row.get("low",  0) or 0),
        "close":        float(row.get("close", 0) or 0),
        "volume":       int(row.get("volume", 0) or 0),
        "amount":       float(row.get("amount", 0) or 0),
        "is_suspended": int(row.get("suspendFlag", 0) or 0) == 1,
    }


def _clean_index(symbol: str, df) -> dict | None:
    if df is None or df.empty:
        return None
    row = df.iloc[-1]
    if row.isna().all():
        return None
    return {
        "symbol": symbol,
        "open":   float(row.get("open", 0) or 0),
        "high":   float(row.get("high", 0) or 0),
        "low":    float(row.get("low",  0) or 0),
        "close":  float(row.get("close", 0) or 0),
        "volume": int(row.get("volume", 0) or 0),
        "amount": float(row.get("amount", 0) or 0),
    }


def _build_payload(trade_date, stocks_raw, etfs_raw, indexes_raw) -> dict:
    def _collect(raw_dict, clean_fn):
        items, skipped = [], 0
        for sym, df in raw_dict.items():
            r = clean_fn(sym, df)
            if r:
                items.append(r)
            else:
                skipped += 1
        return items, skipped

    stocks,  s_skip = _collect(stocks_raw,  _clean_stock_or_etf)
    etfs,    e_skip = _collect(etfs_raw,    _clean_stock_or_etf)
    indexes, i_skip = _collect(indexes_raw, _clean_index)

    log.info(
        f"清洗完成：股票 {len(stocks)}（跳过 {s_skip}）| "
        f"ETF {len(etfs)}（跳过 {e_skip}）| 指数 {len(indexes)}（跳过 {i_skip}）"
    )
    if stocks_raw and s_skip > len(stocks) * 0.1:
        log.warning(f"跳过股票数量超过 10%（{s_skip} 只），请检查下载是否完整")
    return {"trade_date": trade_date, "stocks": stocks, "indexes": indexes, "etfs": etfs}


def _push_to_server(payload: dict) -> bool:
    url     = config.SERVER_BASE_URL.rstrip("/") + "/market-data"
    headers = {"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        body = resp.json()
        code = body.get("code")
        if code == 0:
            d = body.get("data", {})
            recv = d.get("received", {})
            log.info(f"推送成功：stocks={recv.get('stocks')} etfs={recv.get('etfs')} indexes={recv.get('indexes')}")
            return True
        if code == 2001:
            log.warning("服务器返回 2001（日期重复），视为成功")
            return True
        log.error(f"推送失败：code={code} message={body.get('message')}")
        return False
    except Exception as e:
        log.error(f"推送异常：{e}")
        return False


def _push(payload: dict, trade_date: str) -> None:
    if config.PUSH_MODE in ("local", "mock_qmt"):
        filename = f"market_data_{trade_date}.json"
        config.save_local_push(filename, payload)
        log.info(
            f"[{config.PUSH_MODE}] payload 已写入 local_push/{filename} "
            f"（stocks={len(payload['stocks'])} etfs={len(payload['etfs'])} "
            f"indexes={len(payload['indexes'])}）"
        )
        _wechat_notify(f"行情推送（{config.PUSH_MODE}）{trade_date}：ok")
        return

    for attempt in range(1, config.PUSH_MAX_RETRIES + 1):
        log.info(f"第 {attempt}/{config.PUSH_MAX_RETRIES} 次推送...")
        if _push_to_server(payload):
            _wechat_notify(f"行情推送成功 {trade_date}")
            return
        if attempt < config.PUSH_MAX_RETRIES:
            time.sleep(config.PUSH_RETRY_INTERVAL_SECONDS)
    _wechat_alert(f"行情推送失败：重试 {config.PUSH_MAX_RETRIES} 次仍失败（trade_date={trade_date}）")


def main():
    log.info("=== market_push 启动（v2.2）===")
    startup_check()

    if config.PUSH_MODE == "mock_qmt":
        trade_date = config.MOCK_TRADE_DATE
        log.info(f"mock_qmt 模式：读取 market_payload_mock.json（trade_date={trade_date}）")
        payload = config.load_mock_market_payload()
        payload["trade_date"] = trade_date
        _push(payload, trade_date)
        log.info("=== market_push 完成（mock_qmt）===")
        return

    # v2.2：支持 FORCE_TRADE_DATE 强制指定交易日
    trade_date = getattr(config, "FORCE_TRADE_DATE", None)
    if trade_date:
        log.info(f"FORCE_TRADE_DATE={trade_date}，强制作为交易日下载")
    else:
        trade_date = datetime.now().strftime("%Y%m%d")
        if not is_trading_day(trade_date):
            log.info(f"今日 {trade_date} 非交易日，正常退出")
            return

    log.info(f"交易日 {trade_date}，开始下载行情")
    stock_list, etf_list, index_list = startup_check()
    all_codes = stock_list + etf_list + index_list
    _download_all(all_codes)

    stocks_raw  = _read_klines(stock_list,  trade_date, "front")
    etfs_raw    = _read_klines(etf_list,    trade_date, "front")
    indexes_raw = _read_klines(index_list,  trade_date, "none")

    if not stocks_raw:
        _wechat_alert(f"股票行情读取结果为空（trade_date={trade_date}）")
        return

    payload = _build_payload(trade_date, stocks_raw, etfs_raw, indexes_raw)
    if not payload["stocks"]:
        _wechat_alert(f"清洗后股票数据为空（trade_date={trade_date}）")
        return

    _push(payload, trade_date)
    log.info("=== market_push 完成 ===")


if __name__ == "__main__":
    main()
