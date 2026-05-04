"""
v2.3 统一数据采集引擎。
支持 QMT 行情/财务/分红/元数据/可转债/期货/行业/板块/交易日历
和 AKShare 宏观数据。
所有数据以 Parquet 格式增量写入 data/ 目录。

用法:
  python data_collector.py --full              # 全量采集
  python data_collector.py --freq daily        # 按频率采集
  python data_collector.py --type market_ohlcv,financial_tables
  python data_collector.py --stats             # 查看数据统计
  python data_collector.py --type macro_cpi --dry-run
"""

import argparse
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd
import yaml

import config

log = config.setup_logger("data_collector")


# ═══════════════════════════════════════════════════════════════════════════════
# Parquet 工具
# ═══════════════════════════════════════════════════════════════════════════════

def append_to_parquet(filepath: str, new_df: pd.DataFrame, pk_col: str = "trade_date") -> int:
    """追加写入 Parquet，按主键去重。返回新增行数。"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    if new_df.empty:
        return 0
    if pk_col not in new_df.columns:
        raise ValueError(f"主键列 '{pk_col}' 不在 DataFrame 中, 可用列: {list(new_df.columns)}")

    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        existing = pd.read_parquet(filepath)
        existing_pks = set(existing[pk_col].values)
        new_rows = new_df[~new_df[pk_col].isin(existing_pks)]
        if new_rows.empty:
            return 0
        merged = pd.concat([existing, new_rows], ignore_index=True)
    else:
        new_rows = new_df.copy()
        merged = new_rows

    merged.sort_values(pk_col, inplace=True)
    merged.to_parquet(filepath, index=False, compression="snappy")
    return len(new_rows)


def read_parquet_safe(filepath: str) -> pd.DataFrame:
    """安全读取 Parquet，不存在时返回空 DataFrame。"""
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        return pd.read_parquet(filepath)
    return pd.DataFrame()


def _convert_timestamp_to_yyyymmdd(series: pd.Series) -> pd.Series:
    """将毫秒时间戳列转换为 YYYYMMDD 整数。"""
    return series.apply(
        lambda ts: int(datetime.fromtimestamp(ts / 1000).strftime("%Y%m%d"))
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 配置加载
# ═══════════════════════════════════════════════════════════════════════════════

def load_collectors_config() -> dict:
    with open(config.COLLECTORS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ═══════════════════════════════════════════════════════════════════════════════
# 基类
# ═══════════════════════════════════════════════════════════════════════════════

class BaseCollector(ABC):
    """所有采集器的基类。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name: str = ""
        self.desc: str = cfg.get("description", "")

    @abstractmethod
    def collect(self, trade_date: Optional[str] = None) -> int:
        """执行采集。返回新增数据行数。"""
        ...

    def dry_run(self, trade_date: Optional[str] = None):
        """预览采集信息，不写入。"""
        log.info(f"[DRY-RUN] {self.name}: {self.desc}")


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 行情采集器（股票/ETF/指数）
# ═══════════════════════════════════════════════════════════════════════════════

