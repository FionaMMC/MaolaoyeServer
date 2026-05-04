"""
v2.3 统一数据采集引擎。
支持 QMT 行情/财务/分红/元数据 和 AKShare 宏观数据。
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
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd
import yaml

# 子目录 client/ 运行需补 v2.3/ 到 sys.path 才能 import config
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
# QMT 行情采集器
# ═══════════════════════════════════════════════════════════════════════════════

class MarketOHLCVCollector(BaseCollector):
    """
    日线 OHLCV 采集：股票 + ETF + 指数。
    数据结构与 market_push.py 保持一致，但写入本地 Parquet。
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

    def _get_code_lists(self):
        """获取三类标的代码列表。"""
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        stock_list = xtdata.get_stock_list_in_sector(self.stock_sector)
        etf_set = set()
        for s in self.etf_sectors:
            etf_set.update(xtdata.get_stock_list_in_sector(s))
        etf_list = sorted(etf_set)
        index_list = list(self.index_codes)

        return stock_list, etf_list, index_list

    def collect(self, trade_date: Optional[str] = None) -> int:
        from xtquant import xtdata
        xtdata.data_dir = config.QMT_USERDATA_DIR

        if trade_date is None:
            trade_date = getattr(config, "FORCE_TRADE_DATE", None) or datetime.now().strftime("%Y%m%d")

        log.info(f"开始采集行情，交易日={trade_date}")

        stock_list, etf_list, index_list = self._get_code_lists()
        all_codes = stock_list + etf_list + index_list
        log.info(f"标的数量: 股票 {len(stock_list)}, ETF {len(etf_list)}, 指数 {len(index_list)}, 合计 {len(all_codes)}")

        # 批量下载
        xtdata.download_history_data2(all_codes, "1d", trade_date, trade_date)
        time.sleep(self.sleep_sec)

        # 读取并分类写入
        total_new = 0
        for symbol in all_codes:
            try:
                raw = xtdata.get_market_data_ex(self.OHLCV_FIELDS, [symbol], "1d",
                                                 trade_date, trade_date,
                                                 dividend_type="front" if not symbol.endswith((".SH",".SZ")) or symbol[:1] not in ("30","00","60","51") else "none")
                # 修正：股票/ETF 用 front，指数用 none
                if symbol in index_list:
                    raw = xtdata.get_market_data_ex(self.OHLCV_FIELDS, [symbol], "1d",
                                                     trade_date, trade_date, dividend_type="none")
                else:
                    raw = xtdata.get_market_data_ex(self.OHLCV_FIELDS, [symbol], "1d",
                                                     trade_date, trade_date, dividend_type="front")

                if not raw or symbol not in raw or raw[symbol].empty:
                    continue

                df = raw[symbol].copy()
                df.reset_index(inplace=True)
                df.rename(columns={"index": "trade_date", "time": "trade_date"}, inplace=True)

                if "trade_date" in df.columns:
                    # 转换时间戳为 YYYYMMDD int
                    if df["trade_date"].dtype == "int64" and df["trade_date"].iloc[0] > 1e9:
                        df["trade_date"] = df["trade_date"].apply(
                            lambda ts: int(datetime.fromtimestamp(ts / 1000).strftime("%Y%m%d"))
                        )

                # 选择输出目录
                if symbol in index_list:
                    out_dir = self.INDEX_DIR
                elif symbol in etf_list:
                    out_dir = self.ETF_DIR
                else:
                    out_dir = self.STOCK_DIR

                filepath = os.path.join(config.DATA_DIR, out_dir, f"{symbol}.parquet")
                n = append_to_parquet(filepath, df, pk_col="trade_date")
                total_new += n

            except Exception as e:
                log.debug(f"跳过 {symbol}: {e}")

        log.info(f"行情采集完成，新增 {total_new} 行")
        return total_new


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

        # 获取全市场股票列表
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
                        if table not in raw[symbol] or not raw[symbol][table]:
                            continue
                        try:
                            df = pd.DataFrame(raw[symbol][table])
                            if df.empty:
                                continue
                            # 确定主键列
                            pk = "report_date" if "report_date" in df.columns else df.columns[0]
                            filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, table, f"{symbol}.parquet")
                            n = append_to_parquet(filepath, df, pk_col=pk)
                            total_new += n
                        except Exception as e:
                            log.debug(f"财务写入失败 {symbol}/{table}: {e}")

                log.info(f"财务批量 [{i}:{min(i+self.batch_size, len(stock_list))}] 完成")

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
                # 添加可读日期列
                if "time" in df.columns:
                    df["trade_date"] = df["time"].apply(
                        lambda ts: int(datetime.fromtimestamp(ts / 1000).strftime("%Y%m%d"))
                    )

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

        # 所有标的
        all_codes = xtdata.get_stock_list_in_sector("沪深A股")
        all_codes += xtdata.get_stock_list_in_sector("沪深ETF")
        all_codes = list(dict.fromkeys(all_codes))  # 去重

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
        # 全量覆盖写入（元数据每次覆盖即可）
        filepath = os.path.join(config.DATA_DIR, self.BASE_DIR, "all_instruments.parquet")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        df.to_parquet(filepath, index=False, compression="snappy")
        log.info(f"品种元数据完成: {len(df)} 条，写入 {filepath}")
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
        """调用 AKShare 函数获取数据。"""
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

            # 全量覆盖 + 排序（宏观数据量小，直接覆盖即可）
            # 尝试找出日期列并排序
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
    "financial_tables":   FinancialTablesCollector,
    "dividends":          DividendCollector,
    "instruments":        InstrumentCollector,
    "index_weights":      IndexWeightCollector,
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
                log.warning(f"  跳过 {name}: 无 function")
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
        # 限制子目录深度
        if root.count(os.sep) > config.DATA_DIR.count(os.sep) + 4:
            dirs.clear()

    print(f"\n{'='*60}")
    print(f"  数据统计 — {config.DATA_DIR}")
    print(f"{'='*60}")
    print(f"  Parquet 文件数: {total_files}")
    print(f"  总大小:         {total_size / 1024 / 1024:.1f} MB")
    print(f"{'='*60}")

    # 分类统计
    categories = {
        "行情-股票": "market/daily/stocks",
        "行情-ETF":  "market/daily/etfs",
        "行情-指数": "market/daily/indexes",
        "财务":      "fundamentals/financial_tables",
        "分红":      "fundamentals/dividends",
        "元数据":    "fundamentals/instruments",
        "指数权重":  "fundamentals/index_weights",
        "宏观-中国": "macro/china",
        "宏观-全球": "macro/global",
    }
    for label, subdir in categories.items():
        d = os.path.join(config.DATA_DIR, subdir)
        if os.path.isdir(d):
            files = [f for f in os.listdir(d) if f.endswith(".parquet")]
            size = sum(os.path.getsize(os.path.join(d, f)) for f in files)
            print(f"  {label:12s}: {len(files):6d} 文件, {size/1024/1024:8.1f} MB")
        else:
            print(f"  {label:12s}: (目录不存在)")

    print(f"{'='*60}\n")


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
