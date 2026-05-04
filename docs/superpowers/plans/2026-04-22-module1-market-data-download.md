# 模块一：行情下载与清洗 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在每个交易日收盘后通过 QMT（xtquant）接口下载当日 A 股全市场日线行情，合并换手率与停牌字段，清洗为规范化 DataFrame 并持久化为 parquet，供模块二推送至服务器。

**Architecture:** 按职责拆分 `connector`（xtquant 连接初始化与 startup check）/ `downloader`（行情下载封装）/ `cleaner`（清洗合并）/ `storage`（parquet 持久化）/ `__main__`（CLI 入口）。共享基础设施 `config` 和 `logging_setup` 放 `src/common/`，后续所有模块复用。严格遵守 v3 历史教训：`xtdata.data_dir` 必须显式设置；`download_history_data` 后 `time.sleep(1)` 再读；只用 `download_history_data`（不带 2）；交易日判断内联；不手动操作 `sys.path`。

**Tech Stack:** Python 3.11, xtquant（仅 Windows 可用），pandas, pyarrow（parquet）, PyYAML, pytest, pytest-mock.

**运行环境说明:** 单元测试全部 mock `xtquant`，可在 Mac 跑；真实运行与集成验证必须在 Windows + QMT 客户端登录环境下进行（见 Task 9）。

---

## 文件结构（本计划涉及的全部文件）

**新建（共享基础设施，后续模块 B-E 复用）：**
- `/Users/mameican/Desktop/server/pyproject.toml` — 项目元信息与依赖
- `/Users/mameican/Desktop/server/.gitignore`
- `/Users/mameican/Desktop/server/config/settings.example.yaml` — 配置模板
- `/Users/mameican/Desktop/server/src/__init__.py`
- `/Users/mameican/Desktop/server/src/common/__init__.py`
- `/Users/mameican/Desktop/server/src/common/config.py` — YAML 配置加载
- `/Users/mameican/Desktop/server/src/common/logging_setup.py` — 日志配置
- `/Users/mameican/Desktop/server/tests/__init__.py`
- `/Users/mameican/Desktop/server/tests/common/__init__.py`
- `/Users/mameican/Desktop/server/tests/common/test_config.py`
- `/Users/mameican/Desktop/server/tests/common/test_logging_setup.py`
- `/Users/mameican/Desktop/server/tests/conftest.py`

**新建（模块一专属）：**
- `/Users/mameican/Desktop/server/src/market_data_download/__init__.py`
- `/Users/mameican/Desktop/server/src/market_data_download/connector.py`
- `/Users/mameican/Desktop/server/src/market_data_download/downloader.py`
- `/Users/mameican/Desktop/server/src/market_data_download/cleaner.py`
- `/Users/mameican/Desktop/server/src/market_data_download/storage.py`
- `/Users/mameican/Desktop/server/src/market_data_download/__main__.py`
- `/Users/mameican/Desktop/server/tests/market_data_download/__init__.py`
- `/Users/mameican/Desktop/server/tests/market_data_download/test_connector.py`
- `/Users/mameican/Desktop/server/tests/market_data_download/test_downloader.py`
- `/Users/mameican/Desktop/server/tests/market_data_download/test_cleaner.py`
- `/Users/mameican/Desktop/server/tests/market_data_download/test_storage.py`
- `/Users/mameican/Desktop/server/tests/market_data_download/test_cli.py`

**新建（Windows 集成冒烟测试文档）：**
- `/Users/mameican/Desktop/server/docs/manual_tests/module1_windows_smoke_test.md`

---

## Task 1: 项目骨架与依赖

**Files:**
- Create: `/Users/mameican/Desktop/server/pyproject.toml`
- Create: `/Users/mameican/Desktop/server/.gitignore`
- Create: `/Users/mameican/Desktop/server/src/__init__.py`
- Create: `/Users/mameican/Desktop/server/tests/__init__.py`
- Create: `/Users/mameican/Desktop/server/tests/conftest.py`

- [ ] **Step 1: 写 `pyproject.toml`**

```toml
[project]
name = "qmt-sim-pipeline"
version = "0.1.0"
description = "QMT 模拟盘交易信号执行管道（本地端）"
requires-python = ">=3.11"
dependencies = [
    "pandas>=2.0",
    "pyarrow>=14.0",
    "PyYAML>=6.0",
    "httpx>=0.25",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4",
    "pytest-mock>=3.12",
    "pytest-cov>=4.1",
]

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra -q"
pythonpath = ["."]

[tool.setuptools.packages.find]
where = ["."]
include = ["src*"]
```

**说明:** 不把 `xtquant` 写进 dependencies — 它随 QMT 客户端安装在 Windows 的 userdata 目录里，Mac 上无此包。运行时 `import xtquant` 由 Windows 的 venv sitecustomize.py 暴露。

- [ ] **Step 2: 写 `.gitignore`**

```
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.coverage
htmlcov/
*.egg-info/
build/
dist/
.venv/
venv/

# 配置文件（含密钥）
config/settings.yaml
config/settings.local.yaml

# 运行期数据
data/
logs/
```

