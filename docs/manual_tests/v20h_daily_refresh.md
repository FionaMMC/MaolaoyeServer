# V20H Daily Refresh — 操作手册

每天傍晚把 V20H 5 个 .parquet 重训 + 推上云端 + 触发信号生成的自动化脚本。

> 脚本位置：`/Users/mameican/Desktop/量化/v20h_refresh.py` + `daily_v20h.sh`
> （在 `量化/` 仓库里，不在 server 仓库；server 仓库只放本文档）

---

## 0. 数据流图（完整 cycle）

```
                              ┌─ akshare ──────────┐
                              │ (个股 OHLCV 增量) │  ← 目前手工：偶尔删 cache 重拉
                              └──────────┬─────────┘
                                         ▼
                          v15/cache/stocks/<code>.parquet
                                         │
                ┌───────────────────────┼────────────────────────────┐
                ▼                        ▼                            ▼
         run_v18_final.py        run_v19_research.py         build_stock_close()
                │                        │                            │
                ▼                        ▼                            │
   v18/cache/pred_csi1000        v19/cache/stock_returns              │
   v18/cache/v12_exp_hs300                                            │
                                                                       │
                                                                       │
                          ┌────────────────────────────────────────────┘
                          ▼
                v20h_strategy/data/  ←  copy from caches
                          │
                          ▼
        v2.3/server/plugins/v20h/data/  ← copy locally for test
                          │
                          ▼
              POST /admin/upload-data x5
                          │
                          ▼
              POST /admin/run-pipeline?trade_date=T
                          │
                          ▼
                  673 orders 落库
```

---

## 1. 一次性准备

**venv 必须能 import 全部模块**：
```bash
cd /Users/mameican/Desktop/量化
.venv/bin/python -c "from v15.stock_data import fetch_batch; from walkforward_framework import run_v18_final; from walkforward_framework import run_v19_research; print('ok')"
```

如果报 `ModuleNotFoundError`，去补依赖。

**环境变量（可选，有 default）**：
```bash
export QMT_PIPELINE_BASE_URL=http://120.26.138.82:8000
export QMT_PIPELINE_API_KEY=pipeline-v23-shared-secret-2026
```

---

## 2. 用法速查

### 2.1 最常用：每日同步现有 cache → 云端
（不重训 ML，纯把磁盘上现成的 5 个文件推上去 + 触发 pipeline）

```bash
cd /Users/mameican/Desktop/量化
.venv/bin/python v20h_refresh.py
```

执行时间：~30 秒（主要是 47 MB 上传）。

### 2.2 重训 V18（每周一次或更新 V20H 数据时）

```bash
.venv/bin/python v20h_refresh.py --rebuild-pred
```

执行时间：~15 分钟（首次有缓存约 1 分钟）。

### 2.3 完整重训 + 同步 + 触发（每天傍晚）

```bash
bash daily_v20h.sh
```

或带日期：
```bash
bash daily_v20h.sh 20260430
```

日志写到 `logs/v20h_refresh/<date>.log`。

### 2.4 只测试，不上传

```bash
.venv/bin/python v20h_refresh.py --skip-upload
```

只把 5 个文件 stage 到 `v20h_strategy/data/` 和 `v2.3/server/plugins/v20h/data/`，不动云端。用来检查 stock_close 重建是否成功。

### 2.5 只上传，不触发 pipeline

```bash
.venv/bin/python v20h_refresh.py --no-trigger
```

适合需要手动控制触发时机的场景。

---

## 3. 完整参数表

| 参数 | 含义 | 用时 |
|---|---|---|
| `--target YYYYMMDD` | ML 算到这一天 + 触发这一天的 pipeline。默认今天 | — |
| `--rebuild-pred` | 跑 `run_v18_final.py` 重新算 pred + v12 | ~15 min（首次） / ~1 min（有缓存） |
| `--rebuild-pred-cache` | 加 `--rebuild`，强清 V18 cache 后再算 | ~30 min |
| `--rebuild-rets` | 跑 `run_v19_research.py` 重新算 stock_returns | ~1 min |
| `--no-trigger` | 上传完不触发 `/admin/run-pipeline` | — |
| `--skip-upload` | 只 stage 本地，不上传 | ~5 sec |

