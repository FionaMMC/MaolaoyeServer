# 模块三 Windows 集成冒烟测试

**前置条件:**
1. 模块一、二已跑通，服务器能正常接 `POST /market-data`
2. 与搭档约定好：当日 `POST /market-data` 之后，服务器会生成次日信号
3. `config/settings.yaml` 已配置 server + notify + sqlite_path
4. QMT 客户端正常登录（只为 `next_trading_day` 查交易日历）

**执行步骤（Windows PowerShell）:**

```powershell
cd C:\parttime\qmt模拟盘pipeline\server
C:\parttime\qmt数据推送\venv\Scripts\activate
python -m src.signal_query --today 20260421 --config config\settings.yaml
```

**有信号场景验收:**

- [ ] 退出码 0
- [ ] 日志显示 `state=HAS_SIGNALS`，`signals 表 upsert N 行`
- [ ] 企业微信收到"已制单（20260422），共 N 条信号"
- [ ] SQLite 查询确认：
  ```python
  import sqlite3
  c = sqlite3.connect("data/trading.db")
  print(c.execute("SELECT signal_id, symbol, direction, valid_date FROM signals "
                  "WHERE valid_date='20260422'").fetchall())
  ```

**无信号场景验收:**

- 让搭档手动把当日信号队列清空后再运行
- [ ] 退出码 0，企业微信收到"今日无交易信号（20260422）"（非报警）

**3002 场景验收:**

- 与搭档协调：让服务器在策略运行中返回 3002
- 运行时加 `--wait-secs 10` 加速重试
- [ ] 日志显示等待 10s 后重试；若仍 3002 → 退出码 3，企业微信收到 `[报警]`

**过期信号拒绝验收:**

- 让服务器返回 `valid_date=错误日期` 的信号
- [ ] 日志显示 `校验拒绝 N 条信号`，该信号未写入 signals 表；通知消息含"X 条被拒"

**schema 幂等验证:**

- 重复运行命令
- [ ] 同一 signal_id 不重复插入（`SELECT COUNT(*)` 不增长），quantity 等字段被新值覆盖
