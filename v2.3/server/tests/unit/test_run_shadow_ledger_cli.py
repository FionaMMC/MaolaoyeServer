from pathlib import Path

import pytest

from app.settings import Settings
from scripts.run_shadow_ledger import exit_code, startup_check


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="test",
        db_url=f"sqlite:///{tmp_path}/test.db",
        parquet_root=tmp_path / "market",
        plugins_dir=tmp_path / "plugins",
        strategies_file=tmp_path / "strategies.yaml",
    )


def test_startup_check_requires_config_and_market_data(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(FileNotFoundError, match="strategy config"):
        startup_check(settings, 20260725)

    Path(settings.strategies_file).write_text(
        "shadow_instances: []\n", encoding="utf-8"
    )
    with pytest.raises(FileNotFoundError, match="market data root"):
        startup_check(settings, 20260725)

    Path(settings.parquet_root).mkdir()
    startup_check(settings, 20260725)


def test_shadow_cli_exit_code_fails_only_for_blocked_instances():
    assert exit_code({"instances": [
        {"status": "active"}, {"status": "disabled"},
    ]}) == 0
    assert exit_code({"instances": [
        {"status": "active"}, {"status": "blocked"},
    ]}) == 1
