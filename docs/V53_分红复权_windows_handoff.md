# V53 分红复权数据 — Windows/QMT 端 Handoff

**目的**：产出并推送一张 ETF 分红表 `etf_divid.parquet`，让 server 端 v53 在**算波动/权重**时用后复权（总回报）价，消除除息日「假跌」对 inv_vol 的污染（会系统性低配债券/红利，见 `docs/research` 里 2026-06-21 数据偏差报告 §1）。

**分工**：
- Server 端（已完成，见下）：消费这张表，只在建模处复权；成交/盯市仍用不复权原始价。
- **Windows/QMT 端（本 handoff 你要做的）**：用 QMT `get_divid_factors` 拉除息数据 → 生成 `etf_divid.parquet` → **本地自测通过** → 上传 server。

---

## 0. Server 端已经落好的契约（你只需按这个格式产出）

- 文件名**必须**是 `etf_divid.parquet`，上传 `strategy=v53`。
- 列**必须**是三列，名字精确一致：

  | 列 | 类型 | 含义 |
  |---|---|---|
  | `code` | str | QMT 代码含后缀，如 `511260.SH` |
  | `ex_date` | 日期（`YYYY-MM-DD` 字符串或 date 均可，server 会 `to_datetime`） | 除息日 |
  | `cash` | float | **每股**现金分红（元），要等于该日账面价格跌幅那笔钱 |

- **安全兜底**：这张表没上传之前，server 的复权是恒等变换，v53 行为和现在逐值一致——所以你没推之前不会误改仓。一旦推上，**下一个月首交易日就按新权重实调仓**（债券权重会抬约 +25%），所以先自测、再联测、最后 go-live（见 §4）。
- 无分红的 ETF 不用出现在表里；多出来也无害（server 只对有分红记录的列做还原）。

---

## 1. 生成脚本 `build_etf_divid.py`（Windows QMT venv 跑）

> 遵守项目铁律：顶层设 `xtdata.data_dir`；只用 `download_history_data`（不带 2）；download 后 `sleep(1)`；不碰 `sys.path`；开头 `startup_check()`。

```python
"""build_etf_divid.py — 从 QMT 拉 10 只 v53 ETF 的除息现金，产出 etf_divid.parquet。

跑法：激活 qmt venv 后 `python build_etf_divid.py`
输出：当前目录 etf_divid.parquet（三列 code / ex_date / cash）
"""
import time
import pandas as pd
from xtquant import xtdata

# 铁律 1：任何 xtquant 调用前先设 data_dir
xtdata.data_dir = r"C:\parttime\平安证券量盈QMT策略交易平台\userdata_mini"

# v53 的 10 只 ETF（重点分红的是 511260 债 / 510300 沪深300 / 512890 红利 / 159915 创业板）
CODES = [
    "510300.SH", "159915.SZ", "511260.SH", "518880.SH", "159981.SZ",
    "159985.SZ", "159930.SZ", "513500.SH", "513100.SH", "512890.SH",
]
START, END = "20180101", "20261231"   # 拉全历史；END 给未来端点无害，取到最新为止


def startup_check():
    assert xtdata.data_dir, "xtdata.data_dir 未设置"
    cal = xtdata.get_trading_dates("SH", start_time="20240101", end_time="20240110")
    assert cal, "取交易日历失败，QMT 客户端没连上？"
    print("[startup] OK, data_dir =", xtdata.data_dir)


def build():
    # 铁律 2/3：先 download_history_data，再 sleep(1)，再读
    for code in CODES:
        xtdata.download_history_data(code, period="1d", start_time=START, end_time=END)
    time.sleep(1)

    rows = []
    for code in CODES:
        fac = xtdata.get_divid_factors(code, START, END)
        if fac is None or len(fac) == 0:
            print(f"  {code}: 无分红记录")
            continue
        for dt, r in fac.iterrows():
            # ⚠️ 单位存疑：不同 QMT 版本 interest 可能是「每股」或「每10股」。
            #    不要盲信 —— 用 §2 的锚点自测校准（511260 的 3 个已知值）。
            cash = float(r.get("interest", 0.0))
            if cash <= 0:
                continue
            ex_date = pd.to_datetime(str(dt), format="%Y%m%d").strftime("%Y-%m-%d")
            rows.append({"code": code, "ex_date": ex_date, "cash": cash})
            print(f"  {code} {ex_date} cash={cash}")

    df = pd.DataFrame(rows, columns=["code", "ex_date", "cash"])
    df.to_parquet("etf_divid.parquet", index=False)
    print(f"\n写出 {len(df)} 行 → etf_divid.parquet")
    return df


if __name__ == "__main__":
    startup_check()
    build()
```

