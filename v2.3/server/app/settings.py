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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例，整个进程共用一份配置。"""
    return Settings()
