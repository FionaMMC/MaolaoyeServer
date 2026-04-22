# 模块一 Windows 集成冒烟测试

**前置条件:**
1. Windows 机器上 QMT 客户端已启动并登录模拟盘账号
2. `C:\parttime\qmt数据推送\venv` 已配置（sitecustomize.py 暴露 xtquant）
3. 项目从 Mac 同步到 `C:\parttime\qmt模拟盘pipeline\server\`
4. `config/settings.yaml` 已从 `settings.example.yaml` 拷贝并填写正确的 `data_dir`

**执行步骤（Windows PowerShell）:**

```powershell
cd C:\parttime\qmt模拟盘pipeline\server
C:\parttime\qmt数据推送\venv\Scripts\activate
python -m src.market_data_download --date 20260422 --config config\settings.yaml
```

**验收清单:**

- [ ] 退出码为 0
- [ ] 日志末尾显示"完成，输出 ...\market_data\20260422.parquet"
- [ ] 文件 `data\market_data\20260422.parquet` 存在
- [ ] 用 Python 回读校验：
  ```python
  import pandas as pd
  df = pd.read_parquet("data/market_data/20260422.parquet")
  print(df.shape)
  print(df.columns.tolist())
  print(df.dtypes)
  print(df.head())
  assert df.shape[0] >= 4500
  assert df["close"].notna().sum() > 4000
  assert df["is_suspended"].sum() < 200
  ```
- [ ] 抽查 `600519.SH` 的 OHLCV 与东方财富当日收盘一致
- [ ] 重跑一次幂等：再执行命令应覆盖写入同一 parquet

**常见故障定位:**

| 现象 | 原因 | 处理 |
|---|---|---|
| `startup_check` 抛 RuntimeError | QMT 未登录或 data_dir 错 | 登录 QMT + 核对 data_dir |
| `get_stock_list_in_sector` 返回空 | 板块名写成指数代码了 | settings 里 `sector_name` 应为"沪深A股" |
| `get_market_data` 全 NaN | download 后没 sleep | 检查 `downloader.py` 的 `time.sleep(1)` 是否保留 |
| 退出码 3 | 当天非交易日或字段全缺 | 核对日期、查看下载日志 |