class MarketOHLCVCollector(BaseCollector):
    """
    日线 OHLCV 采集：股票 + ETF + 指数。
    支持前/后复权和不复权，默认前复权。
    """

    STOCK_DIR = "market/daily/stocks"
    ETF_DIR   = "market/daily/etfs"
    INDEX_DIR = "market/daily/indexes"

    OHLCV_FIELDS = ["open", "high", "low", "close", "volume", "amount", "suspendFlag"]

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "market_ohlcv"
        self.stock_sector = cfg.get("stock_sector", "沪深A股")
        self.etf_sectors   = cfg.get("etf_sectors", ["沪深ETF"])
        self.index_codes   = cfg.get("index_codes", [])
        self.sleep_sec     = cfg.get("download_sleep_seconds", 3)
        self.dividend_types = cfg.get("dividend_types", {
            "stocks": "front", "etfs": "front", "indexes": "none"
        })

    def _init_xtdata(self):
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR
        return xtdata

    def _get_code_lists(self, xtdata):
        stock_list = xtdata.get_stock_list_in_sector(self.stock_sector)
        etf_set = set()
        for s in self.etf_sectors:
            etf_set.update(xtdata.get_stock_list_in_sector(s))
        etf_list = sorted(etf_set)
        index_list = list(self.index_codes)
        return stock_list, etf_list, index_list

    def _download_and_save(self, xtdata, codes: list, dvd_type: str,
                           trade_date: str, out_dir: str) -> int:
        """下载一批标的并将数据写入 parquet。"""
        if not codes:
            return 0
        xtdata.download_history_data2(codes, "1d", trade_date, trade_date)
        time.sleep(self.sleep_sec)

        total = 0
        for symbol in codes:
            try:
                raw = xtdata.get_market_data_ex(
                    self.OHLCV_FIELDS, [symbol], "1d",
                    trade_date, trade_date, dividend_type=dvd_type
                )
                if not raw or symbol not in raw or raw[symbol].empty:
                    continue

                df = raw[symbol].copy()
                df.reset_index(inplace=True)
                df.rename(columns={"index": "trade_date", "time": "trade_date"}, inplace=True)

                if "trade_date" in df.columns and df["trade_date"].dtype == "int64":
                    if df["trade_date"].iloc[0] > 1e9:
                        df["trade_date"] = _convert_timestamp_to_yyyymmdd(df["trade_date"])

                filepath = os.path.join(config.DATA_DIR, out_dir, f"{symbol}.parquet")
                n = append_to_parquet(filepath, df, pk_col="trade_date")
                total += n
            except Exception as e:
                log.debug(f"跳过 {symbol}: {e}")
        return total

    def collect(self, trade_date: Optional[str] = None) -> int:
        xtdata = self._init_xtdata()
        if trade_date is None:
            trade_date = getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")

        log.info(f"开始采集行情，交易日={trade_date}")
        stock_list, etf_list, index_list = self._get_code_lists(xtdata)
        all_codes = stock_list + etf_list + index_list
        log.info(f"标的: 股票 {len(stock_list)}, ETF {len(etf_list)}, 指数 {len(index_list)}, 合计 {len(all_codes)}")

        # 批量下载所有标的（不复权，一次搞定）
        xtdata.download_history_data2(all_codes, "1d", trade_date, trade_date)
        time.sleep(max(self.sleep_sec, 5))

        total_new = 0

        # 股票（前复权）
        dvd = self.dividend_types.get("stocks", "front")
        log.info(f"股票 OHLCV: dividend_type={dvd}")
        n = self._download_and_save(xtdata, stock_list, dvd, trade_date, self.STOCK_DIR)
        total_new += n
        log.info(f"  股票: {n} 行新增")

        # ETF（前复权）
        dvd = self.dividend_types.get("etfs", "front")
        log.info(f"ETF OHLCV: dividend_type={dvd}")
        n = self._download_and_save(xtdata, etf_list, dvd, trade_date, self.ETF_DIR)
        total_new += n
        log.info(f"  ETF: {n} 行新增")

        # 指数（不复权）
        dvd = self.dividend_types.get("indexes", "none")
        log.info(f"指数 OHLCV: dividend_type={dvd}")
        n = self._download_and_save(xtdata, index_list, dvd, trade_date, self.INDEX_DIR)
        total_new += n
        log.info(f"  指数: {n} 行新增")

        log.info(f"行情采集完成，新增 {total_new} 行")
        return total_new


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 可转债采集器
# ═══════════════════════════════════════════════════════════════════════════════