- [ ] **Step 3: 写空的 `__init__.py`**

两份文件内容相同（一行注释）：

```python
# 包标记
```

分别写到：
- `/Users/mameican/Desktop/server/src/__init__.py`
- `/Users/mameican/Desktop/server/tests/__init__.py`

- [ ] **Step 4: 写 `tests/conftest.py`**

```python
"""pytest 共享 fixtures"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    return cfg


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d
```

- [ ] **Step 5: 创建 venv、安装依赖、验证 pytest 可启动**

```bash
cd /Users/mameican/Desktop/server
python3.11 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
pytest --collect-only
```

预期输出末尾：`no tests ran` 或 `collected 0 items`，无 ImportError。

- [ ] **Step 6: Commit**

```bash
cd /Users/mameican/Desktop/server
git init
git add pyproject.toml .gitignore src/__init__.py tests/__init__.py tests/conftest.py
git commit -m "chore: scaffold project layout and pytest setup"
```

---

## Task 2: 共享配置加载（`src/common/config.py`）

**Files:**
- Create: `/Users/mameican/Desktop/server/config/settings.example.yaml`
- Create: `/Users/mameican/Desktop/server/src/common/__init__.py`
- Create: `/Users/mameican/Desktop/server/src/common/config.py`
- Create: `/Users/mameican/Desktop/server/tests/common/__init__.py`
- Create: `/Users/mameican/Desktop/server/tests/common/test_config.py`

- [ ] **Step 1: 写配置模板 `config/settings.example.yaml`**

```yaml
# QMT 模拟盘 Pipeline 配置模板
# 实际使用：cp config/settings.example.yaml config/settings.yaml 后按环境修改

qmt:
  # QMT 客户端 userdata_mini 目录（xtdata.data_dir 必须设置为此路径）
  # Windows 实际路径示例：C:/parttime/平安证券量盈QMT策略交易平台/userdata_mini
  data_dir: "C:/parttime/平安证券量盈QMT策略交易平台/userdata_mini"
  account_id: "REPLACE_ME"

server:
  base_url: "https://example.com"
  api_key: "REPLACE_ME"
  timeout: 30

paths:
  data_root: "./data"
  log_dir: "./logs"
  sqlite_path: "./data/trading.db"

notify:
  wecom_webhook: "REPLACE_ME"

market_data:
  # xtquant 板块名（不是指数代码！）
  sector_name: "沪深A股"
```

- [ ] **Step 2: 写两个空的 `__init__.py`**

内容各一行：

```python
# 包标记
```

分别写到：
- `/Users/mameican/Desktop/server/src/common/__init__.py`
- `/Users/mameican/Desktop/server/tests/common/__init__.py`

- [ ] **Step 3: 先写失败测试 `tests/common/test_config.py`**

```python
"""src.common.config 测试"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.config import Config, load_config


def test_load_config_reads_yaml(tmp_config_dir: Path):
    cfg_file = tmp_config_dir / "settings.yaml"
    cfg_file.write_text(
        """
qmt:
  data_dir: "/tmp/fake_qmt"
  account_id: "ACC123"
server:
  base_url: "https://api.example.com"
  api_key: "KEY123"
  timeout: 15
paths:
  data_root: "/tmp/data"
  log_dir: "/tmp/logs"
  sqlite_path: "/tmp/data/trading.db"
notify:
  wecom_webhook: "https://webhook.example.com"
market_data:
  sector_name: "沪深A股"
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_file)

    assert isinstance(cfg, Config)
    assert cfg.qmt.data_dir == "/tmp/fake_qmt"
    assert cfg.qmt.account_id == "ACC123"
    assert cfg.server.base_url == "https://api.example.com"
    assert cfg.server.timeout == 15
    assert cfg.paths.data_root == "/tmp/data"
    assert cfg.market_data.sector_name == "沪深A股"


def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_load_config_missing_required_key_raises(tmp_config_dir: Path):
    cfg_file = tmp_config_dir / "settings.yaml"
    cfg_file.write_text(
        """
qmt:
  account_id: "ACC"
server:
  base_url: "x"
  api_key: "y"
  timeout: 10
paths:
  data_root: "."
  log_dir: "."
  sqlite_path: "."
notify:
  wecom_webhook: "w"
market_data:
  sector_name: "沪深A股"
""",
        encoding="utf-8",
    )
    with pytest.raises(KeyError):
        load_config(cfg_file)
```

- [ ] **Step 4: 跑测试确认失败**

```bash
source /Users/mameican/Desktop/server/venv/bin/activate
pytest tests/common/test_config.py -v
```

预期：`ModuleNotFoundError: No module named 'src.common.config'`。

- [ ] **Step 5: 实现 `src/common/config.py`**

