# CLAUDE.md — QMT 模拟盘 Pipeline

每次开始任务前自动读取本文件。本文件说明项目背景、必读文档、以及开发规则。

---

## 项目背景

本项目目标是建立一套自动化交易信号执行管道：
- 本地通过 QMT（迅投）接口每日收盘后下载行情
- 将行情推送至服务器，服务器运行策略并生成买卖信号
- 本地查询信号后在次日开盘竞价前自动下单
- 收盘后将成交结果推回服务器，供策略绩效跟踪使用

当前状态：**规划阶段，尚未开始开发。**

---

## 必读文档（每次任务开始前读取）

### 本项目文档

- `C:\parttime\qmt模拟盘pipeline\项目设计文档（纯股）.md`
  当前开发版本：完整工作流、时间线、字段定义、模块拆分、SQLite 表结构、开发顺序

- `C:\parttime\qmt模拟盘pipeline\API接口文档（纯股）.md`
  当前开发版本：三个服务器接口的完整规范（行情推送、信号查询、成交回报）

- `C:\parttime\qmt模拟盘pipeline\项目设计文档（含期权）.md`
  扩展版，股票流程验证完成后参考

- `C:\parttime\qmt模拟盘pipeline\API接口文档（含期权）.md`
  扩展版，股票流程验证完成后参考

### 历史开发教训（必读，开发前务必了解）

- `C:\parttime\qmt数据推送\v3\CLAUDE.md`
  v1 开发过程中踩过的八个坑，包含：
  - `xtdata.data_dir` 必须显式设置
  - `get_stock_list_in_sector()` 参数是板块名而非指数代码
  - 交易日判断逻辑不要单独成模块
  - 不要手动操作 `sys.path`
  - `download_history_data` 后必须 `sleep(1)` 再读取
  - 只用 `download_history_data`，不用带 `2` 的版本

- `C:\parttime\qmt数据推送\v3\README.md`
  v3 数据下载项目文档，含目录结构、数据清洗说明、财务数据 Point-in-Time 查询方法

### XtQuant API 文档（遇到具体 API 问题时查阅）

- `C:\parttime\qmt数据推送\200. 快速开始 _ 迅投知识库.md`
  QMT 环境初始化、连接方式、venv 配置

- `C:\parttime\qmt数据推送\201. XtQuant.XtData 行情模块 _ 迅投知识库.md`
  行情下载、历史数据读取、板块成分、交易日历等 API

- `C:\parttime\qmt数据推送\202. XtQuant.Xttrade 交易模块 _ 迅投知识库.md`
  下单、撤单、查询持仓、查询成交等交易 API

- `C:\parttime\qmt数据推送\203. 完整实例 _ 迅投知识库.md`
  官方完整使用示例

---

## 开发规则（来自历史教训，不得违反）

1. 任何 xtquant API 调用之前，脚本顶层必须设置：
   ```python
   xtdata.data_dir = r"C:\parttime\平安证券量盈QMT策略交易平台\userdata_mini"
   ```

2. `download_history_data` 后必须 `time.sleep(1)` 再调用 `get_market_data`

3. 只用 `download_history_data`，不用 `download_history_data2`

4. `get_stock_list_in_sector()` 传板块名字符串（如 `"中证1000"`），不传指数代码

5. 不得在脚本中手动操作 `sys.path`；激活 venv 后直接 import 即可

6. 在 API 行为完全验证之前，保持单文件结构，不做过度抽象

7. 不写带 TODO 的占位函数；要么实现，要么不写

8. 每个脚本开头必须有 `startup_check()` 验证关键前提

---

## 环境信息

- QMT 客户端路径：`C:\parttime\平安证券量盈QMT策略交易平台`
- QMT 数据目录：`C:\parttime\平安证券量盈QMT策略交易平台\userdata_mini`
- Python 环境：使用 `C:\parttime\qmt数据推送\venv`（已配置 sitecustomize.py）
- 历史数据项目：`C:\parttime\qmt数据推送\v3\`（已完成全市场历史数据一次性下载）

---

## 任务分工

- **本地端**（本项目负责）：行情下载、数据清洗、行情推送、信号查询、竞价下单、成交回报推送、微信通知
- **服务器端**（搭档负责）：行情存储、策略运算、信号队列、成交回报处理、策略绩效跟踪、偏离检测
