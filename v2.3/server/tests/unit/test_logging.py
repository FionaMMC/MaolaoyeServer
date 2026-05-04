"""tests/unit/test_logging.py"""
import io
import json
import logging

import pytest
import structlog

from app.logging_setup import get_logger, setup_logging


def test_setup_logging_json_mode(capsys):
    setup_logging(log_level="INFO", json_output=True)
    log = get_logger("test_module")
    log.info("hello", trade_date="20260430", count=3)
    captured = capsys.readouterr()
    # JSON 模式下每行应该是合法 JSON
    line = captured.out.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["event"] == "hello"
    assert parsed["trade_date"] == "20260430"
    assert parsed["count"] == 3
    assert parsed["logger"] == "test_module"


def test_setup_logging_console_mode(capsys):
    setup_logging(log_level="INFO", json_output=False)
    log = get_logger("test_console")
    log.info("greeting", name="alice")
    captured = capsys.readouterr()
    out = captured.out
    assert "greeting" in out
    assert "alice" in out


def test_log_level_filtering(capsys):
    setup_logging(log_level="WARNING", json_output=True)
    log = get_logger("test_level")
    log.info("filtered_out")
    log.warning("kept")
    captured = capsys.readouterr()
    out = captured.out
    assert "filtered_out" not in out
    assert "kept" in out
