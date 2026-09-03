# Hydra 2026-09-04 实盘订单拉取 Handoff

交接日期：2026-09-03  
执行目标：接手人从现场补齐研究/QMT 信息，持续执行到 Windows 成功冻结 `20260904` 订单并完成晚间 online preflight。  
边界：本 handoff **授权生成和拉取明日 live 订单，但不授权今晚提前 submit，不授权手改权重/hash/日期，不授权删库或覆盖状态库。**

## 0. 完成标准

以下八项必须同时满足：

1. Server 只有一个 `execution_date=20260904` 的 Hydra live target；
2. Server 有一个 `trade_date=20260904`、`order_count>0` 的 attempt；
3. stage 后 Server generation 已重新关闭；
4. Windows `query` 返回 `FETCHED`，或同一批次的 `ALREADY_FETCHED`，且 `orders>0`；
5. Windows `preflight` 返回 `READY_FOR_OFFLINE_SUBMIT`；
6. stage、query、preflight 的 `batch_sha256` 完全一致；
7. preflight 持仓差异计数全为 0，且 `abs(cash_diff)<=1`；
8. 今晚 MiniQMT 没有因本流程新增的真实委托。

八项全部满足即完成本次 handoff。明早 `submit` 是下一阶段，今晚不得手工试跑。

## 1. 已知现场事实

截至 2026-09-03 19:31（中国时间）的只读核验：

- Server 健康：`healthz=ok`、`readyz=ready`、systemd active、当前重启计数 0；
- Server 代码：`00016b9274ce1652d1d5bfa5f70e967fce0f1187`；
- Windows 客户端应为：`c22af8a0fda1dd82e1555679c32237ab9e387576`；
- publisher allowlist 已包含 `aa6b60deef44b244764385e7b6bd681429b9b362`；
- live instance：`live_hydra_v481_rb`；account alias：`hydra-live`；
- Server 当前账本：现金 `72057.27`，持仓 `{}`，reconciliation status 为 `ok`；
- generation 关闭，delivery 开启，Server risk mode 为 disabled；
- Server 尚无 `20260904` target、attempt 或 live order；
- Server 留有一笔 `20260831` 的旧 canary PENDING 记录；团队说明真实 canary 已成交，所以必须先核对 canary 后的 QMT 当前事实。

## 2. 执行规则与 hard stop

从 Phase A 开始顺序执行。普通路径、工具或重复安装问题由接手人自行修复后继续，无需逐步请示。

以下情况必须停止并保留证据，不能自行绕过：

1. 当前 QMT 现金/持仓与 Server 基线不一致；
2. 研究交付物的 execution date 不是 `20260904`，或日期/hash 不一致；
3. Server 已有另一个 2026 年 9 月 Hydra target，且 basket hash 不同；
4. 出现真实 QMT 委托、未知提交状态或疑似重复订单。

禁止通过删订单、删 target、覆盖 SQLite、手改 JSON、虚构 cash-flow 或提前 submit 来消除报错。

## Phase A — Windows 安装核验

在 Windows PowerShell 执行：

```powershell
$InstallRoot = "C:\hydra-live"
$PythonExe = "C:\parttime\annaconda\envs\py311_qmt\python.exe"
$ExpectedRelease = "c22af8a0fda1dd82e1555679c32237ab9e387576"

$ActualRelease = (Get-Content `
  "$InstallRoot\config\active-release.txt" -Raw).Trim()
if ($ActualRelease -ne $ExpectedRelease) {
  throw "错误版本：expected=$ExpectedRelease actual=$ActualRelease"
}

& "$InstallRoot\bin\Run-HydraLive.ps1" `
  -Command doctor -InstallRoot $InstallRoot -PythonExe $PythonExe
if ($LASTEXITCODE -ne 0) { throw "doctor failed" }
```

通过标准：doctor 输出 `LOCAL_CONFIG_OK`、`state_schema=ok`、`server_contacted=false`、`qmt_contacted=false`。安装器最后一次结果还应包含：

```json
{
  "status": "INSTALLED",
  "offline_acceptance": "PASS",
  "local_doctor": "PASS",
  "env_preserved": true,
  "state_preserved": true,
  "tasks_modified": false
}
```

`tasks_modified=false` 是正常的，只表示安装器没有擅自修改计划任务。