```python
"""YAML 配置加载与 dataclass 封装。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class QmtConfig:
    data_dir: str
    account_id: str


@dataclass(frozen=True)
class ServerConfig:
    base_url: str
    api_key: str
    timeout: int


@dataclass(frozen=True)
class PathsConfig:
    data_root: str
    log_dir: str
    sqlite_path: str


@dataclass(frozen=True)
class NotifyConfig:
    wecom_webhook: str


@dataclass(frozen=True)
class MarketDataConfig:
    sector_name: str


@dataclass(frozen=True)
class Config:
    qmt: QmtConfig
    server: ServerConfig
    paths: PathsConfig
    notify: NotifyConfig
    market_data: MarketDataConfig


def _require(d: dict[str, Any], key: str) -> Any:
    if key not in d:
        raise KeyError(f"settings.yaml 缺少必填字段: {key}")
    return d[key]


def load_config(path: Path | str) -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")

    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    qmt = _require(raw, "qmt")
    server = _require(raw, "server")
    paths = _require(raw, "paths")
    notify = _require(raw, "notify")
    md = _require(raw, "market_data")

    return Config(
        qmt=QmtConfig(
            data_dir=_require(qmt, "data_dir"),
            account_id=_require(qmt, "account_id"),
        ),
        server=ServerConfig(
            base_url=_require(server, "base_url"),
            api_key=_require(server, "api_key"),
            timeout=int(_require(server, "timeout")),
        ),
        paths=PathsConfig(
            data_root=_require(paths, "data_root"),
            log_dir=_require(paths, "log_dir"),
            sqlite_path=_require(paths, "sqlite_path"),
        ),
        notify=NotifyConfig(
            wecom_webhook=_require(notify, "wecom_webhook"),
        ),
        market_data=MarketDataConfig(
            sector_name=_require(md, "sector_name"),
        ),
    )
```

- [ ] **Step 6: 跑测试确认通过**

```bash
pytest tests/common/test_config.py -v
```

预期：3 个测试全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add config/settings.example.yaml src/common/__init__.py src/common/config.py tests/common/__init__.py tests/common/test_config.py
git commit -m "feat(common): add YAML config loader with dataclass schema"
```

---

## Task 3: 共享日志配置（`src/common/logging_setup.py`）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/common/logging_setup.py`
- Create: `/Users/mameican/Desktop/server/tests/common/test_logging_setup.py`

- [ ] **Step 1: 先写失败测试**

```python
"""src.common.logging_setup 测试"""
from __future__ import annotations

import logging
from pathlib import Path

from src.common.logging_setup import setup_logging


def test_setup_logging_creates_log_dir_and_file(tmp_path: Path):
    log_dir = tmp_path / "logs"

    logger = setup_logging(log_dir=log_dir, module_name="test_module", level="INFO")
    logger.info("hello from test")

    for h in logger.handlers:
        h.flush()

    assert log_dir.exists()
    log_files = list(log_dir.glob("test_module-*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "hello from test" in content


def test_setup_logging_idempotent(tmp_path: Path):
    """重复调用不应重复添加 handler。"""
    log_dir = tmp_path / "logs"
    setup_logging(log_dir=log_dir, module_name="m2", level="INFO")
    setup_logging(log_dir=log_dir, module_name="m2", level="INFO")

    logger = logging.getLogger("m2")
    assert len(logger.handlers) == 2  # file + console
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/common/test_logging_setup.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `src/common/logging_setup.py`**

```python
"""统一日志配置：控制台 + 按日期文件。"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(
    log_dir: Path | str,
    module_name: str,
    level: str = "INFO",
) -> logging.Logger:
    """为指定 module_name 配置日志 handler。幂等。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(module_name)
    logger.setLevel(getattr(logging, level.upper()))

    if logger.handlers:
        return logger

    today = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"{module_name}-{today}.log"

    file_h = logging.FileHandler(log_file, encoding="utf-8")
    file_h.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(file_h)

    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(stream_h)

    logger.propagate = False
    return logger
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/common/test_logging_setup.py -v
```

预期：2 个测试全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/common/logging_setup.py tests/common/test_logging_setup.py
git commit -m "feat(common): add idempotent logging setup with file+console handlers"
```

---

## Task 4: xtquant 连接器与 startup_check

**Files:**
- Create: `/Users/mameican/Desktop/server/src/market_data_download/__init__.py`
- Create: `/Users/mameican/Desktop/server/src/market_data_download/connector.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_download/__init__.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_download/test_connector.py`

**背景:** v3 历史教训第一条——任何 xtquant API 调用前，必须在脚本顶层设置 `xtdata.data_dir`。此模块封装"设置 data_dir + 验证连接"，所有下游模块通过它初始化。

- [ ] **Step 1: 先写两个空的 `__init__.py`**

```python
# 包标记
```

分别写到：
- `/Users/mameican/Desktop/server/src/market_data_download/__init__.py`
- `/Users/mameican/Desktop/server/tests/market_data_download/__init__.py`

- [ ] **Step 2: 写失败测试**