若 `get_divid_factors` 返回空：先确认 `download_history_data` 那步没报错、QMT 已登录；个别版本除息数据要单独补下（可再跑一次 download 后 `sleep(1)`）。

---

## 2. 验收自测 `validate_etf_divid.py`（**这是关卡，必须过**）

两道校验，任一不过都别上传：

1. **已知锚点**：511260 三笔除息（来自 2026-06-21 偏差报告 + yfinance 复权实测）必须对上，容差 ±10%。**若你的值恰好是锚点的 ~10 倍 → interest 是「每10股」口径，全表 `cash /= 10` 后重跑。**
2. **价格跌幅交叉核对**：每个除息日的 `cash` 应≈ 该 ETF 除息日相对前一交易日的**原始 close 跌幅那笔钱**（市场本身也会动，容差放宽到每股跌幅的 ±50% 只为抓数量级/单位错误，不是抓精度）。

```python
"""validate_etf_divid.py — 上传前自测。全过才 upload。"""
import time
import pandas as pd
from xtquant import xtdata

xtdata.data_dir = r"C:\parttime\平安证券量盈QMT策略交易平台\userdata_mini"

# 锚点：511260 近一年三笔（报告 §1.2，yfinance get_stock_actions 实测）
ANCHORS = {
    ("511260.SH", "2025-09-23"): 1.36,
    ("511260.SH", "2025-12-26"): 0.833,
    ("511260.SH", "2026-03-25"): 0.6711,
}


def check_anchors(df):
    ok = True
    for (code, ex), expect in ANCHORS.items():
        hit = df[(df["code"] == code) & (df["ex_date"] == ex)]
        if hit.empty:
            print(f"  ✗ 缺锚点 {code} {ex}（期望 {expect}）"); ok = False; continue
        got = float(hit.iloc[0]["cash"])
        ratio = got / expect
        flag = "OK" if 0.9 <= ratio <= 1.1 else ("疑似×10 单位错" if 8 <= ratio <= 12 else "✗ 偏差过大")
        print(f"  {code} {ex}: got={got} expect={expect} ratio={ratio:.2f} [{flag}]")
        if not (0.9 <= ratio <= 1.1):
            ok = False
    return ok


def check_price_drop(df):
    ok = True
    for code in df["code"].unique():
        xtdata.download_history_data(code, period="1d", start_time="20180101", end_time="20261231")
    time.sleep(1)
    for _, row in df.iterrows():
        code, ex, cash = row["code"], row["ex_date"], float(row["cash"])
        d = xtdata.get_market_data(
            ["close"], [code], period="1d",
            start_time="20180101", end_time="20261231")["close"].loc[code]
        d.index = pd.to_datetime(d.index.astype(str), format="%Y%m%d")
        exts = pd.Timestamp(ex)
        prior = d[d.index < exts]
        if prior.empty or exts not in d.index:
            print(f"  ? {code} {ex}: 缺价格，跳过"); continue
        drop = float(prior.iloc[-1]) - float(d.loc[exts])
        # 只抓数量级/单位错误：cash 与跌幅同数量级即可
        if drop <= 0:
            print(f"  ? {code} {ex}: 除息日未跌(drop={drop:.3f})，cash={cash} 存疑")
            continue
        ratio = cash / drop
        flag = "OK" if 0.5 <= ratio <= 2.0 else "✗ 数量级不符"
        print(f"  {code} {ex}: cash={cash} vs 账面跌={drop:.3f} ratio={ratio:.2f} [{flag}]")
        if not (0.3 <= ratio <= 3.0):
            ok = False
    return ok


if __name__ == "__main__":
    df = pd.read_parquet("etf_divid.parquet")
    df["ex_date"] = pd.to_datetime(df["ex_date"]).dt.strftime("%Y-%m-%d")
    print("== 锚点校验 =="); a = check_anchors(df)
    print("== 价格跌幅交叉核对 =="); b = check_price_drop(df)
    print("\n结果:", "PASS ✅ 可上传" if (a and b) else "FAIL ❌ 别上传，先修")
```