class ConvertibleBondCollector(BaseCollector):
    """可转债日线 OHLCV。"""

    BASE_DIR = "market/daily/convertible_bonds"
    OHLCV_FIELDS = ["open", "high", "low", "close", "volume", "amount"]

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "convertible_bonds"
        self.sector = cfg.get("sector", "可转债")
        self.sleep_sec = cfg.get("download_sleep_seconds", 3)

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        if trade_date is None:
            trade_date = getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")

        # 获取全部可转债
        cb_list = xtdata.get_stock_list_in_sector(self.sector)
        # QMT 可转债代码通常是 123xxx.SZ 或 110xxx.SH -- 只保留沪深交易所的
        cb_list = [c for c in cb_list if c.endswith(('.SZ', '.SH'))]
        log.info(f"可转债采集: {len(cb_list)} 只, 交易日={trade_date}")

        if not cb_list:
            return 0

        xtdata.download_history_data2(cb_list, "1d", trade_date, trade_date)
        time.sleep(self.sleep_sec)

        total = 0
        for symbol in cb_list:
            try:
                raw = xtdata.get_market_data_ex(
                    self.OHLCV_FIELDS, [symbol], "1d",
                    trade_date, trade_date, dividend_type="none"
                )
                if not raw or symbol not in raw or raw[symbol].empty:
                    continue
                df = raw[symbol].copy()
                df.reset_index(inplace=True)
                df.rename(columns={"index": "trade_date", "time": "trade_date"}, inplace=True)
                if "trade_date" in df.columns and df["trade_date"].dtype == "int64":
                    if df["trade_date"].iloc[0] > 1e9:
                        df["trade_date"] = _convert_timestamp_to_yyyymmdd(df["trade_date"])

                filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, f"{symbol}.parquet")
                n = append_to_parquet(filepath, df, pk_col="trade_date")
                total += n
            except Exception as e:
                log.debug(f"可转债跳过 {symbol}: {e}")

        log.info(f"可转债采集完成，新增 {total} 行")
        return total


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 期货采集器
# ═══════════════════════════════════════════════════════════════════════════════

class FuturesCollector(BaseCollector):
    """期货主力合约日线 OHLCV。"""

    BASE_DIR = "market/daily/futures"
    OHLCV_FIELDS = ["open", "high", "low", "close", "volume", "amount", "openInterest"]

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "futures"
        self.sectors = cfg.get("sectors", ["金融期货", "商品期货"])
        self.sleep_sec = cfg.get("download_sleep_seconds", 3)

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        if trade_date is None:
            trade_date = getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")

        # 获取所有期货板块的合约
        all_contracts = set()
        for sec in self.sectors:
            try:
                contracts = xtdata.get_stock_list_in_sector(sec)
                all_contracts.update(contracts)
                log.info(f"  板块 [{sec}]: {len(contracts)} 个合约")
            except Exception as e:
                log.warning(f"期货板块 [{sec}] 失败: {e}")

        contract_list = sorted(all_contracts)
        log.info(f"期货采集: {len(contract_list)} 个合约, 交易日={trade_date}")

        if not contract_list:
            return 0

        xtdata.download_history_data2(contract_list, "1d", trade_date, trade_date)
        time.sleep(self.sleep_sec)

        total = 0
        for symbol in contract_list:
            try:
                raw = xtdata.get_market_data_ex(
                    self.OHLCV_FIELDS, [symbol], "1d",
                    trade_date, trade_date, dividend_type="none"
                )
                if not raw or symbol not in raw or raw[symbol].empty:
                    continue
                df = raw[symbol].copy()
                df.reset_index(inplace=True)
                df.rename(columns={"index": "trade_date", "time": "trade_date"}, inplace=True)
                if "trade_date" in df.columns and df["trade_date"].dtype == "int64":
                    if df["trade_date"].iloc[0] > 1e9:
                        df["trade_date"] = _convert_timestamp_to_yyyymmdd(df["trade_date"])

                # 提取品种代码（如 IF、IC 等）
                match = re.match(r'^([A-Za-z]+)', symbol)
                if match:
                    df["product"] = match.group(1).upper()

                filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, f"{symbol}.parquet")
                n = append_to_parquet(filepath, df, pk_col="trade_date")
                total += n
            except Exception as e:
                log.debug(f"期货跳过 {symbol}: {e}")

        log.info(f"期货采集完成，新增 {total} 行")
        return total


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 财务数据采集器
# ═══════════════════════════════════════════════════════════════════════════════