```python
"""connector 测试：mock xtquant"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_xtquant(monkeypatch):
    fake_xtdata = SimpleNamespace(
        data_dir="",
        get_trading_dates=MagicMock(return_value=[]),
    )
    fake_pkg = SimpleNamespace(xtdata=fake_xtdata)
    monkeypatch.setitem(sys.modules, "xtquant", fake_pkg)
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake_xtdata)
    return fake_xtdata


def test_init_xtquant_sets_data_dir(fake_xtquant):
    from src.market_data_download.connector import init_xtquant

    init_xtquant(data_dir="/tmp/fake_qmt")
    assert fake_xtquant.data_dir == "/tmp/fake_qmt"


def test_init_xtquant_empty_data_dir_raises(fake_xtquant):
    from src.market_data_download.connector import init_xtquant

    with pytest.raises(ValueError, match="data_dir"):
        init_xtquant(data_dir="")


def test_startup_check_ok(fake_xtquant):
    from src.market_data_download.connector import startup_check

    fake_xtquant.get_trading_dates.return_value = [
        "20260420", "20260421", "20260422",
    ]

    startup_check(data_dir="/tmp/fake_qmt")
    assert fake_xtquant.data_dir == "/tmp/fake_qmt"


def test_startup_check_trading_dates_empty_raises(fake_xtquant):
    from src.market_data_download.connector import startup_check

    fake_xtquant.get_trading_dates.return_value = []

    with pytest.raises(RuntimeError, match="QMT"):
        startup_check(data_dir="/tmp/fake_qmt")
```

- [ ] **Step 3: 跑测试确认失败**

```bash
pytest tests/market_data_download/test_connector.py -v
```

预期：ImportError。

- [ ] **Step 4: 实现 `connector.py`**

```python
"""xtquant 连接初始化与启动检查。

严格遵守 v3 历史教训：
1. xtdata.data_dir 必须在任何 xtquant 调用之前设置
2. 不手动操作 sys.path（依赖 venv 的 sitecustomize.py 提供 xtquant）
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def init_xtquant(data_dir: str) -> None:
    """设置 xtquant.xtdata.data_dir。必须在任何其他 xtquant 调用之前执行。"""
    if not data_dir:
        raise ValueError("data_dir 不能为空 — 请在 settings.yaml 中配置 qmt.data_dir")

    # 延迟 import：Mac 上测试时 xtquant 由 monkeypatch 注入，生产时由 Windows venv 提供
    from xtquant import xtdata

    xtdata.data_dir = data_dir
    logger.info("xtdata.data_dir 已设置为 %s", data_dir)


def startup_check(data_dir: str) -> None:
    """启动前检查：data_dir 已设置 + QMT 可访问交易日历。

    失败抛异常，调用方负责报警或退出。
    """
    init_xtquant(data_dir)

    from xtquant import xtdata

    dates = xtdata.get_trading_dates("SH", count=5)
    if not dates:
        raise RuntimeError(
            "QMT 连接失败或 data_dir 无数据：get_trading_dates 返回空。"
            "请确认 QMT 客户端已登录且 data_dir 路径正确。"
        )

    logger.info("startup_check 通过，最近 5 个交易日：%s", dates)
```

- [ ] **Step 5: 跑测试确认通过**

```bash
pytest tests/market_data_download/test_connector.py -v
```

预期：4 个测试全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/market_data_download/__init__.py src/market_data_download/connector.py tests/market_data_download/__init__.py tests/market_data_download/test_connector.py
git commit -m "feat(md-download): add xtquant connector with startup_check"
```

---

## Task 5: 行情下载（OHLCV + 换手率 + 停牌）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/market_data_download/downloader.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_download/test_downloader.py`

**v3 历史教训（本任务强制执行）:**
- `download_history_data` 后必须 `time.sleep(1)` 再 `get_market_data`
- 只用 `download_history_data`，不用带 `2` 的版本
- `get_stock_list_in_sector()` 传板块名字符串（如 `"沪深A股"`），不传指数代码
- 交易日判断内联，不单独成模块

**产出:** `download_daily_market_data(trade_date, sector_name) -> dict`，返回：
```python
{
  "trade_date": "20260422",
  "symbols": ["600519.SH", ...],
  "market_data": {field: DataFrame},  # xtdata.get_market_data 的原始返回
}
```

- [ ] **Step 1: 先写失败测试**

