# Plan 13: 阿里云部署 — 替换 v1 mock，上线 v2.3 server

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 写一份完整、可复制粘贴照做的阿里云 ECS 部署指南，把 v1 协议的 mock_server.py 干掉，换成 v2.3 server。同时配套 systemd unit、bootstrap 脚本、健康检查。

**Architecture:**
- 阿里云 ECS Ubuntu 22.04+
- Python 3.11 venv at `/opt/qmt-server/venv/`
- 代码 git clone 自 GitHub repo（与 client 共仓）→ 仅启用 `v2.3/server/`
- systemd 守护 uvicorn
- 安全组放行 8000（或挂 nginx 80/443 反代，可选）
- Parquet 数据 rsync 自 Windows client（首次 bootstrap）

**Files (新建到 v2.3/server/deploy/)：**
- `v2.3/server/deploy/qmt-server.service` (NEW, systemd unit template)
- `v2.3/server/deploy/bootstrap.sh` (NEW, 一键安装脚本)
- `v2.3/server/deploy/nginx-example.conf` (NEW, 反代示例，可选)
- `v2.3/server/deploy/README.md` (NEW, 完整部署步骤)

---

## Task 1: systemd unit 模板

### `v2.3/server/deploy/qmt-server.service`

```ini
[Unit]
Description=QMT Pipeline Server v2.3
Documentation=https://github.com/FionaMMC/MaolaoyeServer
After=network.target

[Service]
Type=simple
User=qmtserver
Group=qmtserver
WorkingDirectory=/opt/qmt-server/v2.3/server
EnvironmentFile=/opt/qmt-server/v2.3/server/.env
ExecStart=/opt/qmt-server/venv/bin/uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 2 \
    --log-level info
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/qmt-server/server.log
StandardError=append:/var/log/qmt-server/server.log

[Install]
WantedBy=multi-user.target
```

---

## Task 2: bootstrap 一键脚本

### `v2.3/server/deploy/bootstrap.sh`

```bash
#!/usr/bin/env bash
# QMT Pipeline Server v2.3 一键 bootstrap 脚本（阿里云 ECS Ubuntu 22.04+）
# 使用方法：
#   1. 用 root 登录 ECS
#   2. curl -fsSL <git raw url>/v2.3/server/deploy/bootstrap.sh | bash
#   或先 git clone 到 /opt/qmt-server/，然后 bash deploy/bootstrap.sh

set -euo pipefail

INSTALL_ROOT="/opt/qmt-server"
SERVICE_USER="qmtserver"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
GIT_URL="${GIT_URL:-git@github.com:FionaMMC/MaolaoyeServer.git}"
LOG_DIR="/var/log/qmt-server"

echo "==> 检查前置依赖"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "缺少 $PYTHON_BIN，安装中..."
    apt update && apt install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt update && apt install -y "$PYTHON_BIN" "$PYTHON_BIN-venv"
}
command -v git >/dev/null 2>&1 || apt install -y git

echo "==> 创建运行用户 $SERVICE_USER"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin "$SERVICE_USER"

echo "==> 准备目录 $INSTALL_ROOT 和 $LOG_DIR"
mkdir -p "$INSTALL_ROOT" "$LOG_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_ROOT" "$LOG_DIR"

echo "==> 拉代码到 $INSTALL_ROOT"
if [ ! -d "$INSTALL_ROOT/.git" ]; then
    sudo -u "$SERVICE_USER" git clone "$GIT_URL" "$INSTALL_ROOT"
else
    sudo -u "$SERVICE_USER" git -C "$INSTALL_ROOT" pull
fi

cd "$INSTALL_ROOT/v2.3/server"

echo "==> 建 venv + 装依赖"
sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv "$INSTALL_ROOT/venv"
sudo -u "$SERVICE_USER" "$INSTALL_ROOT/venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_ROOT/venv/bin/pip" install -e ".[dev]"

echo "==> 创建 .env 模板（如不存在）"
if [ ! -f .env ]; then
    sudo -u "$SERVICE_USER" cp .env.example .env
    echo ""
    echo "⚠️  请编辑 $INSTALL_ROOT/v2.3/server/.env 填写真实 QMT_API_KEY 等配置"
    echo ""
fi

echo "==> 初始化 SQLite + Parquet 目录"
sudo -u "$SERVICE_USER" mkdir -p "$INSTALL_ROOT/v2.3/server/data" \
                                    "$INSTALL_ROOT/v2.3/server/plugins"
# init_db 会在 server 第一次启动时自动建表（dependencies.py 里 lru_cache）

echo "==> 安装 systemd unit"
cp "$INSTALL_ROOT/v2.3/server/deploy/qmt-server.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable qmt-server

echo ""
echo "✅ bootstrap 完成。"
echo ""
echo "下一步："
echo "  1. 编辑 $INSTALL_ROOT/v2.3/server/.env 填配置"
echo "  2. (可选) rsync Windows client 的 Parquet 数据到 $INSTALL_ROOT/v2.3/server/data/"
echo "  3. systemctl start qmt-server"
echo "  4. systemctl status qmt-server"
echo "  5. 阿里云控制台开 8000 入方向安全组"
echo "  6. 验证: curl http://<公网IP>:8000/healthz"
```

