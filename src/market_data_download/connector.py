"""xtquant 连接初始化与启动检查。

严格遵守 v3 历史教训：
1. xtdata.data_dir 必须在任何 xtquant 调用之前设置
2. 不手动操作 sys.path（依赖 venv 的 sitecustomize.py 提供 xtquant）
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def init_xtquant(data_dir: str) -> None:
    """设置 xtquant.xtdata.data_dir。必须在任何其他 xtquant 调用之前执行。"""
    if not data_dir:
        raise ValueError("data_dir 不能为空 — 请在 settings.yaml 中配置 qmt.data_dir")

    # 延迟 import：Mac 上测试时 xtquant 由 monkeypatch 注入，生产时由 Windows venv 提供
    from xtquant import xtdata

    xtdata.data_dir = data_dir
    logger.info("xtdata.data_dir 已设置为 %s", data_dir)


def startup_check(data_dir: str) -> None:
    """启动前检查：data_dir 已设置 + QMT 可访问交易日历。

    失败抛异常，调用方负责报警或退出。
    """
    init_xtquant(data_dir)

    from xtquant import xtdata

    dates = xtdata.get_trading_dates("SH", count=5)
    if not dates:
        raise RuntimeError(
            "QMT 连接失败或 data_dir 无数据：get_trading_dates 返回空。"
            "请确认 QMT 客户端已登录且 data_dir 路径正确。"
        )

    logger.info("startup_check 通过，最近 5 个交易日：%s", dates)