class FinancialTablesCollector(BaseCollector):
    """财务数据 7 张表，按标的分别存储。"""

    BASE_DIR = "fundamentals/financial_tables"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "financial_tables"
        self.tables = cfg.get("tables", [])
        self.start_time = cfg.get("start_time", "")
        self.batch_size = cfg.get("batch_size", 100)
        self.sleep_sec = cfg.get("download_sleep_seconds", 5)

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        stock_list = xtdata.get_stock_list_in_sector("沪深A股")
        log.info(f"财务数据采集: {len(stock_list)} 只股票, {len(self.tables)} 张表")

        total_new = 0
        for i in range(0, len(stock_list), self.batch_size):
            batch = stock_list[i:i + self.batch_size]
            try:
                xtdata.download_financial_data2(batch, self.tables,
                                                 start_time=self.start_time, end_time="")
                time.sleep(self.sleep_sec)

                raw = xtdata.get_financial_data(batch, self.tables,
                                                 start_time=self.start_time, end_time="")
                if not raw:
                    continue

                for symbol in batch:
                    if symbol not in raw:
                        continue
                    for table in self.tables:
                        if table not in raw[symbol]:
                            continue
                        df = raw[symbol][table]
                        if df is None or (hasattr(df, 'empty') and df.empty):
                            continue
                        try:
                            pk = "report_date" if "report_date" in df.columns else df.columns[0]
                            filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, table, f"{symbol}.parquet")
                            n = append_to_parquet(filepath, df, pk_col=pk)
                            total_new += n
                        except Exception as e:
                            log.debug(f"财务写入失败 {symbol}/{table}: {e}")

                progress = min(i+self.batch_size, len(stock_list))
                log.info(f"财务批量 [{i}:{progress}]/{len(stock_list)} 完成, 累计新增 {total_new}")

            except Exception as e:
                log.error(f"财务批量下载失败 [{i}:{i+self.batch_size}]: {e}")

        log.info(f"财务数据采集完成，新增 {total_new} 行")
        return total_new


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 分红采集器
# ═══════════════════════════════════════════════════════════════════════════════

class DividendCollector(BaseCollector):
    """分红送配因子，按标的分别存储。"""

    BASE_DIR = "fundamentals/dividends"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "dividends"

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        stock_list = xtdata.get_stock_list_in_sector("沪深A股")
        log.info(f"分红数据采集: {len(stock_list)} 只股票")

        total_new = 0
        for i, symbol in enumerate(stock_list):
            try:
                data = xtdata.get_divid_factors(symbol)
                if data is None:
                    continue
                df = data if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
                if df.empty:
                    continue
                if "time" in df.columns:
                    df["trade_date"] = _convert_timestamp_to_yyyymmdd(df["time"])

                filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, f"{symbol}.parquet")
                n = append_to_parquet(filepath, df, pk_col="time")
                total_new += n

                if (i + 1) % 500 == 0:
                    log.info(f"分红进度: {i+1}/{len(stock_list)}, 新增 {total_new} 行")

            except Exception as e:
                log.debug(f"分红跳过 {symbol}: {e}")

        log.info(f"分红数据采集完成，新增 {total_new} 行")
        return total_new


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 品种元数据采集器
# ═══════════════════════════════════════════════════════════════════════════════

class InstrumentCollector(BaseCollector):
    """品种基础信息（元数据）快照。"""

    BASE_DIR = "fundamentals/instruments"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "instruments"
        self.invalidate_days = cfg.get("invalidate_days", 30)

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        if trade_date is None:
            trade_date = getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")

        all_codes = xtdata.get_stock_list_in_sector("沪深A股")
        all_codes += xtdata.get_stock_list_in_sector("沪深ETF")
        all_codes = list(dict.fromkeys(all_codes))

        log.info(f"品种元数据采集: {len(all_codes)} 个标的")

        rows = []
        for symbol in all_codes:
            try:
                detail = xtdata.get_instrument_detail(symbol)
                if not detail:
                    continue
                detail["symbol"] = symbol
                detail["update_date"] = int(trade_date)
                rows.append(detail)
            except Exception as e:
                log.debug(f"元数据跳过 {symbol}: {e}")

        if not rows:
            return 0

        df = pd.DataFrame(rows)
        filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, "all_instruments.parquet")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        df.to_parquet(filepath, index=False, compression="snappy")
        log.info(f"品种元数据完成: {len(df)} 条 → {filepath}")
        return len(df)


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 行业/板块映射采集器
# ═══════════════════════════════════════════════════════════════════════════════