```python
"""downloader 测试：mock xtquant"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _fake_market_data_frame(symbols: list[str], date: str, fields: list[str]) -> dict:
    """模拟 xtdata.get_market_data 的返回格式：{field: DataFrame(index=symbol, columns=date)}"""
    defaults = {
        "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
        "volume": 100_000, "amount": 1_020_000.0,
        "turnoverRatio": 0.003, "suspendFlag": 0,
    }
    out = {}
    for f in fields:
        df = pd.DataFrame(index=symbols, columns=[date], dtype="float64")
        for s in symbols:
            df.loc[s, date] = defaults.get(f, 0)
        out[f] = df
    return out


@pytest.fixture
def fake_xtdata(monkeypatch):
    fake = SimpleNamespace(
        data_dir="/tmp/fake",
        get_trading_dates=MagicMock(return_value=["20260421", "20260422"]),
        get_stock_list_in_sector=MagicMock(return_value=["600519.SH", "000001.SZ"]),
        download_history_data=MagicMock(return_value=None),
        get_market_data=MagicMock(),
    )
    fake.get_market_data.side_effect = (
        lambda fields, syms, *_a, **_kw: _fake_market_data_frame(syms, "20260422", fields)
    )
    monkeypatch.setitem(sys.modules, "xtquant", SimpleNamespace(xtdata=fake))
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake)
    return fake


def test_download_happy_path(fake_xtdata):
    from src.market_data_download.downloader import download_daily_market_data

    result = download_daily_market_data(
        trade_date="20260422",
        sector_name="沪深A股",
    )

    assert result["trade_date"] == "20260422"
    assert set(result["symbols"]) == {"600519.SH", "000001.SZ"}
    fake_xtdata.download_history_data.assert_called()
    fake_xtdata.get_stock_list_in_sector.assert_called_with("沪深A股")
    for required in ("open", "high", "low", "close",
                     "volume", "amount", "turnoverRatio", "suspendFlag"):
        assert required in result["market_data"]


def test_download_non_trading_day_raises(fake_xtdata):
    from src.market_data_download.downloader import download_daily_market_data

    fake_xtdata.get_trading_dates.return_value = ["20260421", "20260422"]

    with pytest.raises(ValueError, match="非交易日"):
        download_daily_market_data(trade_date="20260425", sector_name="沪深A股")


def test_download_sleeps_before_reading(fake_xtdata, monkeypatch):
    """v3 历史教训：download 后必须 time.sleep(>=1) 再 get_market_data"""
    from src.market_data_download import downloader as dl_mod

    sleep_calls: list[float] = []
    monkeypatch.setattr(dl_mod.time, "sleep", lambda s: sleep_calls.append(s))

    dl_mod.download_daily_market_data(trade_date="20260422", sector_name="沪深A股")

    assert any(s >= 1 for s in sleep_calls), f"期望至少一次 sleep(>=1)，实际: {sleep_calls}"


def test_download_empty_sector_raises(fake_xtdata):
    from src.market_data_download.downloader import download_daily_market_data

    fake_xtdata.get_stock_list_in_sector.return_value = []

    with pytest.raises(RuntimeError, match="板块"):
        download_daily_market_data(trade_date="20260422", sector_name="不存在的板块")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/market_data_download/test_downloader.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `downloader.py`**

```python
"""全市场日线行情下载。v3 历史教训已内化。"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = [
    "open", "high", "low", "close",
    "volume", "amount",
    "turnoverRatio",
    "suspendFlag",
]


def _is_trading_day(trade_date: str) -> bool:
    """交易日判断内联版：xtdata.get_trading_dates。"""
    from xtquant import xtdata

    dates = xtdata.get_trading_dates("SH", count=30)
    as_str = [str(d)[:8] if not isinstance(d, str) else d for d in dates]
    return trade_date in as_str


def download_daily_market_data(
    trade_date: str,
    sector_name: str,
) -> dict[str, Any]:
    """下载指定交易日的全市场日线行情。

    Args:
        trade_date: YYYYMMDD
        sector_name: 板块名字符串，如 "沪深A股"（不是指数代码！）

    Returns:
        {"trade_date", "symbols", "market_data": {field: DataFrame}}

    Raises:
        ValueError: trade_date 非交易日
        RuntimeError: 板块成分为空
    """
    from xtquant import xtdata

    if not _is_trading_day(trade_date):
        raise ValueError(f"{trade_date} 非交易日，不下载")

    logger.info("拉取板块 %s 的成分股", sector_name)
    symbols = xtdata.get_stock_list_in_sector(sector_name)
    if not symbols:
        raise RuntimeError(f"板块 {sector_name} 返回空，请检查板块名是否正确")
    logger.info("板块 %s 共 %d 只股票", sector_name, len(symbols))

    logger.info("开始 download_history_data（period=1d, date=%s）", trade_date)
    for sym in symbols:
        try:
            xtdata.download_history_data(
                sym,
                period="1d",
                start_time=trade_date,
                end_time=trade_date,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("download_history_data 失败 %s: %s", sym, e)

    # v3 历史教训：download 后必须 sleep(1) 再 get_market_data
    time.sleep(1)

    logger.info("读取 get_market_data，字段 %s", _REQUIRED_FIELDS)
    md = xtdata.get_market_data(
        _REQUIRED_FIELDS,
        symbols,
        period="1d",
        start_time=trade_date,
        end_time=trade_date,
    )

    return {
        "trade_date": trade_date,
        "symbols": symbols,
        "market_data": md,
    }
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/market_data_download/test_downloader.py -v
```

预期：4 个测试全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/market_data_download/downloader.py tests/market_data_download/test_downloader.py
git commit -m "feat(md-download): add daily market data downloader with v3 lessons enforced"
```

---

## Task 6: 清洗合并为规范化 DataFrame

**Files:**
- Create: `/Users/mameican/Desktop/server/src/market_data_download/cleaner.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_download/test_cleaner.py`

**输出 schema（与服务器 `POST /market-data` 字段对齐）:**

| 列 | 类型 | 说明 |
|---|---|---|
| `symbol` | str | `600519.SH` |
| `trade_date` | str | `20260422` |
| `open` | float64 | 开盘价 |
| `high` | float64 | 最高价 |
| `low` | float64 | 最低价 |
| `close` | float64 | 收盘价 |
| `volume` | int64 | 成交量（股） |
| `amount` | float64 | 成交额（元） |
| `turnover_rate` | float64 | 换手率，0~1 |
| `is_suspended` | bool | 是否停牌 |

**清洗规则:**
1. 从 `{field: DataFrame}` 重组为长格式
2. 停牌股（`suspendFlag=1`）：`is_suspended=True`，OHLCV 保留原值
3. 非停牌但 OHLCV 全 0 的行：丢弃（数据缺失）
4. `turnoverRatio` 重命名为 `turnover_rate`
5. volume 转 int64

- [ ] **Step 1: 先写失败测试**

```python
from __future__ import annotations

import math

import pandas as pd
import pytest

from src.market_data_download.cleaner import clean_market_data


def _make_raw(symbols: list[str], date: str, rows: dict[str, dict]) -> dict:
    fields = ["open", "high", "low", "close", "volume", "amount",
              "turnoverRatio", "suspendFlag"]
    md = {}
    for f in fields:
        df = pd.DataFrame(index=symbols, columns=[date], dtype="float64")
        for s in symbols:
            df.loc[s, date] = rows[s].get(f, 0)
        md[f] = df
    return {"trade_date": date, "symbols": symbols, "market_data": md}


def test_clean_basic_two_stocks():
    raw = _make_raw(
        symbols=["600519.SH", "000001.SZ"],
        date="20260422",
        rows={
            "600519.SH": dict(
                open=1520.0, high=1548.0, low=1515.0, close=1540.0,
                volume=12345678, amount=19012345678.0,
                turnoverRatio=0.0032, suspendFlag=0,
            ),
            "000001.SZ": dict(
                open=10.0, high=10.5, low=9.8, close=10.2,
                volume=100_000, amount=1_020_000.0,
                turnoverRatio=0.01, suspendFlag=0,
            ),
        },
    )

    df = clean_market_data(raw)

    assert list(df.columns) == [
        "symbol", "trade_date", "open", "high", "low", "close",
        "volume", "amount", "turnover_rate", "is_suspended",
    ]
    assert len(df) == 2
    row = df.set_index("symbol").loc["600519.SH"]
    assert row["trade_date"] == "20260422"
    assert row["close"] == 1540.0
    assert not bool(row["is_suspended"])
    assert math.isclose(float(row["turnover_rate"]), 0.0032)
    assert df["volume"].dtype.kind in ("i", "u")


def test_clean_suspended_preserves_row():
    raw = _make_raw(
        symbols=["600000.SH"],
        date="20260422",
        rows={
            "600000.SH": dict(
                open=0, high=0, low=0, close=0,
                volume=0, amount=0,
                turnoverRatio=0, suspendFlag=1,
            ),
        },
    )

    df = clean_market_data(raw)

    assert len(df) == 1
    assert bool(df.iloc[0]["is_suspended"])
    assert df.iloc[0]["symbol"] == "600000.SH"


def test_clean_drops_zero_ohlcv_non_suspended():
    """非停牌但 OHLCV 全 0 的行应被丢弃"""
    raw = _make_raw(
        symbols=["GOOD.SH", "GARBAGE.SH"],
        date="20260422",
        rows={
            "GOOD.SH": dict(
                open=10, high=10.5, low=9.8, close=10.2,
                volume=1000, amount=10000.0,
                turnoverRatio=0.01, suspendFlag=0,
            ),
            "GARBAGE.SH": dict(
                open=0, high=0, low=0, close=0,
                volume=0, amount=0,
                turnoverRatio=0, suspendFlag=0,
            ),
        },
    )

    df = clean_market_data(raw)

    assert set(df["symbol"]) == {"GOOD.SH"}


def test_clean_empty_symbols_raises():
    raw = {"trade_date": "20260422", "symbols": [], "market_data": {}}
    with pytest.raises(ValueError, match="空"):
        clean_market_data(raw)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/market_data_download/test_cleaner.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `cleaner.py`**

```python
"""将 downloader 返回的原始结构清洗为规范化长格式 DataFrame。"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_OUTPUT_COLUMNS = [
    "symbol", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "turnover_rate", "is_suspended",
]


