# Plan 01: Scaffold v2.3/server/ — pyproject + Settings + logging + healthz

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox `- [ ]` syntax.

**Goal:** 起 server 端骨架。pyproject + venv + Settings (pydantic-settings) + structlog + FastAPI app with `/healthz` + `/readyz`。完成后 `python -m app.main` 能起服务，`curl /healthz` 返回 200。

**Architecture:** 新建独立 venv `v2.3/server/venv/`，server 的依赖跟 client 隔离，跟生产部署一致。FastAPI app 用 lifespan 管理资源；Settings 类从环境变量+`.env` 加载；logging 用 structlog 输出 JSON 行（生产）+ console (开发)。

**Tech Stack:** Python 3.11 / FastAPI / pydantic-settings / structlog / uvicorn / pytest / httpx (TestClient)

**Files this plan touches:**
```
v2.3/server/
├── pyproject.toml             # NEW
├── .env.example               # NEW
├── .gitignore                 # NEW (server-local, e.g. pipeline-server.db)
├── app/
│   ├── __init__.py            # NEW
│   ├── settings.py            # NEW
│   ├── logging_setup.py       # NEW
│   ├── main.py                # NEW
│   └── api/
│       ├── __init__.py        # NEW
│       └── health.py          # NEW
└── tests/
    ├── __init__.py            # NEW
    ├── conftest.py            # NEW
    └── unit/
        ├── __init__.py        # NEW
        ├── test_settings.py   # NEW
        ├── test_logging.py    # NEW
        └── test_health.py     # NEW
```

---

## Task 1: 项目骨架 (pyproject + venv + .gitignore + 空 __init__.py)

**Files:**
- `v2.3/server/pyproject.toml`
- `v2.3/server/.env.example`
- `v2.3/server/.gitignore`
- `v2.3/server/app/__init__.py`
- `v2.3/server/app/api/__init__.py`
- `v2.3/server/tests/__init__.py`
- `v2.3/server/tests/unit/__init__.py`

- [ ] **Step 1**: 写 `v2.3/server/pyproject.toml`

```toml
[project]
name = "qmt-server"
version = "2.3.0"
description = "QMT 模拟盘 Pipeline 服务器端"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "sqlalchemy>=2.0",
    "pyarrow>=14.0",
    "pandas>=2.0",
    "structlog>=24.1",
    "apscheduler>=3.10",
    "pyyaml>=6.0",
    "python-multipart>=0.0.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4.1",
    "httpx>=0.25",
    "ruff>=0.4",
]

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra -q"
asyncio_mode = "auto"
pythonpath = ["."]

[tool.setuptools.packages.find]
where = ["."]
include = ["app*"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

- [ ] **Step 2**: 写 `v2.3/server/.env.example`

```bash
# === Server 端配置模板 ===
# 实际使用：cp .env.example .env 后按环境填值

# HTTP / 鉴权
QMT_API_KEY=TEST_KEY_123
QMT_HOST=0.0.0.0
QMT_PORT=8000

# 业务数据
QMT_DB_URL=sqlite:///./pipeline-server.db
QMT_PARQUET_ROOT=./data

# 策略
QMT_PLUGINS_DIR=./plugins
QMT_STRATEGIES_FILE=../strategies.yaml

# 日志
QMT_LOG_LEVEL=INFO
QMT_LOG_JSON=false   # true=JSON，部署时打开
```

- [ ] **Step 3**: 写 `v2.3/server/.gitignore`

```
__pycache__/
*.pyc
*.egg-info/
.pytest_cache/
.coverage
htmlcov/
venv/
.venv/

# 配置 (含密钥)
.env
.env.local