class IndustryMapCollector(BaseCollector):
    """
    每只股票 → 行业/板块/概念/地域 映射表。
    覆盖 SW/CSRC/GICS/地域 等多级分类体系。
    """

    BASE_DIR = "fundamentals/industry_map"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "industry_map"
        self.classifications = cfg.get("classifications", [
            {"pattern": "SW1"}, {"pattern": "CSRC1"}, {"pattern": "GICS1"},
            {"pattern": "DY1"}, {"pattern": "DY2"},
        ])

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        if trade_date is None:
            trade_date = getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")

        # 获取所有板块名
        all_sectors = xtdata.get_sector_list()
        log.info(f"板块总数: {len(all_sectors)}")

        # 按 pattern 筛选板块名
        patterns = [c["pattern"] for c in self.classifications]
        selected_sectors = []
        for s in all_sectors:
            for p in patterns:
                if re.match(f"^{p}", s):
                    selected_sectors.append(s)
                    break

        selected_sectors = sorted(set(selected_sectors))
        log.info(f"匹配分类的板块数: {len(selected_sectors)}")
        log.info(f"分类: {patterns}")

        # 构建股票→板块映射
        rows = []
        for sec in selected_sectors:
            try:
                codes = xtdata.get_stock_list_in_sector(sec)
                for code in codes:
                    rows.append({
                        "symbol": code,
                        "sector_name": sec,
                        "update_date": int(trade_date),
                    })
                if len(selected_sectors) > 1:
                    log.debug(f"  {sec}: {len(codes)} 只标的")
            except Exception as e:
                log.warning(f"板块 {sec} 失败: {e}")

        if not rows:
            return 0

        df = pd.DataFrame(rows)
        df.drop_duplicates(subset=["symbol", "sector_name"], inplace=True)

        filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, "industry_map.parquet")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        df.to_parquet(filepath, index=False, compression="snappy")
        log.info(f"行业映射完成: {len(df)} 条 → {filepath}")
        return len(df)


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 指数权重采集器
# ═══════════════════════════════════════════════════════════════════════════════

class IndexWeightCollector(BaseCollector):
    """核心指数成分股权重。"""

    BASE_DIR = "fundamentals/index_weights"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "index_weights"
        self.indexes = cfg.get("indexes", [])

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        if trade_date is None:
            trade_date = getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")

        total_new = 0
        for idx_code in self.indexes:
            try:
                weights = xtdata.get_index_weight(idx_code)
                if not weights:
                    continue
                rows = [{"symbol": k, "weight": v, "index_code": idx_code,
                         "update_date": int(trade_date)} for k, v in weights.items()]
                df = pd.DataFrame(rows)
                filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, f"{idx_code}.parquet")
                n = append_to_parquet(filepath, df, pk_col="symbol")
                total_new += n
                log.info(f"  指数 {idx_code}: {n} 行新增")
            except Exception as e:
                log.warning(f"指数权重 {idx_code} 失败: {e}")

        log.info(f"指数权重采集完成，新增 {total_new} 行")
        return total_new


# ═══════════════════════════════════════════════════════════════════════════════
# QMT ETF 申赎清单 (PCF) 采集器
# ═══════════════════════════════════════════════════════════════════════════════