---

## 3. 上传到 server

自测 PASS 后：

```bash
curl -X POST -H "Authorization: Bearer $QMT_API_KEY" \
  -F "file=@etf_divid.parquet" \
  "http://<server>:8000/admin/upload-data?strategy=v53&filename=etf_divid.parquet"
```

确认落地：

```bash
curl -s -H "Authorization: Bearer $QMT_API_KEY" \
  "http://<server>:8000/admin/data-status?strategy=v53"
# 期望看到 etf_divid.parquet exists=true, bytes>0, mtime=刚才
```

（`<server>` / `$QMT_API_KEY` 同 `docs/V53_建仓手册_windows端.md` 里建仓用的那套。）

---

## 4. 联测 + go-live（server 端 owner 配合，别一步到位）

推上表后先别让它直接实盘调仓：

1. server `plugins/v53/config.yaml` 临时 `dry_run: true`。
2. 对一个「月首交易日」触发一次管线（dry-run 只 log 不下单）：
   ```bash
   curl -X POST -H "Authorization: Bearer $QMT_API_KEY" \
     "http://<server>:8000/admin/run-pipeline?trade_date=<某月首交易日YYYYMMDD>"
   ```
3. 看 server 日志里 `V53[...] DRY-RUN ... target_qty=...`：确认 **511260 债券权重较之前上升**（这正是复权修复的预期方向），其余合理。
4. 和同伴 V48 回测用**同一张 `etf_divid.parquet`**对齐口径（避免你实盘复权、他回测不复权，再开一条缝）。
5. 确认无误 → config 翻回 `dry_run: false`。下一个月首交易日自动按新权重调仓。

---

## 附：常见坑

- **单位 ×10**：QMT `interest` 有的版本是每 10 股口径。锚点校验就是抓这个——比值 ≈10 就全表 `/10`。
- **除息日非交易日**：server 端按交易日 index 对齐分红，非交易日的除息日会被忽略。正常 A 股 ETF 除息日都是交易日，一般不触发；若锚点日对不上先查这里。
- **税前/税后**：要的是「让账面价格掉下去的那笔钱」（≈ 账面跌幅）。ETF 分红一般在基金层不预扣，`interest` 通常就等于账面跌幅，用 §2 第二道校验确认即可。
- **只更新增量也行**：`etf_divid.parquet` 是全量覆盖上传。以后新增一笔除息，重跑 `build` 出全量再传即可，不用做增量拼接。

---

## Server 端本次改了什么（供你了解，不用动）

- `plugins/v53_adapter.py`：新增 `_to_total_return()`（分红加回→总回报），`run()` 在 `compute_targets` 前复权建模价；`_load_resources` 加载 `etf_divid.parquet`（缺失则空表→不复权）；`data_files` 白名单加入 `etf_divid.parquet`。
- 成交/盯市路径（`_resolve_reference_price` / NAV / 股数 / 下单价）**未改**，仍走不复权原始 close。
- 新增单测覆盖：除息假跌被还原、无分红列不变、空表退化、执行价不受复权影响。全绿。
</content>
</invoke>
