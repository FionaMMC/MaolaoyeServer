# 影子账本独立服务部署 · 2026-10-02

状态：`DEPLOYED_NO_ORDER_WORKER`，实际服务运行`PASS`。范围仅限理论影子记账；没有启用任何新实盘自动买卖。

## 部署方式

生产主目录保留在`/opt/qmt-server/v2.3/server`，其应用文件与已有策略配置改动均未覆盖。新版本部署到`/opt/qmt-server/releases/shadow-close-20261002/server`，在服务器原有代码基础上仅替换影子模型、服务以及对应测试，并追加ShadowFill模型注册。

通过`/etc/systemd/system/qmt-shadow-ledger.service.d/50-shadow-close-20261002.conf`覆盖独立影子服务的ExecStart与PYTHONPATH。实际入口是新发布目录中的`server/scripts/run_shadow_ledger.py`；工作目录、环境文件、行情根目录和数据库沿用生产配置。主服务的内置scheduler为disabled，因此未发现另一个主进程内的旧影子定时器与本次发布并行执行。

新建`shadow_fills`表保留向前模拟成交审计，不补造旧成交，不改变真实orders/trades。使用qmtserver身份验证模块加载路径并执行这一项新增表迁移。

## 验证

- 本地相关回归51项通过；服务器隔离测试26项通过。测试工具仅安装在发布目录`test-deps`，生产venv包未更新。
- 代码/迁移部署前后，原有30张表逐行内容的聚合SHA-256不变。新增表为空。
- 10/2 09:51 CST实际执行独立影子service成功，进程退出码0、systemd Result=success，定时器active。
- 运行仅改变影子状态和影子净值表；所有非影子表哈希不变。Base为active；Hydra为stale_target、仅估值。当天未生成新模拟成交。
- `/healthz=ok`、`/readyz=ready`；主服务仍是9/28启动的进程，交易服务未重启，私有环境及策略配置SHA不变。

历史研究中的492笔模拟成交不等于本轮生产新增成交；未来有新目标时的首轮成交仍需以shadow_fills和官方收盘价验证。

## 回滚

原systemd服务文件和生产应用未覆盖，因此回滚只需移除本次专用drop-in并daemon-reload，不能恢复整份旧数据库来覆盖新账。

由运维先确认`qmt-shadow-ledger.service`没有运行，然后执行：

```sh
sudo rm /etc/systemd/system/qmt-shadow-ledger.service.d/50-shadow-close-20261002.conf
sudo systemctl daemon-reload
sudo systemctl show qmt-shadow-ledger.service -p ExecStart
```

恢复后的入口应为原生产目录下`python -m scripts.run_shadow_ledger`。不删除shadow_fills表或其审计记录，不修改交易服务或定时器配置。部署失败处理也遵循这个原则。

后续主系统升级时需要同步评估本独立发布的基础依赖，避免影子服务长期固定在旧版app副本。

回执：`deployment_receipt.json`、`worker_run_receipt.json`、`stage_receipt.json`、`shadow_tests.xml`。服务器备份目录：`/var/backups/qmt-server/shadow-close-20261002-hukqm6hn`。