def clean_market_data(raw: dict[str, Any]) -> pd.DataFrame:
    """清洗合并 downloader 的返回。

    Returns: 10 列长格式 DataFrame。
    """
    trade_date = raw["trade_date"]
    symbols = raw["symbols"]
    md = raw["market_data"]

    if not symbols:
        raise ValueError("market_data 为空，无数据可清洗")

    def _col(field: str) -> pd.Series:
        df = md.get(field, pd.DataFrame())
        if df.empty or trade_date not in df.columns:
            return pd.Series(dtype="float64", index=symbols)
        return df[trade_date]

    df = pd.DataFrame({
        "symbol": symbols,
        "trade_date": trade_date,
        "open": _col("open").reindex(symbols).astype("float64").values,
        "high": _col("high").reindex(symbols).astype("float64").values,
        "low": _col("low").reindex(symbols).astype("float64").values,
        "close": _col("close").reindex(symbols).astype("float64").values,
        "volume": _col("volume").reindex(symbols).fillna(0).astype("int64").values,
        "amount": _col("amount").reindex(symbols).astype("float64").values,
        "turnover_rate": _col("turnoverRatio").reindex(symbols).astype("float64").values,
        "is_suspended": (
            _col("suspendFlag").reindex(symbols).fillna(0).astype("int64") == 1
        ).values,
    })

    before = len(df)
    ohlcv_zero = (
        (df["open"] == 0) & (df["high"] == 0) & (df["low"] == 0) & (df["close"] == 0)
    )
    df = df.loc[~(ohlcv_zero & ~df["is_suspended"])].reset_index(drop=True)
    dropped = before - len(df)
    if dropped:
        logger.info("丢弃 %d 条非停牌 0 值行", dropped)

    df = df[_OUTPUT_COLUMNS]
    logger.info("清洗完成：%d 行 × %d 列", len(df), len(df.columns))
    return df
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/market_data_download/test_cleaner.py -v
```

预期：4 个测试全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/market_data_download/cleaner.py tests/market_data_download/test_cleaner.py
git commit -m "feat(md-download): add cleaner producing canonical market-data DataFrame"
```

