"""Hydra live client 私有环境配置与 fail-closed 启动检查。"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false")


def _positive_int(name: str) -> int:
    value = int(os.environ.get(name, "0"))
    if value <= 0:
        raise ValueError(f"{name} 必须为正数")
    return value


def _positive_float(name: str) -> float:
    value = float(os.environ.get(name, "0"))
    if value <= 0:
        raise ValueError(f"{name} 必须为正数")
    return value


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"缺少环境变量 {name}")
    return value


@dataclass(frozen=True)
class LiveClientConfig:
    mode: str
    execution_domain: str
    account_id: str
    expected_account_sha256: str
    account_alias: str
    instance_id: str
    api_key: str
    server_base_url: str
    userdata_dir: Path
    session_id: int
    state_db: Path
    log_dir: Path
    task_prefix: str
    trading_enabled: bool
    allow_insecure_http: bool
    allowed_symbols: frozenset[str]
    max_daily_orders: int
    max_single_order_notional: float
    max_daily_buy_notional: float
    max_daily_sell_notional: float
    max_daily_turnover_notional: float
    max_price_offset_bps: float

    @classmethod
    def from_env(cls) -> "LiveClientConfig":
        cfg = cls(
            mode=os.environ.get("HYDRA_LIVE_MODE", "mock_qmt").strip().lower(),
            execution_domain=_required("HYDRA_LIVE_EXECUTION_DOMAIN"),
            account_id=_required("HYDRA_LIVE_QMT_ACCOUNT_ID"),
            expected_account_sha256=_required("HYDRA_LIVE_EXPECTED_ACCOUNT_SHA256"),
            account_alias=_required("HYDRA_LIVE_ACCOUNT_ALIAS"),
            instance_id=_required("HYDRA_LIVE_INSTANCE_ID"),
            api_key=_required("HYDRA_LIVE_API_KEY"),
            server_base_url=_required("HYDRA_LIVE_SERVER_BASE_URL").rstrip("/"),
            userdata_dir=Path(_required("HYDRA_LIVE_QMT_USERDATA_DIR")),
            session_id=_positive_int("HYDRA_LIVE_QMT_SESSION_ID"),
            state_db=Path(_required("HYDRA_LIVE_STATE_DB")),
            log_dir=Path(_required("HYDRA_LIVE_LOG_DIR")),
            task_prefix=_required("HYDRA_LIVE_TASK_PREFIX"),
            trading_enabled=_bool("HYDRA_LIVE_TRADING_ENABLED"),
            allow_insecure_http=_bool("HYDRA_LIVE_ALLOW_INSECURE_HTTP"),
            allowed_symbols=frozenset(
                value.strip()
                for value in _required("HYDRA_LIVE_ALLOWED_SYMBOLS").split(",")
                if value.strip()
            ),
            max_daily_orders=_positive_int("HYDRA_LIVE_MAX_DAILY_ORDERS"),
            max_single_order_notional=_positive_float(
                "HYDRA_LIVE_MAX_SINGLE_ORDER_NOTIONAL"
            ),
            max_daily_buy_notional=_positive_float(
                "HYDRA_LIVE_MAX_DAILY_BUY_NOTIONAL"
            ),
            max_daily_sell_notional=_positive_float(
                "HYDRA_LIVE_MAX_DAILY_SELL_NOTIONAL"
            ),
            max_daily_turnover_notional=_positive_float(
                "HYDRA_LIVE_MAX_DAILY_TURNOVER_NOTIONAL"
            ),
            max_price_offset_bps=_positive_float(
                "HYDRA_LIVE_MAX_PRICE_OFFSET_BPS"
            ),
        )
        cfg.validate_startup()
        return cfg

    def validate_startup(self) -> None:
        if self.mode not in {"mock_qmt", "live"}:
            raise ValueError("HYDRA_LIVE_MODE 必须是 mock_qmt/live")
        if self.execution_domain != "live":
            raise ValueError("live client execution_domain 必须固定为 live")
        account_hash = hashlib.sha256(self.account_id.encode()).hexdigest()
        if account_hash != self.expected_account_sha256:
            raise ValueError("QMT account fingerprint 不匹配")
        paper_ids = {
            value.strip()
            for value in os.environ.get("HYDRA_PAPER_QMT_ACCOUNT_IDS", "").split(",")
            if value.strip()
        }
        if self.mode == "live" and not paper_ids:
            raise ValueError("实盘模式必须显式配置 paper account denylist")
        if self.account_id in paper_ids:
            raise ValueError("live account 出现在 paper account denylist")
        if not self.allowed_symbols:
            raise ValueError("Hydra ETF 白名单不能为空")
        if self.max_price_offset_bps > 50:
            raise ValueError("client 价格偏移上限不能超过 50bps")
        if self.state_db.resolve() == (self.log_dir / "pipeline.db").resolve():
            raise ValueError("live SQLite 与日志目录配置异常")
        paper_paths = {
            Path(value.strip()).resolve()
            for value in os.environ.get("HYDRA_PAPER_WRITABLE_PATHS", "").split(os.pathsep)
            if value.strip()
        }
        if self.mode == "live" and not paper_paths:
            raise ValueError("实盘模式必须显式配置 paper writable-path denylist")
        live_paths = {
            self.userdata_dir.resolve(), self.state_db.resolve(), self.log_dir.resolve(),
        }
        if live_paths & paper_paths:
            raise ValueError("live client 可写路径与 paper client 重叠")
        paper_sessions = {
            int(value.strip())
            for value in os.environ.get("HYDRA_PAPER_QMT_SESSION_IDS", "").split(",")
            if value.strip()
        }
        if self.mode == "live" and not paper_sessions:
            raise ValueError("实盘模式必须显式配置 paper session denylist")
        if self.session_id in paper_sessions:
            raise ValueError("live QMT session_id 与 paper client 冲突")
        paper_task_prefixes = {
            value.strip()
            for value in os.environ.get("HYDRA_PAPER_TASK_PREFIXES", "").split(",")
            if value.strip()
        }
        if self.mode == "live" and not paper_task_prefixes:
            raise ValueError("实盘模式必须显式配置 paper task-prefix denylist")
        if self.task_prefix in paper_task_prefixes:
            raise ValueError("live Windows task prefix 与 paper client 冲突")
        parsed = urlparse(self.server_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("HYDRA_LIVE_SERVER_BASE_URL 非法")
        if self.mode == "live" and parsed.scheme != "https" and not self.allow_insecure_http:
            raise ValueError("实盘模式默认要求 HTTPS")
        if self.mode == "live" and not self.userdata_dir.is_dir():
            raise ValueError("实盘 QMT userdata 目录不存在")

    def require_submission_enabled(self) -> None:
        if not self.trading_enabled:
            raise RuntimeError("HYDRA_LIVE_TRADING_ENABLED=false，客户端紧急开关关闭")