class EtfPcCollector(BaseCollector):
    """ETF 申购赎回清单（PCF）。"""

    BASE_DIR = "fundamentals/etf_pc"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "etf_pc"
        self.etf_sector = cfg.get("etf_sector", "沪深ETF")
        self.top_n = cfg.get("top_n", 200)

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        if trade_date is None:
            trade_date = getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")

        etf_list = xtdata.get_stock_list_in_sector(self.etf_sector)
        etf_list = sorted(etf_list)[:self.top_n]  # 取前 N 只
        log.info(f"ETF PCF 采集: {len(etf_list)} 只 ETF")

        all_rows = []
        component_rows = []  # 成分股明细
        for i, symbol in enumerate(etf_list):
            try:
                info = xtdata.get_etf_info(symbol)
                if info is None or not isinstance(info, dict) or not info:
                    continue
                # 提取标量字段（净值 / 现金余额等）
                row = {"symbol": symbol, "update_date": int(trade_date)}
                stocks = info.pop("stocks", {})
                for k, v in info.items():
                    row[k] = str(v) if isinstance(v, (dict, list)) else v
                all_rows.append(row)
                # 提取成分股明细
                for component_symbol, comp_info in stocks.items():
                    cr = {"etf_symbol": symbol, "component_symbol": component_symbol,
                          "update_date": int(trade_date)}
                    if isinstance(comp_info, dict):
                        cr.update({k: str(v) if isinstance(v, (dict, list)) else v
                                   for k, v in comp_info.items()})
                    else:
                        cr["raw"] = str(comp_info)
                    component_rows.append(cr)
                if (i + 1) % 50 == 0:
                    log.info(f"ETF PCF 进度: {i+1}/{len(etf_list)}")
            except Exception as e:
                log.debug(f"ETF PCF 跳过 {symbol}: {e}")

        if not all_rows:
            return 0

        # ETF 概要信息
        df = pd.DataFrame(all_rows)
        filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, f"etf_info_{trade_date}.parquet")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        df.to_parquet(filepath, index=False, compression="snappy")
        n1 = len(df)

        # ETF 成分股明细
        if component_rows:
            df_c = pd.DataFrame(component_rows)
            filepath_c = os.path.join(config.DATA_DIR, self.BASE_DIR, f"etf_components_{trade_date}.parquet")
            df_c.to_parquet(filepath_c, index=False, compression="snappy")
            n2 = len(df_c)
            log.info(f"ETF PCF 完成: 概要 {n1} 条 + 成分股 {n2} 条 → {os.path.dirname(filepath)}")
            return n1 + n2

        log.info(f"ETF PCF 完成: {n1} 条 → {filepath}")
        return n1


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 板块列表采集器
# ═══════════════════════════════════════════════════════════════════════════════

class SectorListCollector(BaseCollector):
    """所有板块/行业/概念/地域完整列表及成分股数量。"""

    BASE_DIR = "reference"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "sector_list"

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        if trade_date is None:
            trade_date = getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")

        sectors = xtdata.get_sector_list()
        log.info(f"板块列表采集: {len(sectors)} 个板块")

        rows = []
        for sec in sectors:
            try:
                codes = xtdata.get_stock_list_in_sector(sec)
                rows.append({
                    "sector_name": sec,
                    "member_count": len(codes),
                    "update_date": int(trade_date),
                })
            except Exception as e:
                log.warning(f"板块 {sec} 失败: {e}")

        df = pd.DataFrame(rows)
        filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, "sector_list.parquet")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        df.to_parquet(filepath, index=False, compression="snappy")
        log.info(f"板块列表完成: {len(df)} 条 → {filepath}")
        return len(df)


# ═══════════════════════════════════════════════════════════════════════════════
# QMT 交易日历采集器
# ═══════════════════════════════════════════════════════════════════════════════

class TradingCalendarCollector(BaseCollector):
    """交易日历缓存。"""

    BASE_DIR = "reference"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "trading_calendar"
        self.years_ahead = cfg.get("years_ahead", 1)

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        now = datetime.now()
        start = f"{now.year - 5}0101"
        end   = f"{now.year + self.years_ahead}1231"

        dates = xtdata.get_trading_dates("SH", start, end)
        # get_trading_dates 返回毫秒时间戳列表
        rows = []
        for ts in dates:
            dt = datetime.fromtimestamp(ts / 1000)
            rows.append({
                "trade_date": int(dt.strftime("%Y%m%d")),
                "year": dt.year,
                "month": dt.month,
                "weekday": dt.weekday(),
                "is_trading_day": True,
            })

        df = pd.DataFrame(rows)
        df.sort_values("trade_date", inplace=True)

        filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, "trading_calendar.parquet")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        df.to_parquet(filepath, index=False, compression="snappy")
        log.info(f"交易日历完成: {len(df)} 天 ({start}~{end}) → {filepath}")
        return len(df)


# ═══════════════════════════════════════════════════════════════════════════════
# AKShare 宏观数据采集器
# ═══════════════════════════════════════════════════════════════════════════════

