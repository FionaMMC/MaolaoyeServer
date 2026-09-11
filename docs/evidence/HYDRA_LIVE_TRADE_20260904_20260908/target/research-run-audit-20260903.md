# Hydra 4.8 月度研究运行审计 — 202609 周期

运行时间：2026-09-03 18:35 ~ 19:25 (Asia/Shanghai)
操作者：AI agent（用户授权执行）
研究提交（锁定）：aa6b60deef44b244764385e7b6bd681429b9b362
as_of_date: 20260903 / execution_date: 20260904
最终状态：**PREPARED_NOT_STAGED**（未上传 server、未 stage、未开任何订单闸门）

## 与标准脚本的偏差（重要，供复核）

`Invoke-HydraMonthEndResearch.ps1` 在本机为 UTF-8 无 BOM 含中文脚本，PowerShell 5.1
按 ANSI 误读导致整体解析失败。补救：按 UTF-8 读出、加 BOM 写入
`%TEMP%\hydra-ps-scripts\` 后执行，字节内容不变，仓库未修改。

### 尝试记录

1. **尝试 1**（18:38）：直接执行原脚本 → PS 解析错误（编码问题），无任何语句执行、无副作用。
2. **尝试 2**（18:41，BOM 副本，ReleaseRoot=`month-end\202609` 初次）：
   - 四包冻结成功（ZIP sha256=6f35345d...）
   - `prepare-model-input` 失败：`159985.SZ HFQ 覆盖不完整`（model_hfq 停在 20260828，
     execution_raw 完整至 20260903 —— xtdata 后复权重算瞬时滞后）
   - 现场归档为 `month-end\202609.attempt1-failed-159985-hfq-lag`
3. **尝试 3**（18:5x，BOM 副本，全新 ReleaseRoot）：
   - 四包冻结成功（新 ZIP，snapshot manifest 见下）
   - `prepare-model-input` 成功（159985.SZ HFQ 已自然补齐，全部 10 标的预检通过）
   - `git worktree add --detach aa6b60d` 实际成功，但 git 的 stderr 进度输出经外层
     `2>&1` 重定向 + 脚本内 `$ErrorActionPreference='Stop'` 触发 PS 假异常中断
   - 已验证：worktree 存在于 `research_worktree\`，HEAD = aa6b60deef...（git 无损）
4. **手动续跑**（19:1x~19:2x）：按脚本源码逐字执行剩余四步：
   - `build_v481_rb_weights.py --decision-date 2026-09-03`（aa6b60d 版本不支持
     --next-trading-date，与脚本内动态检测逻辑一致，未传该参数）→ 83 行 target
   - `hydra_month_end publish-target ... --execution-date 20260904 --publisher-commit aa6b60d...`
   - `scripts.build_hydra_target_request ...`（server 仓库，PYTHONPATH 按脚本设置）
   - `scripts.hydra_capital_preflight ... --capital 100000/200000/700000`

所有命令与脚本源码中的调用完全一致，未跳过任何校验。

## 冻结四包（尝试 3 最终版）

| 流 | 行数 | 标的数 | file_sha256 |
| --- | --- | --- | --- |
| model_hfq (back) | 28391 | 10 (9 白名单 + 511010.SH 研究专用) | b378db0c3742d4ad92ed6bff7f9d8a4385383eb68854d63c0710bfa8c04dd1ae |
| execution_raw (none) | 25126 | 9 白名单 | e3913610f8bd2b8f0a12008ec0d6235dcf23d1962d5e6df1bcb0f1fa86d96b08 |
| corporate_actions | 20 | 4 | c3bc1fec0a690b3584d301c58073093035d8b43a08b719a0c5040eea8fbdcbb4 |
| trading_calendar | 249 | — | 62f0a7bb842a690c57a6d265f91a1805f3977f4b4b0888da0837c6decfe41c91 |

## 发布产物

- `release\Hydra_latest.parquet` — target_sha256: 390b17498dc8b3e4d84a75116c2c061e3405e1092ed2a6d2aebc58d763a02e2c
- `release\Hydra_latest.json` — sidecar_sha256: 8485567b9b1d3a3773ea28cfde2211f87bb9d118675b1ddc35c97bf5bd8a684a
- `target-request.json` — basket_sha256: 535457e17291f641a44eea9ad64543f879b762557e7d739023d67205dec82357（9 权重）
- `capital-preflight.json` — 见文件；7 万资金场景 exposure 99.5%、9/9 持仓、最大权重误差 0.27pp

## 最终权重（v48.1-RB@aa6b60d，weight_sum=1.0）

| 代码 | 权重 |
| --- | --- |
| 511260.SH | 0.798449 |
| 510300.SH | 0.058338 |
| 518880.SH | 0.037364 |
| 159985.SZ | 0.034860 |
| 159981.SZ | 0.021321 |
| 513500.SH | 0.015385 |
| 159930.SZ | 0.015328 |
| 513100.SH | 0.011348 |
| 159915.SZ | 0.007607 |

## 待人工审核后下一步

1. 人工审核本目录（权重、sidecar、capital preflight）
2. 管理员运行 `Enable-Hydra48Live.ps1 -ReleaseRoot <LOCAL_HYDRA_ROOT>\month-end\202609 ...`（激活 + stage + 创建 09:10/15:10 任务）
3. 激活后：任务切换到新 runner（Run-HydraLive.ps1 query/preflight/submit/settle 四段式）
