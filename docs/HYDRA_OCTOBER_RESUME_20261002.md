# 十月调仓：另一台电脑接力入口（2026-10-02）

**拉取 `codex/hydra-october-handoff-20261002`。这是当前开发/交接分支，包含生产基线及最新修复、研究结果和离线工具；拉代码不等于生产已经部署。** 主服务器本次不切版本、不重启，不提交实盘订单。

## 拉取代码

```sh
git clone --branch codex/hydra-october-handoff-20261002 git@github.com:FionaMMC/MaolaoyeServer.git
cd MaolaoyeServer
git rev-parse HEAD
```

已有仓库则先确认本机未提交修改，再 `git fetch origin`，切到该分支并 `git pull --ff-only`。不要在生产 `/opt/qmt-server` 目录照抄切分支命令。GitHub 访问需要你自己的 GitHub SSH 授权，服务器 `.env` 不包含该授权。

## 连接服务器

当前 SSH：`root@120.26.138.82:22`。在新电脑的 `~/.ssh/config` 配置：

```sshconfig
Host qmt
    HostName 120.26.138.82
    User root
    Port 22
    IdentityFile ~/.ssh/id_rsa
    IdentitiesOnly yes
```

`IdentityFile` 改成你实际保管并已获服务器授权的私钥路径。私钥不在仓库、不在 `.env` 中，也没有包含在本次交付；没有对应私钥时，需要用已有受信任机器为新电脑公钥开通访问。不要关闭主机指纹检查。当前服务器 ED25519 指纹（通过已有 SSH 会话读取）：

```text
SHA256:S4LnjEkxclAAFUFVMjBP7XwFqBJXG3nfmOFenaOq6ms
```

只读连通性检查：

```sh
ssh qmt 'hostname; git -C /opt/qmt-server rev-parse HEAD'
ssh qmt 'systemctl is-active qmt-server.service; curl -fsS http://127.0.0.1:8000/healthz'
ssh qmt 'curl -fsS http://127.0.0.1:8000/readyz'
```

查看 Web 服务可用本地隧道：`ssh -N -L 18000:127.0.0.1:8000 qmt`，然后访问 `http://127.0.0.1:18000`。API 鉴权仍使用私有配置，隧道不绕过鉴权。

## 版本与路径

|项目|本次核实值|
|---|---|
|生产主仓库 HEAD|`b688b6c989a9a4bd6d44a6a9a96df7f56f0b607b`|
|生产分支|`codex/etf-retry-live-20260927`，该提交从服务器导入并合入交接分支|
|生产工作目录|`/opt/qmt-server/v2.3/server`|
|Python|`/opt/qmt-server/venv/bin/python`|
|主进程|`qmt-server.service`，Uvicorn `app.main:app`，端口 8000，检查时 active、healthz ok、readyz ready|
|私有服务器环境|`/opt/qmt-server/v2.3/server/.env`，本次单独导出，绝不进 Git|
|生产策略配置|`v2.3/server/strategies.yaml` 有既有未提交运行配置差异；不能以 HEAD 断言工作树完全等同该提交|
|独立收盘影子服务|`/opt/qmt-server/releases/shadow-close-20261002/server`，由 systemd drop-in 选择，见影子部署报告|
|Windows/MiniQMT|由同事操作；没有本次原生切换/双阶段实盘验收回执|

私有 `server.private.env` 单独交给用户保管，仓库只保存说明。它是**服务器**环境，不能替代 Windows 的 `C:\hydra-live\config\hydra-live.env`，也不要用于本地测试；其中路径和权限属于生产环境。用安全渠道迁移，文件权限限制为本人可读写。

## 先读这些结果

1. [连续回放与备用金资金占用](../reports/hydra_fill_replay_20261002/REPORT.md)：全期/近期、0/1/3/5% 备用金、滑点/容量/终局延迟压力。**尚未同时通过年化拖累 ≤0.5 个百分点与每轮欠配 ≤2%。**
2. [十月同事交接清单](../reports/hydra_october_20261002/HANDOFF.md)：旧问题、首对交易日、需要的回执、当前缺口。
3. [现金队列与跨日剩余量修复](../reports/hydra_cash_edges_20261002/REPORT.md)：本地修复，不把测试通过写成实盘已部署。
4. [多因子/小盘/713 归因](../reports/hydra_acceptance_20260930/ATTRIBUTION.md)。
5. [收盘影子服务实际部署](../reports/hydra_release_20261002/DEPLOYMENT.md)：已部署的是不下单的独立影子 worker。
6. [远程研究数据路径](REMOTE_RESEARCH_DATA.md)。原始行情、账户明细、大型输入留在服务器，仅返回汇总。

## 十月必须保持的上下文

- 用户选择：参考日收盘卖、次日开盘买；若中间隔周末/假期，两步一起移到自然日相邻的交易日。10/8、10/9 是国庆后第一对；10/10 仍休市。
- 现有生产执行批次仍是同日卖买现金队列。`reference_date` 不代表已经在参考日收盘卖出。完整双阶段调度及 Windows 原生验收尚未完成；不能仅改日期就称上线。
- 30 秒等待循环在 Windows client 直接查询 MiniQMT。一次完整扫描后等待 30 秒，实际间隔为查询耗时加 30 秒。每笔新买单前再核对已确认卖款、策略归属现金、QMT 可用现金及预留资金。受理不等于成交；提交结果不明不能重发。
- 跨日维持同一版本权重和原股数上限，核实已成交后只补剩余量；可缩量、不扩量、不因最新估值自动反卖已完成证券。A1000 已成交、B500 未成交，不会因次日估值出现 A900/B600 就追加 B600。
- 9/30 市场日线已存在于远程 `data/market/daily/etfs/`，但正式 Hydra 月度四流仍截至 9/3，尚无 9/30 月度输入/目标。市场有行情不等于正式研究输入齐全。
- 用户接受的评估线：年化拖累 ≤0.5 个百分点、每轮结束总欠配 ≤2%。备用金和手续费余款全部计入总占用资金；不允许从收益分母删掉。

## 测试与后续工作

最新完整测试结果见 `reports/hydra_october_20261002/VERIFICATION.json`。服务端测试在开发环境运行，不加载生产 `.env`；客户端 offline acceptance 使用 mock 网关。

下一步先由同事返回 9/30 四流、当前 QMT 资产/持仓/委托/成交与旧单历史证据、已安装版本及任务清单。对账之后才能形成十月具体股数计划并测算整手可行误差、资金需求与阶段截止时间。不是等待人为重新批准研究，而是这些实际输入尚不可得。
