# 模块五 + 六 Windows 集成冒烟测试

**前置条件:**
1. Plan A/B/C/D 已在 Windows 上跑通
2. `orders` 表里有至少一条 `submit_status='SUCCESS'` 的当日订单
3. QMT 客户端登录中，今日已经在集合竞价下过单（否则 query_stock_trades 返回空）

**09:35 auction 阶段验证:**

09:25~09:30 集合竞价结束后执行：

```powershell
cd C:\parttime\qmt模拟盘pipeline\server
C:\parttime\qmt数据推送\venv\Scripts\activate
python -m src.trade_result --stage auction --today 20260422 --config config\settings.yaml
```

- [ ] 退出码 0
- [ ] 日志显示 `query_stock_trades 返回 N 条`、`trades 表 stage=auction 写入 M 行`、`POST /trade-result stage=auction 第 1/3 次`
- [ ] 企业微信收到"竞价成交通知（20260422）：成交 X 单 / 部分 Y 单 / 未成 Z 单"
- [ ] SQLite 确认：
  ```sql
  SELECT signal_id, filled_quantity, status, report_status
  FROM trades
  WHERE signal_id IN (SELECT signal_id FROM signals WHERE valid_date='20260422');
  ```
  每行 `report_status='SUCCESS'`

**15:30 close 阶段验证:**

```powershell
python -m src.trade_result --stage close --today 20260422 --config config\settings.yaml
```

- [ ] 退出码 0
- [ ] 日志显示 `trades 表 stage=close 写入 M 行`
- [ ] 对比 auction 与 close：同一 order_id 在 trades 里只剩一行（DELETE+INSERT 语义）
- [ ] 若某单在盘中继续成交，`close` 的 filled_quantity/filled_price 应 >= auction 的值
- [ ] 企业微信收到"收盘成交汇总（20260422）：..."
- [ ] 服务器侧确认 close 的结果覆盖了 auction（搭档核查）

**未匹配信号演练:**

- 与搭档约定：服务器对 signal_id `s-nonexistent` 不认，返回 `unmatched_signal_ids: ["s-nonexistent"]`
- 手动在 orders 表插入一条关联到 `s-nonexistent` 的记录（仅测试）
- 运行命令
- [ ] 退出码 3
- [ ] 企业微信收到 `[报警] auction 存在未匹配信号：['s-nonexistent']`

**服务器离线演练:**

- 断网或改 `server.base_url` 为无效地址
- 运行命令
- [ ] 日志显示 3 次 network error
- [ ] 退出码 3，企业微信收到 `[报警] auction 成交回报推送失败`
- [ ] `trades` 表 `report_status='FAILED'` 已回标

**QMT 客户端未登录演练:**

- 关闭 QMT 客户端
- 运行命令
- [ ] 退出码 2，企业微信 `[报警] auction 回报脚本启动失败：QMT 连接异常`

**Windows 任务计划程序配置:**

- 09:35 触发 `scripts\daily_0935_auction_report.bat`
- 15:30 触发 `scripts\daily_1530_close_report.bat`（与模块一并行，互不干扰）
- 15:30 的脚本不应 crash 于 orders 表为空（当天根本没有信号 / 没下单）— 由 `_load_orders` 返回空列表处理
