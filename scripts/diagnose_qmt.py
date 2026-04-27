"""QMT 拉数据全链路诊断脚本。

在 Windows + QMT 客户端登录后跑一次，按层级独立测试：
  Step 1. import xtquant
  Step 2. 设置 xtdata.data_dir
  Step 3. 取交易日历
  Step 4. 取板块成分
  Step 5. 单只股票 download_history_data
  Step 6. get_market_data 读出真实价格

任何一步挂掉都会立即停下并给修复建议。

跑法（Windows PowerShell）:
    cd C:\\parttime\\qmt模拟盘pipeline\\server
    C:\\parttime\\qmt数据推送\\venv\\Scripts\\activate
    python scripts/diagnose_qmt.py ^
        --data-dir "C:/parttime/平安证券量盈QMT策略交易平台/userdata_mini"

可选参数:
    --symbol 600519.SH   # 测试单只股票（默认茅台）
    --date 20260423      # 测试日期；不传则自动取最近交易日
    --sector "沪深A股"   # 板块名（不传指数代码！）
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback


def _step(n: int, name: str) -> None:
    print(f"\n=== [Step {n}] {name} ===", flush=True)


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}", flush=True)


def _fail(reason: str, hint: str | None = None, exc: BaseException | None = None) -> None:
    print(f"  [FAIL] {reason}", flush=True)
    if exc is not None:
        print("  --- traceback ---")
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        print("  -----------------")
    if hint:
        print(f"  >> 修复建议: {hint}", flush=True)
    print("\n诊断中止。请修复上面问题后重跑此脚本。", flush=True)
    sys.exit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", required=True,
                   help="QMT 客户端的 userdata_mini 路径")
    p.add_argument("--symbol", default="600519.SH",
                   help="单只股票测试用，默认 600519.SH（茅台）")
    p.add_argument("--date", default=None,
                   help="测试日期 YYYYMMDD；不传则自动取最近交易日")
    p.add_argument("--sector", default="沪深A股",
                   help="板块名（字符串，不是指数代码！）")
    args = p.parse_args()

    print("=" * 60)
    print(" QMT 数据拉取诊断")
    print("=" * 60)
    print(f"data_dir = {args.data_dir}")
    print(f"symbol   = {args.symbol}")
    print(f"sector   = {args.sector}")

    # ============ Step 1: import xtquant ============
    _step(1, "import xtquant")
    try:
        from xtquant import xtdata  # noqa: F401  (后面会用)
    except ImportError as e:
        _fail(
            f"xtquant 不可 import: {e}",
            "确认 venv 已激活：C:\\parttime\\qmt数据推送\\venv\\Scripts\\activate；"
            "该 venv 的 sitecustomize.py 应该把 xtquant 路径加到 sys.path",
            exc=e,
        )
    except Exception as e:
        _fail(f"import 时其他异常: {e}", exc=e)
    _ok(f"xtquant.xtdata 模块来自 {xtdata.__file__}")

    # ============ Step 2: 设置 data_dir ============
    _step(2, "设置 xtdata.data_dir")
    try:
        xtdata.data_dir = args.data_dir
    except Exception as e:
        _fail(f"赋值失败: {e}", "确认路径存在且当前用户可读", exc=e)
    _ok(f"xtdata.data_dir = {xtdata.data_dir}")

    # ============ Step 3: 交易日历 ============
    _step(3, 'get_trading_dates("SH", count=5)')
    try:
        dates = xtdata.get_trading_dates("SH", count=5)
    except Exception as e:
        _fail(
            f"调用异常: {e}",
            "QMT 客户端必须正在运行并已登录；data_dir 必须正确指向客户端目录",
            exc=e,
        )
    if not dates:
        _fail(
            "返回空列表",
            "QMT 客户端没登录 / data_dir 路径错 / 客户端没有交易日历缓存",
        )
    sample_type = type(dates[0]).__name__
    _ok(f"取到 {len(dates)} 个交易日，元素类型 {sample_type}，样例 {dates[:3]}")

    # 标准化为 YYYYMMDD 字符串供后续比较
    as_str = [str(d)[:8] if not isinstance(d, str) else d for d in dates]
    print(f"  规整后样例: {as_str[:3]}")

    # 选定测试日期
    if args.date:
        test_date = args.date
        if test_date not in as_str:
            _fail(
                f"--date {test_date} 不在 xtquant 返回的近 5 个交易日里 "
                f"({as_str})",
                "改用最近一个交易日，或扩大 count 后重试",
            )
    else:
        test_date = as_str[-1]
    print(f"  将使用 test_date = {test_date}")

    # ============ Step 4: 板块成分 ============
    _step(4, f'get_stock_list_in_sector("{args.sector}")')
    try:
        symbols = xtdata.get_stock_list_in_sector(args.sector)
    except Exception as e:
        _fail(f"调用异常: {e}", exc=e)
    if not symbols:
        _fail(
            f'板块 "{args.sector}" 返回空',
            '板块名必须传字符串（如 "沪深A股"、"上证A股"），不能传指数代码（如 "000001.SH"）',
        )
    _ok(f"板块 {args.sector} 共 {len(symbols)} 只，样例 {symbols[:3]}")

    if args.symbol not in symbols:
        print(f"  [WARN] 测试 symbol {args.symbol} 不在该板块成分内，仍继续测下载")

    # ============ Step 5: 单只 download ============
    _step(5, f'download_history_data("{args.symbol}", period="1d", '
              f"start={test_date}, end={test_date})")
    try:
        ret = xtdata.download_history_data(
            args.symbol,
            period="1d",
            start_time=test_date,
            end_time=test_date,
        )
    except TypeError as e:
        _fail(
            f"参数名错误: {e}",
            "xtquant 该版本 download_history_data 签名不同；"
            "试一下 start_date/end_date，或不带 kwargs 改用位置参数。"
            "对照 C:\\parttime\\qmt数据推送\\v3\\ 里 v3 项目实测过的写法",
            exc=e,
        )
    except Exception as e:
        _fail(f"调用异常: {e}", exc=e)
    _ok(f"download_history_data 返回: {ret!r}")

    # v3 历史教训
    print("  sleep(1) 等 QMT 缓存就绪...")
    time.sleep(1)

    # ============ Step 6: get_market_data 读数 ============
    _step(6, "get_market_data 读 OHLCV")
    fields = ["open", "high", "low", "close", "volume"]
    try:
        md = xtdata.get_market_data(
            fields,
            [args.symbol],
            period="1d",
            start_time=test_date,
            end_time=test_date,
        )
    except TypeError as e:
        _fail(
            f"参数名错误: {e}",
            "xtquant 该版本 get_market_data 签名不同；试 stock_list= / "
            "field_list= / period= 等不同写法",
            exc=e,
        )
    except Exception as e:
        _fail(f"调用异常: {e}", exc=e)

    if not isinstance(md, dict):
        _fail(
            f"返回类型不是 dict 而是 {type(md).__name__}",
            "我们模块一假定返回 dict；如果实际是 DataFrame 等，"
            "需要在 downloader.py 里调整解包逻辑",
        )

    print(f"  返回 dict 的 keys: {list(md.keys())}")
    if "close" not in md:
        _fail(
            f"缺少 'close' 字段",
            f"实际字段: {list(md.keys())}；可能字段命名不同，"
            "如 'closePrice'，需要在 downloader.py 的 _REQUIRED_FIELDS 里改",
        )

    close_df = md["close"]
    print(f"  close DataFrame 形状: {close_df.shape}")
    print(f"  close columns: {list(close_df.columns)[:5]}")
    print(f"  close index:   {list(close_df.index)[:3]}")

    # 取实际收盘价
    try:
        # md["close"] 通常是 DataFrame(index=symbol, columns=date)
        if test_date in close_df.columns:
            close_price = close_df.loc[args.symbol, test_date]
        elif args.symbol in close_df.index:
            close_price = close_df.loc[args.symbol].iloc[0]
        else:
            close_price = close_df.iloc[0, 0]
    except Exception as e:
        _fail(f"无法从 close DataFrame 取值: {e}",
              "DataFrame 形状与预期不符，看上面打印的 shape/columns/index", exc=e)

    if close_price is None or (isinstance(close_price, float) and close_price != close_price):
        # NaN 检测：x != x
        _fail(
            f"close 取出来是 NaN",
            "1) sleep 时长可能不够，调大重试；2) test_date 当天股票停牌；"
            "3) QMT 缓存里没有这天的数据，去 QMT 客户端图表里手动加载一遍",
        )

    _ok(f"{args.symbol} 在 {test_date} 的收盘价 = {close_price}")

    # ============ 全部通过 ============
    print("\n" + "=" * 60)
    print(" 全部诊断通过 ✅")
    print("=" * 60)
    print(f"\n请人工核对：{args.symbol} 在 {test_date} 的收盘价 = {close_price}")
    print("跟东方财富/同花顺当天的收盘对一下；一致则放心跑全市场。\n")

    print("下一步：跑模块一 CLI 拉全市场行情：")
    print(f"  python -m src.market_data_download --date {test_date} "
          f"--config config\\settings.yaml")
    print("\n（全市场约 5000 只串行下载，预计需要几分钟到十几分钟）\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
