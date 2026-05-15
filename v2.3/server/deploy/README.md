# 阿里云 ECS 部署指南 — v2.3 server

把这份文档发给搭档（或你自己执行）。从干净 Ubuntu 22.04 ECS 到生产可用，全步骤覆盖。

---

## 0. 前置信息

需要确认：

- [ ] 阿里云 ECS 公网 IP：`__________`
- [ ] SSH 登录账号（通常 root）
- [ ] 域名（可选，仅 nginx 反代时需要）
- [ ] 跟 client 端约定的 API Key（生产环境用强随机串）

---

## 1. 一键 bootstrap

```bash
ssh root@<公网IP>
cd /tmp
git clone git@github.com:FionaMMC/MaolaoyeServer.git
cd MaolaoyeServer
bash v2.3/server/deploy/bootstrap.sh
```

或者跳过 git clone，直接：

```bash
curl -fsSL https://raw.githubusercontent.com/FionaMMC/MaolaoyeServer/master/v2.3/server/deploy/bootstrap.sh | bash
```

bootstrap 会：
- 装 Python 3.11
- 创建 `qmtserver` 系统用户
- git clone 代码到 `/opt/qmt-server/`
- 建 venv 装依赖
- 复制 `.env.example` 为 `.env`
- 注册 systemd unit
- enable（不 start，等编辑 .env）

---

## 2. 编辑 .env

```bash
vi /opt/qmt-server/v2.3/server/.env
```

至少改这几项：

```bash
QMT_API_KEY=<跟 client 约定的强随机串>
QMT_PORT=8000
QMT_LOG_JSON=true     # 生产开 JSON 日志
QMT_LOG_LEVEL=INFO

# 这些通常不用动
QMT_DB_URL=sqlite:////opt/qmt-server/v2.3/server/pipeline-server.db
QMT_PARQUET_ROOT=/opt/qmt-server/v2.3/server/data
QMT_PLUGINS_DIR=/opt/qmt-server/v2.3/server/plugins
QMT_STRATEGIES_FILE=/opt/qmt-server/v2.3/server/strategies.yaml
```

---

## 3. （可选）从 Windows client 同步初始 Parquet 数据

如果 client 端 `data_collector.py` 已经在 Windows 上采过历史 Parquet（通常 65 MB+），把它 rsync 上来作为 server 端 cold start 数据：

```bash
# 在 Windows 端（如果有 ssh）
rsync -avz C:/parttime/qmt模拟盘pipeline/v2.3/data/ \
    qmtserver@<公网IP>:/opt/qmt-server/v2.3/server/data/

# 或者：手动打包传上去
# Windows: tar -czf data.tar.gz v2.3/data/  (用 Git Bash / WSL)
# scp 后在 ECS 上 cd /opt/qmt-server/v2.3/server && tar -xzf /tmp/data.tar.gz
```

不同步也行——server 启动后会接收 client 每天的 `POST /market-data` 增量入库；只是策略前 N 天没历史数据可用。

---

## 4. 配置策略

```bash
vi /opt/qmt-server/v2.3/server/strategies.yaml
```

按 `00-overview.md` 的格式定义你的 `account_groups`。最少一条以验证 server 启动。

把策略 .py 文件放到 `/opt/qmt-server/v2.3/server/plugins/`：

```bash
# 示例：用自带的 buy_on_dip_example
ls /opt/qmt-server/v2.3/server/plugins/
# 应能看到 _example_buy_threshold.py 和 README.md
```

---

## 5. 阿里云控制台开安全组

ECS 控制台 → 实例 → 安全组 → 配置规则 → 入方向 → 添加：

| 字段 | 值 |
|---|---|
| 协议类型 | 自定义 TCP |
| 端口范围 | 8000/8000 |
| 授权对象 | 0.0.0.0/0（联测期开放，上线收紧到 client IP） |

如果挂 nginx 反代到 80/443，则放行 80 + 443 而不是 8000。

---

## 6. 启动 + 验证

```bash
systemctl start qmt-server
systemctl status qmt-server     # 应显示 active (running)
journalctl -u qmt-server -f      # 看启动日志
```

验证（在你 Mac 上）：

```bash
# liveness
curl http://<公网IP>:8000/healthz
# {"status":"ok"}

# readiness
curl http://<公网IP>:8000/readyz
# {"status":"ready","checks":{"parquet_root":"ok"}}

# 鉴权 + GET /orders（无订单时正常返回空）
curl -H "Authorization: Bearer <你的 API_KEY>" \
     "http://<公网IP>:8000/orders?date=20260430"
# {"code":0,"message":"ok","data":{"date":"20260430","orders":[]}}

# 推一条行情试试
curl -X POST http://<公网IP>:8000/market-data \
  -H "Authorization: Bearer <你的 API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "trade_date":"20260430",
    "stocks":[{"symbol":"600519.SH","open":1500,"high":1520,"low":1490,"close":1510,
               "volume":1000,"amount":1500000,"is_suspended":false}],
    "indexes":[],
    "etfs":[]
  }'
# {"code":0,"message":"ok","data":{"trade_date":"20260430","received":{"stocks":1,...}}}
```

