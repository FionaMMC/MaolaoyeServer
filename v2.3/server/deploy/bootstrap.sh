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
