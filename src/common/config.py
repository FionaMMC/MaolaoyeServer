"""YAML 配置加载与 dataclass 封装。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class QmtConfig:
    data_dir: str
    account_id: str


@dataclass(frozen=True)
class ServerConfig:
    base_url: str
    api_key: str
    timeout: int


@dataclass(frozen=True)
class PathsConfig:
    data_root: str
    log_dir: str
    sqlite_path: str


@dataclass(frozen=True)
class NotifyConfig:
    wecom_webhook: str


@dataclass(frozen=True)
class MarketDataConfig:
    sector_name: str


@dataclass(frozen=True)
class Config:
    qmt: QmtConfig
    server: ServerConfig
    paths: PathsConfig
    notify: NotifyConfig
    market_data: MarketDataConfig


def _require(d: dict[str, Any], key: str) -> Any:
    if key not in d:
        raise KeyError(f"settings.yaml 缺少必填字段: {key}")
    return d[key]


def load_config(path: Path | str) -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")

    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    qmt = _require(raw, "qmt")
    server = _require(raw, "server")
    paths = _require(raw, "paths")
    notify = _require(raw, "notify")
    md = _require(raw, "market_data")

    return Config(
        qmt=QmtConfig(
            data_dir=_require(qmt, "data_dir"),
            account_id=_require(qmt, "account_id"),
        ),
        server=ServerConfig(
            base_url=_require(server, "base_url"),
            api_key=_require(server, "api_key"),
            timeout=int(_require(server, "timeout")),
        ),
        paths=PathsConfig(
            data_root=_require(paths, "data_root"),
            log_dir=_require(paths, "log_dir"),
            sqlite_path=_require(paths, "sqlite_path"),
        ),
        notify=NotifyConfig(
            wecom_webhook=_require(notify, "wecom_webhook"),
        ),
        market_data=MarketDataConfig(
            sector_name=_require(md, "sector_name"),
        ),
    )