class MacroBaseCollector(BaseCollector):
    """宏观数据采集器基类，处理 AKShare 调用和 Parquet 存储。"""

    BASE_DIR = "macro"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.function_name = cfg.get("function", "")
        self.sub_dir = "china"

    def _call_akshare(self) -> pd.DataFrame:
        import akshare as ak
        fn = getattr(ak, self.function_name)
        return fn()

    def collect(self, trade_date: Optional[str] = None) -> int:
        try:
            df = self._call_akshare()
            if df.empty:
                log.warning(f"{self.name}: 返回空数据")
                return 0

            filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, self.sub_dir, f"{self.name}.parquet")
            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            date_candidates = [c for c in df.columns if any(kw in str(c).lower()
                              for kw in ["date", "日期", "月份", "时间", "统计"])]
            if date_candidates:
                df.sort_values(date_candidates[0], inplace=True)

            df.to_parquet(filepath, index=False, compression="snappy")
            log.info(f"{self.name}: {len(df)} 行 → {filepath}")
            return len(df)

        except Exception as e:
            log.error(f"{self.name} 采集失败: {e}")
            return 0


# ═══════════════════════════════════════════════════════════════════════════════
# 采集器注册表
# ═══════════════════════════════════════════════════════════════════════════════

COLLECTOR_CLASSES = {
    "market_ohlcv":       MarketOHLCVCollector,
    "convertible_bonds":  ConvertibleBondCollector,
    "futures":            FuturesCollector,
    "financial_tables":   FinancialTablesCollector,
    "dividends":          DividendCollector,
    "instruments":        InstrumentCollector,
    "industry_map":       IndustryMapCollector,
    "index_weights":      IndexWeightCollector,
    "etf_pc":             EtfPcCollector,
    "sector_list":        SectorListCollector,
    "trading_calendar":   TradingCalendarCollector,
}


class MacroGlobalCollector(BaseCollector):
    """全球宏观数据采集器，遍历 indicators 列表。"""

    BASE_DIR = "macro/global"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "macro_global"
        self.indicators = cfg.get("indicators", [])

    def collect(self, trade_date: Optional[str] = None) -> int:
        import akshare as ak
        total = 0
        for ind in self.indicators:
            name = ind.get("name", "")
            fn_name = ind.get("function", "")
            if not fn_name:
                continue
            try:
                fn = getattr(ak, fn_name)
                df = fn()
                if df.empty:
                    log.warning(f"  {name}: 空数据")
                    continue
                filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, f"{name}.parquet")
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                df.to_parquet(filepath, index=False, compression="snappy")
                log.info(f"  {name}: {len(df)} 行 → {filepath}")
                total += len(df)
            except Exception as e:
                log.error(f"  {name} ({fn_name}): {e}")
        return total

    def dry_run(self, trade_date: Optional[str] = None):
        log.info(f"[DRY-RUN] {self.name}: {len(self.indicators)} 个全球指标")


def _make_macro_collector(name: str, cfg: dict) -> BaseCollector:
    """动态创建宏观采集器实例。"""
    if name == "macro_global":
        return MacroGlobalCollector(cfg)
    c = MacroBaseCollector(cfg)
    c.name = name
    c.sub_dir = "china" if name != "macro_global" else "global"
    return c


def get_collector(name: str, cfg: dict) -> Optional[BaseCollector]:
    """根据名称获取采集器实例。"""
    if name in COLLECTOR_CLASSES:
        return COLLECTOR_CLASSES[name](cfg)
    elif name.startswith("macro_"):
        return _make_macro_collector(name, cfg)
    return None


def get_all_collectors() -> list[tuple[str, BaseCollector]]:
    """加载 collectors.yaml 并创建所有已启用的采集器。"""
    cc = load_collectors_config()
    collectors = []
    for name, cfg in cc.items():
        if name in ("settings", "frequencies"):
            continue
        if isinstance(cfg, dict) and cfg.get("enabled", True):
            c = get_collector(name, cfg)
            if c:
                collectors.append((name, c))
    return collectors


def get_collectors_by_freq(freq: str) -> list[tuple[str, BaseCollector]]:
    """按频率获取采集器列表。"""
    cc = load_collectors_config()
    freq_map = cc.get("frequencies", {})
    names = freq_map.get(freq, [])
    collectors = []
    for name in names:
        cfg = cc.get(name, {})
        if cfg.get("enabled", True):
            c = get_collector(name, cfg)
            if c:
                collectors.append((name, c))
    return collectors


# ═══════════════════════════════════════════════════════════════════════════════
# 数据统计
# ═══════════════════════════════════════════════════════════════════════════════