## Phase B — 锁定并审核唯一研究目录

找到截图对应的完整 `C:\hydra-live\month-end\...` 目录，记为 `$MonthEndRoot`。不要凭截图抄权重。目录至少应包含：

- `Hydra_latest.parquet` 和唯一配套 sidecar JSON；
- `snapshot.json`；
- 四个目录及各自的 `data.parquet`、`manifest.json`：`model_hfq`、`execution_raw`、`corporate_actions`、`trading_calendar`；
- capital/lot-rounding 报告和研究审计结论。

必须确认：

- strategy 为批准的 `v48.1-RB`；
- publisher 为完整 SHA `aa6b60deef44b244764385e7b6bd681429b9b362`；
- 9 只 ETF 无重复、权重非负、权重和为 1；
- target 中没有 `511010.SH`；
- target、sidecar、snapshot、四个 manifest 的日期/hash 一致；
- `execution_date=20260904`，冻结日历也确认它是 decision date 后下一交易日；
- capital 报告基于本次实际账户规模，并给出整手后的仓位覆盖/偏离；
- 没有 `REJECTED`、缺价格、停牌、未解释公司行动或 hash mismatch。

如果 sidecar 的 execution date 不是 `20260904`，不得编辑旧 JSON。用同一批准 producer 在新目录重新发布 catch-up artifact，并重算全部关联 hash。

## Phase C — stage 前核对 canary 后账户基线

在 MiniQMT 账户页或用已验证的只读查询，记录当前：可用现金、总资产、全部持仓、可卖持仓、活动/可撤委托。

与 Server 已知基线比较：

```text
Server cash      = 72057.27
Server positions = {}
```

通过标准：Hydra 白名单持仓完全相同、现金差绝对值不超过 1 元、没有活动或未知委托。

如果 canary 买入的 100 份仍在账户，或者现金因成交/费用变化，这里必然不通过。不要先 stage。当前公开 live API 故意不能覆盖已初始化账本；应保存 QMT 委托、成交、费用、现金和持仓证据，走受控 baseline recovery。不得把 canary 假装成 formal Hydra trade，也不得直接改 SQLite 凑平。

只有 Phase C 通过后才能继续。

## Phase D — 安装四份冻结数据

把 Phase B 的完整目录通过现有受控方式传到 Server 私有目录，例如：

```text
/opt/qmt-server/private/hydra-incoming/20260904/
```

传输后确保 `qmtserver` 只能读取、不能由其他普通用户改写：

```bash
INCOMING=/opt/qmt-server/private/hydra-incoming/20260904
sudo chown -R root:qmtserver "$INCOMING"
sudo chmod -R u=rwX,g=rX,o= "$INCOMING"
```

在 Server 执行，`INCOMING` 按实际路径调整：

```bash
cd /opt/qmt-server/v2.3/server
INCOMING=/opt/qmt-server/private/hydra-incoming/20260904

for stream in model_hfq execution_raw corporate_actions trading_calendar; do
  sudo -u qmtserver /opt/qmt-server/venv/bin/python \
    -m scripts.stage_hydra_data \
    --parquet "$INCOMING/$stream/data.parquet" \
    --manifest "$INCOMING/$stream/manifest.json" \
    --root /opt/qmt-server/v2.3/server/data || exit 1
done
```

四次都应成功。`installed=false` 只表示完全相同的内容地址已存在，是允许的幂等结果；hash/内容不同必须停止，不得覆盖。

## Phase E — 生成 live target request

```bash
cd /opt/qmt-server/v2.3/server
INCOMING=/opt/qmt-server/private/hydra-incoming/20260904

sudo -u qmtserver /opt/qmt-server/venv/bin/python \
  -m scripts.build_hydra_target_request \
  --target "$INCOMING/Hydra_latest.parquet" \
  --sidecar "$INCOMING/Hydra_latest.json" \
  --data-snapshot "$INCOMING/snapshot.json" \
  --execution-date 20260904 \
  --execution-domain live \
  --account-alias hydra-live \
  --instance-id live_hydra_v481_rb \
  --cash-buffer 0 \
  --output "$INCOMING/target-request-live-20260904.json"
```

sidecar 文件名不同则替换为 Phase B 核实的唯一配套文件，不能猜。记录输出的 `basket_sha256` 和 weights 数量。此步骤不创建订单。

