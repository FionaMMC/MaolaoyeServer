"""Server 端配置：环境变量 QMT_* + .env 文件。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
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
    # api_key 是兼容现有模拟盘部署的 legacy paper token；新部署优先使用
    # paper_api_key / live_api_key 两把互不相同的 key。
    api_key: str = ""
    paper_api_key: str = ""
    live_api_key: str = ""
    paper_client_id: str = "legacy-paper-client"
    live_client_id: str = ""
    paper_account_aliases_csv: str = ""
    live_account_aliases_csv: str = ""
    host: str = "0.0.0.0"
    port: int = 8000

    # 双重闸门：即使 live token 已配置，也不代表允许生成或领取实盘订单。
    # 开启实盘时必须由部署环境显式设置，代码库默认永远 fail-closed。
    live_order_generation_enabled: bool = False
    live_order_delivery_enabled: bool = False
    live_cash_flow_ingest_enabled: bool = False
    live_account_initialization_enabled: bool = False
    live_qmt_account_sha256: str = ""

    # Hydra live relay allowlists / limits。0 或空值代表“未获业务批准”，live fail-closed。
    hydra_allowed_symbols_csv: str = (
        "510300.SH,159915.SZ,511260.SH,518880.SH,159981.SZ,"
        "159985.SZ,159930.SZ,513500.SH,513100.SH"
    )
    hydra_allowed_publisher_commits_csv: str = ""
    live_max_daily_orders: int = 0
    live_max_single_order_notional: float = 0.0
    live_max_daily_buy_notional: float = 0.0
    live_max_daily_sell_notional: float = 0.0
    live_max_daily_turnover_notional: float = 0.0
    live_max_price_offset_bps: float = 0.0

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

    @model_validator(mode="after")
    def validate_execution_domain_secrets(self) -> "Settings":
        """禁止 paper/live 共用凭据，避免客户端误连后静默跨域。"""
        paper_key = self.paper_api_key or self.api_key
        if paper_key and self.live_api_key and paper_key == self.live_api_key:
            raise ValueError("paper_api_key 与 live_api_key 必须不同")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例，整个进程共用一份配置。"""
    return Settings()
