"""统一日志配置：控制台 + 按日期文件。"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(
    log_dir: Path | str,
    module_name: str,
    level: str = "INFO",
) -> logging.Logger:
    """为指定 module_name 配置日志 handler。幂等。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(module_name)
    logger.setLevel(getattr(logging, level.upper()))

    if logger.handlers:
        return logger

    today = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"{module_name}-{today}.log"

    file_h = logging.FileHandler(log_file, encoding="utf-8")
    file_h.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(file_h)

    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(stream_h)

    logger.propagate = False
    return logger
