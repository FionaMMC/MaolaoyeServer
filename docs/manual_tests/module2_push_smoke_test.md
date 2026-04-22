# 模块二 Windows 集成冒烟测试

**前置条件:**
1. 模块一已跑通，`data\market_data\20260422.parquet` 存在
2. 服务器 `/market-data` 接口已就绪（搭档告知）
3. `config/settings.yaml` 的 `server.base_url` 和 `server.api_key` 已填正确
4. `config/settings.yaml` 的 `notify.wecom_webhook` 已填企业微信机器人 URL

**执行步骤（Windows PowerShell）:**

```powershell
cd C:\parttime\qmt模拟盘pipeline\server
C:\parttime\qmt数据推送\venv\Scripts\activate
python -m src.market_data_push --date 20260422 --config config\settings.yaml
```

**验收清单:**

- [ ] 退出码为 0
- [ ] 日志显示 `POST /market-data 第 1/3 次尝试` 和 `推送成功：received_count=...`
- [ ] 企业微信群收到"行情推送成功 (20260422)：N 条"提示
- [ ] 服务器侧确认收到数据（与搭档核对 received_count 一致）

**重试 & 报警验证（故意断网）:**

1. 断开机器网络，或把 `server.base_url` 改成 `https://nonexistent.example.com`
2. 重跑命令
3. 预期：日志显示 3 次网络异常；企业微信收到"[报警] 行情推送失败..."；退出码 3

**4xx 快速失败验证:**

1. 把 `server.api_key` 改成错误值
2. 重跑命令
3. 预期：日志显示 1 次 4xx，不重试；企业微信收到 alert；退出码 3

**幂等验证:**

1. 成功推送后立即再推一次同一日期
2. 预期：服务器返回 `code=2001`（日期重复）时视为非 0 business code，会重试 3 次，最终发 alert——这是 **预期行为**，服务器方可改为 `code=0, received_count=<原值>` 视作幂等成功