## Phase F — 备份并短暂开启 Server generation

动态 `auto` 风控已是运行单中的既定选择；本次把订单数上限收紧为 9。

先备份配置、service unit 和 SQLite：

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=/opt/qmt-server/backups/hydra-prestage-$STAMP
sudo mkdir -p "$BACKUP"
sudo install -m 600 /opt/qmt-server/v2.3/server/.env "$BACKUP/server.env"
sudo systemctl cat qmt-server | sudo tee "$BACKUP/qmt-server.service.txt" >/dev/null

sudo /opt/qmt-server/venv/bin/python - "$BACKUP/pipeline-server.db" <<'PY'
import sqlite3
import sys
from pathlib import Path

source = Path("/opt/qmt-server/v2.3/server/pipeline-server.db")
destination = Path(sys.argv[1])
src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
dst = sqlite3.connect(destination)
try:
    src.backup(dst)
    if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise RuntimeError("backup quick_check failed")
finally:
    dst.close()
    src.close()
PY
```

使用 `sudoedit /opt/qmt-server/v2.3/server/.env`，保证每个变量只出现一次，并设置：

```text
QMT_LIVE_ORDER_GENERATION_ENABLED=true
QMT_LIVE_ORDER_DELIVERY_ENABLED=true
QMT_HYDRA_LIVE_RISK_MODE=auto
QMT_LIVE_AUTO_MAX_DAILY_ORDERS=9
QMT_LIVE_AUTO_BUFFER_BPS=100
```

不要改 API key、账户 alias、账户身份或 publisher allowlist。受控重启一次：

```bash
sudo systemctl restart qmt-server
sleep 3
systemctl is-active qmt-server
systemctl show qmt-server -p NRestarts --value
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

只有 active、health ok、ready/ownership ok 且没有连续重启才继续。

## Phase G — 只 stage 精确 target

不要把 key 放进命令行或 shell history。下面从 Server 私有配置读取 key：

```bash
cd /opt/qmt-server/v2.3/server
INCOMING=/opt/qmt-server/private/hydra-incoming/20260904

sudo -u qmtserver /opt/qmt-server/venv/bin/python - \
  "$INCOMING/target-request-live-20260904.json" <<'PY'
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from app.settings import get_settings

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
settings = get_settings()
request = urllib.request.Request(
    "http://127.0.0.1:8000/hydra/targets/stage",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {settings.live_api_key}",
        "Content-Type": "application/json",
    },
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        print(response.status)
        print(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    print(exc.code)
    print(exc.read().decode("utf-8"))
    raise
PY
```

成功响应必须满足：domain 为 live、trade date 为 `20260904`、`0<order_count<=9`，并返回 target/attempt/batch id 和 `batch_sha256`。首次应为 `idempotent_replay=false`；完全相同请求因输出丢失而重试时允许为 `true`。

HTTP 409 若表示同月不同 target，不得删除旧 target，先比较 basket hash。

## Phase H — 立即重新关闭 generation

使用 `sudoedit` 只把下列项改回：

```text
QMT_LIVE_ORDER_GENERATION_ENABLED=false
```

保持 delivery 为 true、risk mode 为 auto。再次受控重启并重复 health/ready/重启计数检查。generation 仍为 true 时不得离开 Server。

## Phase I — Server 订单核验

用只读 SQLite 查询核对 `20260904`，最终证据必须显示：

- 恰好一个 live target 和一个 attempt；
- 所有订单 valid date 为 `20260904`、状态为 PENDING；
- 所有订单共享 Phase G 的同一 batch hash；
- Windows query 前 `fetched_at` 为空；
- 数量为 100 的整数倍，symbol 属于批准的 9 只 ETF；
- limit price 基于未复权执行价格，偏移不超过 50bps。

订单为空时不要伪造测试单；回到 stage 响应、整手取整和资金报告排查。

## Phase J — Windows 拉取并冻结订单

先备份私有 env，再只修改以下非秘密运行开关。今晚保持 trading disabled：

```text
HYDRA_LIVE_RISK_MODE=auto
HYDRA_LIVE_AUTO_MAX_DAILY_ORDERS=9
HYDRA_LIVE_AUTO_BUFFER_BPS=100
HYDRA_LIVE_TRADING_ENABLED=false
```

