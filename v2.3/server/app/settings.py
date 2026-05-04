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
    strategies_file: Path = Path("../strategies.yaml")

    # 日志
    log_level: str = "INFO"
    log_json: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例，整个进程共用一份配置。"""
    return Settings()
