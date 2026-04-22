# 模块四 Windows 集成冒烟测试

**前置条件:**
1. Plan A/B/C 已上 Windows 并跑通；`data/trading.db` 的 `signals` 表里有明日 `valid_date` 的待下单信号
2. QMT 客户端已登录模拟盘账号（`settings.yaml` 的 `qmt.account_id` 与客户端一致）
3. QMT 客户端已启用交易功能（xttrader 接口需要在客户端设置里打开）
4. 本次是 **模拟盘**，不要用真金账号做冒烟测试

**干跑验证（任意时间均可）:**

```powershell
cd C:\parttime\qmt模拟盘pipeline\server
C:\parttime\qmt数据推送\venv\Scripts\activate
python -m src.auction_order --today 20260422 --config config\settings.yaml
```

- [ ] 退出码 0
- [ ] QMT 委托窗口看到对应 N 条限价委托，`price = limit_price × (1 + price_offset)`（精度 0.01）
- [ ] `orders` 表新增 N 行：
  ```python
  import sqlite3
  c = sqlite3.connect("data/trading.db")
  print(c.execute(
      "SELECT order_id, signal_id, submitted_price, submit_status "
      "FROM orders ORDER BY submitted_at DESC LIMIT 10"
  ).fetchall())
  ```
- [ ] 企业微信收到"竞价下单完成：N 单成功"

**幂等验证:**

- 立即重跑相同命令
- [ ] 第二次不再重复下单（`read_active_signals` 排除已存在的 signal_id）
- [ ] `orders` 表行数不增

**QMT 断开演练:**

- 关掉 QMT 客户端后跑命令
- [ ] 退出码 2
- [ ] 企业微信收到 `[报警] 下单脚本启动失败：QMT 连接异常 ...`

**MARKET 信号拒绝演练:**

- 手动 SQL 插入一条 `order_type='MARKET'` 的信号（仅测试）:
  ```sql
  INSERT INTO signals VALUES ('test-mkt', '600519.SH', 'BUY', 100,
    'MARKET', NULL, 0.005, 'test', '2026-04-21T18:30:00+08:00',
    '20260422', '2026-04-21T19:00:00+08:00');
  ```
- 跑命令
- [ ] 该信号的 orders 行 `submit_status='FAILED'`, `order_id='fail-test-mkt'`
- [ ] 企业微信收到 `[报警] 竞价下单完成 ... / 1 单失败`，其中包含 `test-mkt: MARKET 单被拒`
- 测试完成后记得 `DELETE FROM signals WHERE signal_id='test-mkt'` 并 `DELETE FROM orders WHERE signal_id='test-mkt'`

**真实触发验证（交易日 09:10）:**

1. 配置 Windows 任务计划程序：
   - 触发器：每交易日 09:10（或用日重复 + 排除周末）
   - 操作：启动程序 `C:\parttime\qmt模拟盘pipeline\server\scripts\daily_0910_auction.bat`
   - 选项：勾选"不管用户是否登录都运行"需要密码，若不勾则要保证该 Windows 账号已登录
2. 确认任务计划程序执行后：
   - [ ] `logs/auction_YYYYMMDD.log` 末尾显示完成
   - [ ] QMT 委托窗口 09:15 前已提交所有限价单
   - [ ] 09:25 集合竞价结束后查 QMT 是否成交（部分成交 / 全部成交 / 未成交）
   - 注：成交结果由模块五 09:35 查询并推服务器