def show_stats():
    """显示各类数据的覆盖情况。"""
    total_files = 0
    total_size = 0
    for root, dirs, files in os.walk(config.DATA_DIR):
        for f in files:
            if f.endswith(".parquet"):
                fp = os.path.join(root, f)
                sz = os.path.getsize(fp)
                total_files += 1
                total_size += sz
        if root.count(os.sep) > config.DATA_DIR.count(os.sep) + 4:
            dirs.clear()

    print(f"\n{'='*65}")
    print(f"  数据统计 — {config.DATA_DIR}")
    print(f"{'='*65}")
    print(f"  Parquet 文件数: {total_files}")
    print(f"  总大小:         {total_size / 1024 / 1024:.1f} MB")
    print(f"{'='*65}")

    categories = {
        "行情-股票":        "market/daily/stocks",
        "行情-ETF":         "market/daily/etfs",
        "行情-指数":        "market/daily/indexes",
        "行情-可转债":      "market/daily/convertible_bonds",
        "行情-期货":        "market/daily/futures",
        "财务":             "fundamentals/financial_tables",
        "分红":             "fundamentals/dividends",
        "元数据":           "fundamentals/instruments",
        "行业映射":         "fundamentals/industry_map",
        "指数权重":         "fundamentals/index_weights",
        "ETF_申赎":         "fundamentals/etf_pc",
        "宏观-中国":        "macro/china",
        "宏观-全球":        "macro/global",
        "参考数据":         "reference",
    }
    for label, subdir in categories.items():
        d = os.path.join(config.DATA_DIR, subdir)
        if os.path.isdir(d):
            files = [f for f in os.listdir(d) if f.endswith(".parquet")]
            # 递归统计子目录
            if any(os.path.isdir(os.path.join(d, x)) for x in os.listdir(d)):
                count = 0
                size = 0
                for root, dirs, fnames in os.walk(d):
                    for fn in fnames:
                        if fn.endswith(".parquet"):
                            count += 1
                            size += os.path.getsize(os.path.join(root, fn))
                print(f"  {label:12s}: {count:6d} 文件, {size/1024/1024:8.1f} MB")
            else:
                size = sum(os.path.getsize(os.path.join(d, f)) for f in files)
                print(f"  {label:12s}: {len(files):6d} 文件, {size/1024/1024:8.1f} MB")
        else:
            print(f"  {label:12s}: (目录不存在)")

    print(f"{'='*65}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="v2.3 数据采集引擎")
    parser.add_argument("--full", action="store_true", help="全量采集所有已启用数据")
    parser.add_argument("--freq", type=str, choices=["daily", "weekly", "monthly", "quarterly"],
                        help="按频率采集")
    parser.add_argument("--type", type=str, help="逗号分隔的采集器名称列表")
    parser.add_argument("--stats", action="store_true", help="查看数据统计")
    parser.add_argument("--dry-run", action="store_true", help="预览，不实际写入")
    parser.add_argument("--date", type=str, help="指定交易日 (YYYYMMDD)，默认今天或 FORCE_TRADE_DATE")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    trade_date = args.date or getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")
    log.info(f"交易日: {trade_date}")

    if args.full:
        collectors = get_all_collectors()
        log.info(f"全量采集: {len(collectors)} 个采集器")
    elif args.freq:
        collectors = get_collectors_by_freq(args.freq)
        log.info(f"频率 [{args.freq}]: {len(collectors)} 个采集器")
    elif args.type:
        cc = load_collectors_config()
        names = [n.strip() for n in args.type.split(",")]
        collectors = []
        for name in names:
            cfg = cc.get(name, {})
            c = get_collector(name, cfg)
            if c:
                collectors.append((name, c))
        log.info(f"指定类型: {[n for n,_ in collectors]}")
    else:
        print("请指定 --full / --freq / --type / --stats")
        return

    total_added = 0
    for name, collector in collectors:
        log.info(f"\n{'='*50}\n  [{name}] {collector.desc}\n{'='*50}")
        try:
            if args.dry_run:
                collector.dry_run(trade_date)
            else:
                n = collector.collect(trade_date)
                total_added += n
                log.info(f"  [{name}] 完成，新增 {n} 行")
        except Exception as e:
            log.error(f"  [{name}] 失败: {e}", exc_info=True)

    log.info(f"\n全部采集完成，累计新增 {total_added} 行")


if __name__ == "__main__":
    main()