---

## Task 7: Parquet 持久化

**Files:**
- Create: `/Users/mameican/Desktop/server/src/market_data_download/storage.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_download/test_storage.py`

**输出路径:** `{data_root}/market_data/{trade_date}.parquet`，存在则覆盖。

- [ ] **Step 1: 先写失败测试**

```python
from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.market_data_download.storage import save_market_data_parquet


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["600519.SH"],
        "trade_date": ["20260422"],
        "open": [1520.0],
        "high": [1548.0],
        "low": [1515.0],
        "close": [1540.0],
        "volume": [12345678],
        "amount": [19012345678.0],
        "turnover_rate": [0.0032],
        "is_suspended": [False],
    })


def test_save_creates_file(tmp_path: Path):
    df = _sample_df()

    out_path = save_market_data_parquet(df, trade_date="20260422", data_root=tmp_path)

    assert out_path == tmp_path / "market_data" / "20260422.parquet"
    assert out_path.exists()

    loaded = pd.read_parquet(out_path)
    pd.testing.assert_frame_equal(loaded, df)


def test_save_overwrites_existing(tmp_path: Path):
    df1 = _sample_df()
    df2 = _sample_df().assign(close=[9999.0])

    save_market_data_parquet(df1, trade_date="20260422", data_root=tmp_path)
    out2 = save_market_data_parquet(df2, trade_date="20260422", data_root=tmp_path)

    loaded = pd.read_parquet(out2)
    assert loaded["close"].iloc[0] == 9999.0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/market_data_download/test_storage.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `storage.py`**

```python
"""Parquet 持久化。"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def save_market_data_parquet(
    df: pd.DataFrame,
    trade_date: str,
    data_root: Path | str,
) -> Path:
    """保存清洗后的行情到 parquet：{data_root}/market_data/{trade_date}.parquet"""
    data_root = Path(data_root)
    out_dir = data_root / "market_data"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / f"{trade_date}.parquet"
    df.to_parquet(out_path, engine="pyarrow", index=False)
    logger.info("行情写入 %s（%d 行）", out_path, len(df))
    return out_path
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/market_data_download/test_storage.py -v
```

预期：2 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/market_data_download/storage.py tests/market_data_download/test_storage.py
git commit -m "feat(md-download): persist cleaned market data to parquet"
```

---

## Task 8: CLI 入口

**Files:**
- Create: `/Users/mameican/Desktop/server/src/market_data_download/__main__.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_download/test_cli.py`

**用法:**

```bash
python -m src.market_data_download --date 20260422 --config config/settings.yaml
```

**退出码:**
- `0` 成功
- `1` 配置/参数错误
- `2` xtquant 调用失败（含非交易日、板块为空）
- `3` 清洗后无有效数据

- [ ] **Step 1: 先写失败测试**

```python
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _write_cfg(path: Path, data_root: str) -> Path:
    cfg = path / "settings.yaml"
    cfg.write_text(
        f"""
qmt:
  data_dir: "/tmp/fake_qmt"
  account_id: "ACC"
server:
  base_url: "https://x"
  api_key: "K"
  timeout: 10
paths:
  data_root: "{data_root}"
  log_dir: "{data_root}/logs"
  sqlite_path: "{data_root}/trading.db"
notify:
  wecom_webhook: "w"
market_data:
  sector_name: "沪深A股"