---

## 7. 切换 client base_url

在 Windows client 的 `config.py`：

```python
PUSH_MODE = "server"
SERVER_BASE_URL = "http://<公网IP>:8000"  # 或 https://your-domain
API_KEY = "<跟 server 约定的同一个 key>"
```

跑一遍 client e2e（`v2.3/client/run_e2e.py`），看是否能成功 POST 行情、GET 订单、POST 回报。

---

## 8. 日常运维

```bash
# 重启
systemctl restart qmt-server

# 看日志
journalctl -u qmt-server -f
# 或: tail -f /var/log/qmt-server/server.log

# 手动触发管线（联测、灾备）
curl -X POST -H "Authorization: Bearer <KEY>" \
     "http://<公网IP>:8000/admin/run-pipeline?trade_date=20260430"

# 查看 Swagger UI 文档
# 浏览器打开: http://<公网IP>:8000/docs

# 升级代码
cd /opt/qmt-server
sudo -u qmtserver git pull
sudo -u qmtserver venv/bin/pip install -e v2.3/server[dev]   # 万一依赖更新
systemctl restart qmt-server
```

---

## 9. 常见故障排查

| 现象 | 原因 | 修复 |
|---|---|---|
| `systemctl status` 显示 failed | 看 `journalctl -u qmt-server -n 50` | 通常是 .env 配置错或 venv 路径错 |
| `curl http://<公网IP>:8000/healthz` 超时 | 安全组没开 8000 | 阿里云控制台加规则 |
| `curl ...` connection refused | server 没起 / 绑了 127.0.0.1 | systemd unit 检查 `--host 0.0.0.0` |
| 401 + code=1001 | API_KEY 不匹配 | 对一遍两边的 .env / config.py |
| GET /orders 永远空 | strategies.yaml 没配置或没行情数据 | 推一次 /market-data 后等 cron 16:00 触发，或手动 POST /admin/run-pipeline |
| /readyz 503 | parquet_root 不存在 | `mkdir -p /opt/qmt-server/v2.3/server/data` |

---

## 10. 服务硬化（生产必做）

### 10a. systemd 沙箱

unit 已带（参见 `qmt-server.service` `[Service]` 末尾）：

- `NoNewPrivileges` `PrivateTmp` `ProtectSystem=strict` `ProtectHome`
- `ReadWritePaths=/opt/qmt-server/v2.3/server /var/log/qmt-server`
- `ProtectKernel{Tunables,Modules}` `ProtectControlGroups`
- `RestrictNamespaces` `RestrictRealtime` `LockPersonality`
- `SystemCallArchitectures=native`

部署时只需 `cp qmt-server.service /etc/systemd/system/ && systemctl daemon-reload && systemctl restart qmt-server`。

### 10b. 日志轮转

```bash
cp /opt/qmt-server/v2.3/server/deploy/logrotate-qmt-server.conf \
   /etc/logrotate.d/qmt-server
logrotate -v -d /etc/logrotate.d/qmt-server   # 干跑验证
```

每天滚一次，保 14 天，gzip 压缩，`copytruncate` 避免重启服务。

### 10c. UFW 白名单（推荐）

```bash
# 删僵尸规则（如装机时阿里云模板留下的）
for p in 20 21 888 37415 39000:40000; do ufw --force delete allow $p/tcp; done

# 宝塔面板 8888 收紧到管理员 IP
ufw --force delete allow 8888/tcp
ufw allow from <你的固定IP> to any port 8888 proto tcp
ufw allow from 127.0.0.1 to any port 8888 proto tcp

# 业务端口保留
# 22, 80, 443, 8000 都 ALLOW Anywhere（8000 在 §11 nginx 上线后再收紧）

ufw reload
ufw status numbered
```

### 10d. 阿里云安全组（手动 console）

UFW 是 OS 层，阿里云 SG 是云网层。两层都要：

- 8888 → 删 `0.0.0.0/0`，加你 Mac IP/32
- 22 → 建议也收紧到你 Mac IP/32

---

## 11. 切换 nginx + HTTPS（可选，上线推荐）

```bash
apt install -y nginx certbot python3-certbot-nginx
cp /opt/qmt-server/v2.3/server/deploy/nginx-example.conf \
    /etc/nginx/sites-available/qmt-server.conf
# 编辑改 server_name 为真实域名
ln -s /etc/nginx/sites-available/qmt-server.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 签 Let's Encrypt
certbot --nginx -d your-domain.example.com

# 之后 client base_url 改成 https://your-domain.example.com（不用端口号）
# 安全组 8000 可以收紧成"只允许 127.0.0.1"
```

---

## 11. 干掉旧 v1 mock

如果之前部署过 v1 的 `mock_server.py`（来自 `scripts/mock_server.py`），先停掉：

```bash
# 找出旧 mock 进程
ps aux | grep mock_server
# kill -9 <pid>
# 或如果是 nohup 起的:
# kill $(cat ~/mock_server.pid 2>/dev/null) 2>/dev/null
```

确认 8000 端口不被旧进程占用：

```bash
ss -tlnp | grep 8000   # 应只看到 uvicorn
```

---

## 完成
