"""内联的"下一交易日"计算，仅此处调用 xtquant.get_trading_dates。"""
from __future__ import annotations


def next_trading_day(today: str) -> str:
    """根据 xtquant 日历返回 today 之后的第一个交易日（YYYYMMDD）。

    Raises:
        ValueError: today 不在 xtquant 返回的交易日列表中
        RuntimeError: today 已是列表末尾，无下一个
    """
    from xtquant import xtdata

    dates = xtdata.get_trading_dates("SH", count=30)
    as_str = [str(d)[:8] if not isinstance(d, str) else d for d in dates]

    if today not in as_str:
        raise ValueError(f"today={today} 不在近期交易日列表（非交易日或超出 30 天窗口）")

    idx = as_str.index(today)
    if idx + 1 >= len(as_str):
        raise RuntimeError(
            f"today={today} 已是 xtquant 日历末尾，无下一交易日（请扩大 count）"
        )
    return as_str[idx + 1]