# 运行期数据
pipeline-server.db
pipeline-server.db-journal
logs/
data/  # Parquet 仓库由 rsync bootstrap 而来；不入库
```

- [ ] **Step 4**: 写 5 个空 `__init__.py`

```python
# 包标记
```

写到：`app/`, `app/api/`, `tests/`, `tests/unit/`，加上 `app/__init__.py`。

- [ ] **Step 5**: 建独立 venv + 安装依赖

```bash
cd /Users/mameican/Desktop/server/v2.3/server
python3.11 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
pytest --collect-only
```

预期：`no tests collected`（还没写测试）+ 无 ImportError。

- [ ] **Step 6**: Commit

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/pyproject.toml v2.3/server/.env.example v2.3/server/.gitignore \
        v2.3/server/app/__init__.py v2.3/server/app/api/__init__.py \
        v2.3/server/tests/__init__.py v2.3/server/tests/unit/__init__.py
git commit -m "chore(server): scaffold v2.3/server/ project layout and venv"
```

---

## Task 2: Settings (`app/settings.py`)

**Files:**
- `v2.3/server/app/settings.py`
- `v2.3/server/tests/unit/test_settings.py`

**产出:** `Settings` 类（pydantic-settings），从环境变量 `QMT_*` 加载，缺省值与 `.env.example` 一致。`get_settings()` 用 lru_cache 单例。

- [ ] **Step 1**: 写测试

```python
"""tests/unit/test_settings.py"""
import os
import pytest

from app.settings import Settings, get_settings


def _clear_qmt_env(monkeypatch):
    """清掉所有 QMT_* 环境变量，便于测试默认值。"""
    for k in list(os.environ.keys()):
        if k.startswith("QMT_"):
            monkeypatch.delenv(k)


def test_settings_default_values(monkeypatch):
    _clear_qmt_env(monkeypatch)
    s = Settings()
    assert s.api_key == ""
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.db_url.startswith("sqlite")
    assert s.log_level == "INFO"
    assert s.log_json is False


def test_settings_from_env(monkeypatch):
    _clear_qmt_env(monkeypatch)
    monkeypatch.setenv("QMT_API_KEY", "PROD_KEY_xyz")
    monkeypatch.setenv("QMT_PORT", "9000")
    monkeypatch.setenv("QMT_LOG_JSON", "true")
    s = Settings()
    assert s.api_key == "PROD_KEY_xyz"
    assert s.port == 9000
    assert s.log_json is True


def test_get_settings_is_cached():
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
```

- [ ] **Step 2**: 跑测试，确认 ImportError

```bash
source venv/bin/activate
pytest tests/unit/test_settings.py -v
```

- [ ] **Step 3**: 实现 `app/settings.py`

```python
"""Server 端配置：环境变量 QMT_* + .env 文件。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有 server 端配置集中在此类。

    优先级：环境变量 > .env > 默认值。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="QMT_",
        extra="ignore",
        case_sensitive=False,
    )

    # HTTP / 鉴权
    api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8000

    # 业务数据
    db_url: str = "sqlite:///./pipeline-server.db"
    parquet_root: Path = Path("./data")

    # 策略
    plugins_dir: Path = Path("./plugins")
    strategies_file: Path = Path("../strategies.yaml")

    # 日志
    log_level: str = "INFO"
    log_json: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例，整个进程共用一份配置。"""
    return Settings()
```

- [ ] **Step 4**: 跑测试，确认 3 个 PASS

- [ ] **Step 5**: Commit

```bash
git add v2.3/server/app/settings.py v2.3/server/tests/unit/test_settings.py
git commit -m "feat(server): add Settings class via pydantic-settings"
```

---

## Task 3: Logging (`app/logging_setup.py`)

**Files:**
- `v2.3/server/app/logging_setup.py`
- `v2.3/server/tests/unit/test_logging.py`

**产出:** `setup_logging(log_level: str, json_output: bool)` 配置 structlog；`get_logger(name)` 返回带绑定的 logger。

- [ ] **Step 1**: 写测试

