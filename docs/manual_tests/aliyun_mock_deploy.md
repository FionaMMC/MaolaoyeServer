# 阿里云 ECS 部署 Mock Server 步骤

> **目的**：在搭档真实策略服务上线前，先把 `scripts/mock_server.py` 部署到阿里云 ECS，
> 解锁本端模块二/三/五/六的端到端联测。
> **执行人**：搭档（云服务器拥有者）。本文档由本端开发者交付给搭档执行。

---

## 0. 前置信息

需要从搭档侧确认：

- [ ] 阿里云 ECS 实例公网 IP：`__________`
- [ ] SSH 登录账号：通常 `root`，部分实例是 `ecs-user`
- [ ] 操作系统：Ubuntu / CentOS / Alibaba Linux（影响包管理命令）

---

## 1. SSH 登录

```bash
ssh root@<公网IP>
```

---

## 2. 安装 Python 3.11 + 依赖

**Ubuntu/Debian**:
```bash
apt update
apt install -y python3.11 python3-pip
```

**CentOS / Alibaba Linux 3**:
```bash
yum install -y python3.11 python3-pip
```

如果 `python3.11` 不可用，`python3.10`/`python3.9` 也行（FastAPI 兼容到 3.8+），
但与本端 Python 3.11 一致最稳。

---

## 3. 上传 mock 代码

**方式 A — 用 git（推荐）**

如果项目已经有远程仓库：
```bash
cd /root
git clone <远程仓库地址> qmt-pipeline
cd qmt-pipeline
pip3 install -r scripts/requirements-mock.txt
```

**方式 B — scp 单文件**

本端 Mac 执行：
```bash
scp /Users/mameican/Desktop/server/scripts/mock_server.py \
    /Users/mameican/Desktop/server/scripts/requirements-mock.txt \
    root@<公网IP>:/root/
```
云服务器执行：
```bash
cd /root
pip3 install -r requirements-mock.txt
```

---

## 4. 阿里云控制台开放安全组（**最容易遗漏**）

1. 登录 [阿里云控制台](https://ecs.console.aliyun.com/)
2. 进入「云服务器 ECS」→ 找到该实例
3. 「网络与安全」→「安全组」→ 选中实例所在的安全组 → 「配置规则」
4. 「入方向」→「手动添加」：

| 字段 | 值 |
|---|---|
| 授权策略 | 允许 |
| 优先级 | 1 |
| 协议类型 | 自定义 TCP |
| 端口范围 | 8000/8000 |
| 授权对象 | `0.0.0.0/0`（联测期间放开；上线时收紧到本端固定 IP） |
| 描述 | qmt mock |

不开这一步，外面 curl 永远不通。

---

## 5. 内部防火墙（CentOS / Alibaba Linux 才需要）

```bash
firewall-cmd --permanent --add-port=8000/tcp
firewall-cmd --reload
```

Ubuntu 默认 ufw 关闭，跳过此步。

---

## 6. 启动 mock server

### 临时跑（关 ssh 就停，仅用于首次验证）

```bash
cd /root  # 或 /root/qmt-pipeline，取决于上面用了哪种上传方式
uvicorn mock_server:app --host 0.0.0.0 --port 8000
# 如果是 git clone 方式，模块路径是 scripts.mock_server
# uvicorn scripts.mock_server:app --host 0.0.0.0 --port 8000
```

看到 `Uvicorn running on http://0.0.0.0:8000` 即就绪。

### 后台跑（断开 ssh 也活，nohup 简单方案）

```bash
nohup uvicorn mock_server:app --host 0.0.0.0 --port 8000 \
    > mock_server.log 2>&1 &
echo $! > mock_server.pid
```

停止：`kill $(cat mock_server.pid)`

### systemd 跑（最稳，重启自动起，推荐用于持续联测期间）

```bash
cat > /etc/systemd/system/qmt-mock.service <<'EOF'
[Unit]
Description=QMT Pipeline Mock Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/root
ExecStart=/usr/bin/uvicorn mock_server:app --host 0.0.0.0 --port 8000
Restart=on-failure
StandardOutput=append:/var/log/qmt-mock.log
StandardError=append:/var/log/qmt-mock.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable qmt-mock
systemctl start qmt-mock
systemctl status qmt-mock     # 应显示 active (running)
```

---

## 7. 外部连通性验证（在本端 Mac 执行）

```bash
# 健康检查（不用 token）
curl http://<公网IP>:8000/_health
# 期望: {"ok":true,"stored":{"market_data":0,"trade_results":0}}

# 鉴权 + 业务接口（需要 Bearer）
curl -H "Authorization: Bearer TEST_KEY_123" \
     "http://<公网IP>:8000/signals?date=20260424"
# 期望: 返回一条 signal_id=mock-20260424-001 的信号

# 推送测试（鉴权失败会 401）
curl -X POST -H "Authorization: Bearer WRONG" \
     -H "Content-Type: application/json" \
     -d '{"trade_date":"20260423","stocks":[]}' \
     http://<公网IP>:8000/market-data
# 期望: 401，{"code":1001,...}
```

三条都符合预期就部署完成。

---

## 8. 本端 settings.yaml 配置

```yaml
server:
  base_url: "http://<公网IP>:8000"
  api_key: "TEST_KEY_123"
  timeout: 30
```

---

## 9. 看实时日志

```bash
# nohup 方式
tail -f /root/mock_server.log

# systemd 方式
journalctl -u qmt-mock -f
# 或 tail -f /var/log/qmt-mock.log
```

联测时本端发请求，云上立刻能看到 `POST /market-data trade_date=... stocks=...` 的日志条目。

---

## 10. 真实策略上线后切换

搭档真实服务部署完毕后：

```bash
systemctl stop qmt-mock
systemctl disable qmt-mock
# 启动真实服务...
```

本端 `settings.yaml` 的 `base_url` 不变（同一台机同一端口），只把 `api_key` 换成搭档发的真实 key。

---

## 排错速查

| 现象 | 原因 |
|---|---|
| `curl: Connection refused` | mock 没起；或者绑了 127.0.0.1 而不是 0.0.0.0 |
| `curl: Connection timed out` | 安全组没放 8000 / firewalld 没开 |
| `curl` 通但本端连不上 | 本端机器在受限网络，或 base_url 协议写错（http/https） |
| 401 | Bearer token 不对，对照 `EXPECTED_API_KEY` |
| 502 / 504（如果搭档加了 nginx） | 上游 mock 进程挂了，看 systemctl status |
