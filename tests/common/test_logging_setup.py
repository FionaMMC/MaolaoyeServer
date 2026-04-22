"""src.common.logging_setup 测试"""
from __future__ import annotations

import logging
from pathlib import Path

from src.common.logging_setup import setup_logging


def test_setup_logging_creates_log_dir_and_file(tmp_path: Path):
    log_dir = tmp_path / "logs"

    logger = setup_logging(log_dir=log_dir, module_name="test_module", level="INFO")
    logger.info("hello from test")

    for h in logger.handlers:
        h.flush()

    assert log_dir.exists()
    log_files = list(log_dir.glob("test_module-*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "hello from test" in content


def test_setup_logging_idempotent(tmp_path: Path):
    """重复调用不应重复添加 handler。"""
    log_dir = tmp_path / "logs"
    setup_logging(log_dir=log_dir, module_name="m2", level="INFO")
    setup_logging(log_dir=log_dir, module_name="m2", level="INFO")

    logger = logging.getLogger("m2")
    assert len(logger.handlers) == 2  # file + console