```python
"""tests/unit/test_logging.py"""
import io
import json
import logging

import pytest
import structlog

from app.logging_setup import get_logger, setup_logging


def test_setup_logging_json_mode(capsys):
    setup_logging(log_level="INFO", json_output=True)
    log = get_logger("test_module")
    log.info("hello", trade_date="20260430", count=3)
    captured = capsys.readouterr()
    # JSON 模式下每行应该是合法 JSON
    line = captured.out.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["event"] == "hello"
    assert parsed["trade_date"] == "20260430"
    assert parsed["count"] == 3
    assert parsed["logger"] == "test_module"


def test_setup_logging_console_mode(capsys):
    setup_logging(log_level="INFO", json_output=False)
    log = get_logger("test_console")
    log.info("greeting", name="alice")
    captured = capsys.readouterr()
    out = captured.out
    assert "greeting" in out
    assert "alice" in out


def test_log_level_filtering(capsys):
    setup_logging(log_level="WARNING", json_output=True)
    log = get_logger("test_level")
    log.info("filtered_out")
    log.warning("kept")
    captured = capsys.readouterr()
    out = captured.out
    assert "filtered_out" not in out
    assert "kept" in out
```

- [ ] **Step 2**: 跑测试，ImportError

- [ ] **Step 3**: 实现 `app/logging_setup.py`

```python
"""structlog 配置：JSON（生产）或 ConsoleRenderer（开发）。"""
from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(log_level: str = "INFO", json_output: bool = False) -> None:
    """初始化全局 logging + structlog。可重复调用，会重置 handler。"""
    level = getattr(logging, log_level.upper())

    # 清空根 logger 的旧 handler
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    # 标准 logging → stdout
    handler = logging.StreamHandler(sys.stdout)
    root.addHandler(handler)
    root.setLevel(level)

    # structlog 处理器链
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,  # 测试需要每次重配
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
```

- [ ] **Step 4**: 跑测试，确认 3 个 PASS

- [ ] **Step 5**: Commit

```bash
git add v2.3/server/app/logging_setup.py v2.3/server/tests/unit/test_logging.py
git commit -m "feat(server): add structlog setup with JSON/console modes"
```

---

## Task 4: Health 端点 (`app/api/health.py` + `app/main.py`)

**Files:**
- `v2.3/server/app/main.py`
- `v2.3/server/app/api/health.py`
- `v2.3/server/tests/conftest.py`
- `v2.3/server/tests/unit/test_health.py`

**产出:**
- `GET /healthz` 200 → `{"status": "ok"}` (liveness, 永远成功)
- `GET /readyz` 200 → `{"status": "ready", "checks": {...}}` (readiness, 检查 parquet_root 存在 + db 可连)

行业惯例：liveness 用于 lb/k8s 重启检查，永远应该返回 OK；readiness 用于流量切入判断，依赖未就绪时返回 503。

- [ ] **Step 1**: 写 conftest

```python
"""tests/conftest.py — 共享 fixtures"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings, get_settings


@pytest.fixture
def settings_for_test(tmp_path: Path) -> Settings:
    """每个测试一个隔离的 Settings（tmp 路径）。"""
    s = Settings(
        api_key="TEST_KEY",
        db_url=f"sqlite:///{tmp_path}/test.db",
        parquet_root=tmp_path / "data",
        plugins_dir=tmp_path / "plugins",
        strategies_file=tmp_path / "strategies.yaml",
        log_level="WARNING",  # 测试时安静
        log_json=False,
    )
    s.parquet_root.mkdir(parents=True, exist_ok=True)
    s.plugins_dir.mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture
def client(settings_for_test, monkeypatch) -> TestClient:
    """FastAPI TestClient + override settings。"""
    get_settings.cache_clear()
    app = create_app(settings_override=settings_for_test)
    return TestClient(app)
```

- [ ] **Step 2**: 写测试