重新执行一次 doctor，必须显示 `risk_mode=auto`、`trading_enabled=false`。如果同名变量重复，先清理到每项唯一一行。

```powershell
$InstallRoot = "C:\hydra-live"
$PythonExe = "C:\parttime\annaconda\envs\py311_qmt\python.exe"

& "$InstallRoot\bin\Run-HydraLive.ps1" `
  -Command query -Date 20260904 `
  -InstallRoot $InstallRoot -PythonExe $PythonExe
if ($LASTEXITCODE -ne 0) { throw "query failed" }
```

通过标准：status 为 `FETCHED` 或同一批次的 `ALREADY_FETCHED`，orders 大于 0，日期为 `20260904`，batch hash 与 Phase G 相同。

成功后不得重新生成 Server 订单，也不得删除/替换 `C:\hydra-live\state` 中的 SQLite。若返回 `NO_ORDERS`，依次核对 date/domain/alias/delivery 和 Server PENDING orders，不要换日期碰运气。

## Phase K — 今晚完成 online preflight

确认 MiniQMT 已登录且没有活动/未知委托，然后执行：

```powershell
& "$InstallRoot\bin\Run-HydraLive.ps1" `
  -Command preflight -Date 20260904 `
  -InstallRoot $InstallRoot -PythonExe $PythonExe
if ($LASTEXITCODE -ne 0) { throw "preflight failed" }
```

唯一成功状态是 `READY_FOR_OFFLINE_SUBMIT`。同时要求 batch hash 与 Phase G/J 相同、三类持仓差异均为 0、现金差绝对值不超过 1 元、risk mode 为 auto、买卖金额与剩余现金合理。

preflight 失败时不得通过重复 query、删本地库或关闭检查解决。保留冻结批次；如果事实变化会改变订单，由负责人决定是否废弃整个 target，不能静默替换。

## Phase L — 核对明早任务，今晚不执行

09:10 action 应指向：

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\hydra-live\bin\Run-HydraLive.ps1" -Command submit -Date 20260904 -PythonExe "C:\parttime\annaconda\envs\py311_qmt\python.exe"
```

同时确认不再调用旧 `order_submit.py`，query/preflight/submit 没有合在一个 action，任务拒绝重叠运行，运行身份能访问 MiniQMT 和私有目录。客户端 `HYDRA_LIVE_RISK_MODE` 应为 auto；是否打开 `HYDRA_LIVE_TRADING_ENABLED` 由明早既定实盘授权决定，今晚不能用手工 submit 测试。

## 3. 最终 evidence 模板

成功后请填写并发回；不要包含 API key 和真实账户号：

```text
HYDRA 20260904 ORDER PULL: PASS

Windows active release: c22af8a0fda1dd82e1555679c32237ab9e387576
Research publisher: aa6b60deef44b244764385e7b6bd681429b9b362
Research as_of_date:
Research decision_date:
Execution date: 20260904
Target basket_sha256:
Server target_id:
Server attempt_id:
Server batch_sha256:
Server order_count:
Server generation reclosed: true
Server delivery enabled: true
Windows query status:
Windows query order_count:
Windows query batch_sha256:
Windows preflight status: READY_FOR_OFFLINE_SUBMIT
Reconciliation mismatched/server_only/qmt_only: 0/0/0
Reconciliation cash_diff:
Risk mode: auto
MiniQMT orders created tonight: 0
09:10 task points to stable runner: true
Evidence directory:
Operator:
Completed at:
```

## 4. 故障分流

- doctor 失败：修 Windows 安装/私有配置，不碰 target。
- 日期不是 `20260904`：重新发布研究 artifact，不手改 JSON。
- QMT 与 Server baseline 不一致：完成受控 baseline recovery，不 stage。
- 数据 batch 不存在：安装完全相同的四份数据，不覆盖内容地址。
- stage 返回 423：generation/auto risk 未加载；核对 `.env` 唯一值和重启。
- stage 返回 409：同月 target 冲突；不删除，比较 basket hash。
- query 返回 `NO_ORDERS`：核对 date/domain/alias/delivery 和 PENDING orders。
- preflight 对账失败：保留冻结批次，修复账本事实，不下单。
- 出现真实委托：立即停止，保存 QMT order id/status/remark，禁止自动重试。