---

## Task 3: nginx 反代（可选）

### `v2.3/server/deploy/nginx-example.conf`

```nginx
# nginx 反代示例：把外部 80/443 转到内部 uvicorn 8000
# /etc/nginx/sites-available/qmt-server.conf
# 软链到 sites-enabled 后 nginx -t && systemctl reload nginx

server {
    listen 80;
    server_name your-domain.example.com;

    # 强制 HTTPS（如有证书）
    # return 301 https://$server_name$request_uri;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120;
    }

    access_log /var/log/nginx/qmt-server.access.log;
    error_log  /var/log/nginx/qmt-server.error.log;
}

# HTTPS 版本（用 certbot 签 Let's Encrypt 后）
# server {
#     listen 443 ssl http2;
#     server_name your-domain.example.com;
#     ssl_certificate /etc/letsencrypt/live/your-domain/fullchain.pem;
#     ssl_certificate_key /etc/letsencrypt/live/your-domain/privkey.pem;
#     # ... location / 同上
# }
```

---

## Task 4: 完整部署 README

### `v2.3/server/deploy/README.md`

```markdown
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

## 10. 切换 nginx + HTTPS（可选，上线推荐）

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
```

---

## 验证 + Commit

这个 plan 没单测（纯部署文档 + shell 脚本 + systemd unit）。验证：

```bash
# 检查 bash 语法
bash -n /Users/mameican/Desktop/server/v2.3/server/deploy/bootstrap.sh
echo "OK"
```

```bash
cd /Users/mameican/Desktop/server
chmod +x v2.3/server/deploy/bootstrap.sh
git add v2.3/server/deploy/
git commit -m "docs(server): add Aliyun deploy guide + systemd unit + bootstrap (Plan 13)"
```

---

## 收尾

- [ ] `deploy/bootstrap.sh` 语法检查通过
- [ ] 1 commit
- [ ] **v2.3 server 14 个 plan 全部完成 🎉**

---

## 整个 v2.3 server 总览

```
v2.3/server/
├── pyproject.toml + .env.example + .gitignore
├── app/
│   ├── main.py + settings.py + logging_setup.py + db.py
│   ├── exceptions.py + auth.py + dependencies.py
│   ├── api/ {health, market_data, orders, trade_result, admin}.py
│   ├── models/ {6 ORM tables}
│   ├── schemas/ {common, market_data, orders, trade_result}.py
│   ├── services/ {ingest, orders_queue, settlement, precheck, aggregate, perf}.py
│   ├── storage/parquet.py
│   ├── strategy/ {base, context, loader, runner}.py
│   └── scheduler/ {pipeline, runtime}.py
├── plugins/ {README, _example_buy_threshold}
├── strategies.yaml
├── tests/ unit/ {142+ tests}
└── deploy/ {qmt-server.service, bootstrap.sh, nginx-example.conf, README.md}
```