---

## 4. 已知限制（第二阶段再做）

### 4.1 akshare cache 不会自动增量更新

`v15/stock_data.py:fetch_stock_daily` 的逻辑是：**缓存文件存在就直接返回，end_date 参数被忽略**。

实际影响：单纯跑 `--rebuild-pred` 不会拉新数据，pred 算出来还是旧 end date。

**短期 workaround**：手工更新 cache。
```bash
# 全删个股 cache 强制重拉（akshare 1000 只股票, ~30 min）
rm -rf /Users/mameican/Desktop/量化/v15/cache/stocks/
.venv/bin/python -c "
from v15.stock_data import get_csi1000_components, fetch_batch
codes = get_csi1000_components()
fetch_batch(codes, start_date='20180101', end_date='$(date +%Y%m%d)', use_cache=True, delay=0.3)
"
```

或针对特定缺失日期：写个增量 fetcher（待办）。

### 4.2 hardcoded end_date 在 run_v18_final.py 第 571 行

脚本现在每次跑 `--rebuild-pred` 时会自动 sed-patch 这行 + 用完还原。如果脚本中途崩了原值没还原，需要手动检查：
```bash
grep "end_date=" /Users/mameican/Desktop/量化/walkforward_framework/run_v18_final.py | head -2
```

应该是 `end_date="20260410"`（原始硬编码值），不应该是其他日期。

---

## 5. 自动化调度

### macOS launchd（推荐）

写一个 plist：`~/Library/LaunchAgents/com.local.v20h_refresh.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.local.v20h_refresh</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/mameican/Desktop/量化/daily_v20h.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>17</integer>
        <key>Minute</key>
        <integer>30</integer>
        <key>Weekday</key>
        <integer>1</integer> <!-- 周一到周五 -->
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/v20h_refresh.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/v20h_refresh.err</string>
</dict>
</plist>
```

加载：
```bash
launchctl load ~/Library/LaunchAgents/com.local.v20h_refresh.plist
launchctl list | grep v20h
```

> ⚠️ launchd 的 Weekday 1=Mon … 5=Fri；要每天就删掉 Weekday key。

### 简化版：crontab

```bash
crontab -e
# 加这一行（周一到周五 17:30）
30 17 * * 1-5 bash /Users/mameican/Desktop/量化/daily_v20h.sh
```

---

## 6. 一次完整测试流程（搭档配合）

**场景**：刷 20260430 的信号，让搭档在 Windows 拿这天的订单做模拟盘联调。

| 步骤 | 谁 | 命令 | 预期 |
|---|---|---|---|
| ① 拉 4/30 之前 akshare 个股数据（如果 cache 不到 4/30） | 你（Mac） | `rm -rf v15/cache/stocks/ && .venv/bin/python -c "..."` | ~30 min |
| ② 重训 V18 + 同步 + 触发 | 你（Mac） | `bash daily_v20h.sh 20260430` | ~16 min |
| ③ 验证云端有 4/30 信号 | 你（Mac） | `curl /orders?date=20260430` | 返回 ~673 单 |
| ④ Windows 拉信号 + 模拟盘下单 | 搭档（Windows） | 跟 [v20h_paper_trading_runbook.md](v20h_paper_trading_runbook.md) | — |

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `缺失上游缓存文件 .../pred_csi1000.parquet` | V18 没跑 | 加 `--rebuild-pred` |
| `缺失上游缓存文件 .../stock_returns.parquet` | V19 没跑 | 加 `--rebuild-rets` |
| `v15 stocks cache 为空` | akshare 没拉过 | 见 4.1 节 |
| `upload xxx 失败` | 网络 / API_KEY 错 | 检查 `QMT_PIPELINE_BASE_URL`、`QMT_PIPELINE_API_KEY` |
| `trigger 失败` | server 没起 / 路由没装 | curl `/healthz` 看是否 200 |
| pipeline 返回 `signals=0` | pred 没覆盖到 trade_date / OHLCV 不够 | 看 [v20h_paper_trading_runbook.md](v20h_paper_trading_runbook.md) 的 0 信号排查 |
| `run_v18_final.py` 未找到 end_date 常量 | 上游脚本被改过 | 手动检查第 571 行 |