""",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def fake_xtdata(monkeypatch):
    fake = SimpleNamespace(
        data_dir="",
        get_trading_dates=MagicMock(return_value=["20260422"]),
        get_stock_list_in_sector=MagicMock(return_value=["600519.SH"]),
        download_history_data=MagicMock(return_value=None),
        get_market_data=MagicMock(),
    )

    def _gmd(fields, syms, *_a, **_kw):
        defaults = {
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 1000, "amount": 10000.0,
            "turnoverRatio": 0.01, "suspendFlag": 0,
        }
        out = {}
        for f in fields:
            df = pd.DataFrame(index=syms, columns=["20260422"], dtype="float64")
            for s in syms:
                df.loc[s, "20260422"] = defaults[f]
            out[f] = df
        return out

    fake.get_market_data.side_effect = _gmd
    monkeypatch.setitem(sys.modules, "xtquant", SimpleNamespace(xtdata=fake))
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake)
    return fake


def test_cli_happy_path(fake_xtdata, tmp_path: Path):
    from src.market_data_download.__main__ import main

    data_root = tmp_path / "data"
    cfg = _write_cfg(tmp_path, str(data_root))

    exit_code = main(["--date", "20260422", "--config", str(cfg)])

    assert exit_code == 0
    assert (data_root / "market_data" / "20260422.parquet").exists()


def test_cli_missing_config_file(tmp_path: Path):
    from src.market_data_download.__main__ import main

    exit_code = main(["--date", "20260422", "--config", str(tmp_path / "nope.yaml")])

    assert exit_code == 1


def test_cli_non_trading_day(fake_xtdata, tmp_path: Path):
    from src.market_data_download.__main__ import main

    fake_xtdata.get_trading_dates.return_value = ["20260421"]
    data_root = tmp_path / "data"
    cfg = _write_cfg(tmp_path, str(data_root))

    exit_code = main(["--date", "20260425", "--config", str(cfg)])

    assert exit_code == 2


def test_cli_missing_date_arg(tmp_path: Path):
    from src.market_data_download.__main__ import main

    with pytest.raises(SystemExit):
        main(["--config", str(tmp_path / "x.yaml")])
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/market_data_download/test_cli.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `__main__.py`**

```python
"""CLI：python -m src.market_data_download --date YYYYMMDD --config path"""
from __future__ import annotations

import argparse
import sys

from src.common.config import load_config
from src.common.logging_setup import setup_logging
from src.market_data_download.cleaner import clean_market_data
from src.market_data_download.connector import startup_check
from src.market_data_download.downloader import download_daily_market_data
from src.market_data_download.storage import save_market_data_parquet


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.market_data_download",
        description="下载并清洗当日行情，写入 parquet。",
    )
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(
        log_dir=cfg.paths.log_dir,
        module_name="market_data_download",
    )
    logger.info("开始执行，trade_date=%s", args.date)

    try:
        startup_check(data_dir=cfg.qmt.data_dir)
        raw = download_daily_market_data(
            trade_date=args.date,
            sector_name=cfg.market_data.sector_name,
        )
    except (RuntimeError, ValueError) as e:
        logger.error("下载阶段失败: %s", e)
        return 2

    try:
        df = clean_market_data(raw)
    except ValueError as e:
        logger.error("清洗后无有效数据: %s", e)
        return 3

    out_path = save_market_data_parquet(
        df, trade_date=args.date, data_root=cfg.paths.data_root,
    )
    logger.info("完成，输出 %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/market_data_download/test_cli.py -v
```

预期：4 个测试 PASS。

- [ ] **Step 5: 全回归**

```bash
pytest -v
```

预期：config + logging + connector + downloader + cleaner + storage + cli 全绿。

- [ ] **Step 6: Commit**

```bash
git add src/market_data_download/__main__.py tests/market_data_download/test_cli.py
git commit -m "feat(md-download): add CLI gluing download→clean→save"
```

---

## Task 9: Windows 集成冒烟测试文档

**Files:**
- Create: `/Users/mameican/Desktop/server/docs/manual_tests/module1_windows_smoke_test.md`

**说明:** 单元测试 mock xtquant 通过 ≠ 真实跑通。必须在 Windows 下用真实 QMT 验一次。

- [ ] **Step 1: 写集成冒烟测试文档**

内容：

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
mkdir -p /Users/mameican/Desktop/server/docs/manual_tests
git add docs/manual_tests/module1_windows_smoke_test.md
git commit -m "docs: add Windows smoke test checklist for module 1"
```

---

## 收尾清单

- [ ] 所有 Task 的 commit 已完成
- [ ] `pytest -v` 全绿
- [ ] Windows 集成冒烟测试通过（Task 9 文档）
- [ ] 通知搭档：模块一就绪，可开始联调 `POST /market-data`（模块二）

---

## 后续计划

- 模块二（行情推送）：`docs/superpowers/plans/2026-04-22-module2-market-data-push.md`
- 模块三（信号查询与制单）：`docs/superpowers/plans/2026-04-22-module3-signal-query-and-order-prep.md`
- 模块四（竞价下单）：`docs/superpowers/plans/2026-04-22-module4-auction-order-submission.md`
- 模块五+六（成交回报）：`docs/superpowers/plans/2026-04-22-module5-6-trade-result-reporting.md`
