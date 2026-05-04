"""structlog 配置：JSON（生产）或 ConsoleRenderer（开发）。"""
from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(log_level: str = "INFO", json_output: bool = False) -> None:
    """初始化全局 logging + structlog。可重复调用，会重置 handler。"""
    level = getattr(logging, log_level.upper())

    # 清空根 logger 的旧 handler
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    # 标准 logging → stdout
    handler = logging.StreamHandler(sys.stdout)
    root.addHandler(handler)
    root.setLevel(level)

    # structlog 处理器链
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,  # 测试需要每次重配
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
