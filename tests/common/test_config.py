"""src.common.config 测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.config import Config, load_config


def test_load_config_reads_yaml(tmp_config_dir: Path):
    cfg_file = tmp_config_dir / "settings.yaml"
    cfg_file.write_text(
        """
qmt:
  data_dir: "/tmp/fake_qmt"
  account_id: "ACC123"
server:
  base_url: "https://api.example.com"
  api_key: "KEY123"
  timeout: 15
paths:
  data_root: "/tmp/data"
  log_dir: "/tmp/logs"
  sqlite_path: "/tmp/data/trading.db"
notify:
  wecom_webhook: "https://webhook.example.com"
market_data:
  sector_name: "沪深A股"
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_file)

    assert isinstance(cfg, Config)
    assert cfg.qmt.data_dir == "/tmp/fake_qmt"
    assert cfg.qmt.account_id == "ACC123"
    assert cfg.server.base_url == "https://api.example.com"
    assert cfg.server.timeout == 15
    assert cfg.paths.data_root == "/tmp/data"
    assert cfg.market_data.sector_name == "沪深A股"


def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_load_config_missing_required_key_raises(tmp_config_dir: Path):
    cfg_file = tmp_config_dir / "settings.yaml"
    cfg_file.write_text(
        """
qmt:
  account_id: "ACC"
server:
  base_url: "x"
  api_key: "y"
  timeout: 10
paths:
  data_root: "."
  log_dir: "."
  sqlite_path: "."
notify:
  wecom_webhook: "w"
market_data:
  sector_name: "沪深A股"
""",
        encoding="utf-8",
    )
    with pytest.raises(KeyError):
        load_config(cfg_file)
