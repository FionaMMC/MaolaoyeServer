"""Server 端配置：环境变量 QMT_* + .env 文件。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有 server 端配置集中在此类。

    优先级：环境变量 > .env > 默认值。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="QMT_",
        extra="ignore",
        case_sensitive=False,
    )

    # HTTP / 鉴权
    api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8000

    # 业务数据
    db_url: str = "sqlite:///./pipeline-server.db"
    parquet_root: Path = Path("./data")

    # 策略
    plugins_dir: Path = Path("./plugins")
    strategies_file: Path = Path("strategies.yaml")

    # 日志
    log_level: str = "INFO"
    log_json: bool = False

    # 交易成本（A 股标准；和 plugins/v20h/config.yaml 保持一致）
    # settlement 写虚拟账本时按此扣费，避免账本相对真账户高估 cash
    stock_commission_rate: float = 0.0003   # 万三佣金（双边）
    stock_min_commission: float = 5.0       # 单笔最低 5 元
    stock_stamp_duty_sell: float = 0.0005   # 千五印花税（仅卖出收）

    # 调度：APScheduler 每交易日 cron 触发策略管线。
    # 生产部署须显式 QMT_SCHEDULER_ENABLED=true（默认 false，避免测试/本地误触发）。
    scheduler_enabled: bool = False
    scheduler_cron_hour: int = 16
    scheduler_cron_minute: int = 0

    # 数据新鲜度护栏：最新行情比 trade_date 旧超过该天数时，管线跳过不下单。
    # 探针标的默认中证1000 指数（每日推送、必在）。
    max_data_staleness_days: int = 5
    data_freshness_probe_category: str = "indexes"
    data_freshness_probe_symbol: str = "000852.SH"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例，整个进程共用一份配置。"""
    return Settings()