```python
"""tests/unit/test_health.py"""


def test_healthz_always_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_when_dependencies_ok(client, settings_for_test):
    # parquet_root 在 fixture 里已经创建好
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert "checks" in body
    assert body["checks"]["parquet_root"] == "ok"


def test_readyz_503_when_parquet_missing(client, settings_for_test):
    # 删除 parquet_root 模拟未就绪
    import shutil
    shutil.rmtree(settings_for_test.parquet_root)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["parquet_root"] != "ok"
```

- [ ] **Step 3**: 跑测试 → ImportError

- [ ] **Step 4**: 实现 `app/api/health.py`

```python
"""Health 端点：liveness + readiness。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.settings import Settings, get_settings

router = APIRouter()


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    """Liveness probe — 进程在跑就 OK。永远不查依赖。"""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Readiness probe — 检查依赖是否就绪，未就绪返回 503。"""
    checks = {}

    # Parquet 仓库根目录
    checks["parquet_root"] = "ok" if settings.parquet_root.exists() else (
        f"missing: {settings.parquet_root}"
    )

    all_ok = all(v == "ok" for v in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "checks": checks}

    return {"status": "ready", "checks": checks}
```

- [ ] **Step 5**: 实现 `app/main.py`

```python
"""FastAPI 入口：create_app + lifespan + router 注册。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.api import health
from app.logging_setup import get_logger, setup_logging
from app.settings import Settings, get_settings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用启停时的资源管理。"""
    log = get_logger("app")
    log.info("server_starting", version="2.3.0")
    yield
    log.info("server_stopping")


def create_app(settings_override: Settings | None = None) -> FastAPI:
    """工厂函数。测试时传 settings_override 注入隔离配置。"""
    if settings_override is not None:
        # 测试用：替换 cached settings
        get_settings.cache_clear()
        # 让 Depends(get_settings) 拿到 override 的值
        # 简单做法：直接覆盖 lru_cache 的内部
        get_settings.__wrapped__ = lambda: settings_override  # type: ignore
        get_settings.cache_clear()

    settings = get_settings() if settings_override is None else settings_override
    setup_logging(log_level=settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title="QMT Pipeline Server",
        version="2.3.0",
        lifespan=_lifespan,
    )
    app.include_router(health.router, tags=["health"])

    return app


# uvicorn 入口：uvicorn app.main:app --host 0.0.0.0 --port 8000
app = create_app()


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
```

- [ ] **Step 6**: 跑测试，3 个 PASS

```bash
pytest tests/unit/test_health.py -v
```

- [ ] **Step 7**: 手动起服务跑一下

```bash
source venv/bin/activate
mkdir -p data plugins
python -m app.main &
SERVER_PID=$!
sleep 1
curl -s http://localhost:8000/healthz
echo ""
curl -s http://localhost:8000/readyz
echo ""
curl -s http://localhost:8000/docs | head -5  # auto-doc OK?
kill $SERVER_PID
```

预期：`/healthz` → `{"status":"ok"}`，`/readyz` → `{"status":"ready",...}`。

- [ ] **Step 8**: 全套回归 + commit

```bash
pytest -v   # 期望: 9 passed (3 settings + 3 logging + 3 health)
git add v2.3/server/app/main.py v2.3/server/app/api/health.py \
        v2.3/server/tests/conftest.py v2.3/server/tests/unit/test_health.py
git commit -m "feat(server): add FastAPI app skeleton with /healthz + /readyz"
```

---

## 收尾

- [ ] `pytest` 全绿（9 个测试）
- [ ] `python -m app.main` 能起服务
- [ ] `curl /healthz` `/readyz` `/docs` 都正常响应
- [ ] 4 个 commit:
  1. chore(server): scaffold project layout
  2. feat(server): Settings via pydantic-settings
  3. feat(server): structlog setup
  4. feat(server): FastAPI app + healthz/readyz

---

## 后续 plan

- 02: Parquet storage layer (`app/storage/parquet.py`) — 支持行情入库 plan 05
- 03: SQLite + ORM models (`app/models/*`)
- 04: API skeleton + auth + 3 端点 stub
