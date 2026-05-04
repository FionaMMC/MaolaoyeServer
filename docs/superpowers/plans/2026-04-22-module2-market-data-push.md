# 模块二：行情推送 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将模块一输出的 parquet 行情按《API接口文档（纯股）》的 `POST /market-data` 规范推送到服务器，失败自动重试 3 次，仍失败则企业微信报警。推送成功后记录响应到日志。

**Architecture:** 新增共享基础设施 `src/common/http_client.py`（带 Bearer 鉴权的 httpx Client 工厂，后续 C/E 模块复用）和 `src/common/notify.py`（企业微信 webhook 推送，后续所有模块复用）。模块二专属：`payload.py`（从 parquet 构造 JSON）、`pusher.py`（POST + 重试）、`__main__.py`（CLI 编排）。重试用固定间隔的退避，不引入第三方 retry 库。

**Tech Stack:** Python 3.11, httpx (同步 Client), pandas, pytest, `httpx.MockTransport`（HTTP 单元测试，无需起本地服务器）。

**前置:** 完成 Plan A（模块一）后再开始本 Plan。本 Plan 复用 Plan A 已建立的：
- `pyproject.toml`、venv、`src/common/config.py`、`src/common/logging_setup.py`
- 模块一的输出文件路径：`{data_root}/market_data/{YYYYMMDD}.parquet`

---

## 文件结构

**新建（共享基础设施，后续 Plan C/D/E 复用）：**
- `/Users/mameican/Desktop/server/src/common/http_client.py`
- `/Users/mameican/Desktop/server/src/common/notify.py`
- `/Users/mameican/Desktop/server/tests/common/test_http_client.py`
- `/Users/mameican/Desktop/server/tests/common/test_notify.py`

**新建（模块二专属）：**
- `/Users/mameican/Desktop/server/src/market_data_push/__init__.py`
- `/Users/mameican/Desktop/server/src/market_data_push/payload.py`
- `/Users/mameican/Desktop/server/src/market_data_push/pusher.py`
- `/Users/mameican/Desktop/server/src/market_data_push/__main__.py`
- `/Users/mameican/Desktop/server/tests/market_data_push/__init__.py`
- `/Users/mameican/Desktop/server/tests/market_data_push/test_payload.py`
- `/Users/mameican/Desktop/server/tests/market_data_push/test_pusher.py`
- `/Users/mameican/Desktop/server/tests/market_data_push/test_cli.py`

**新建（集成冒烟测试文档）：**
- `/Users/mameican/Desktop/server/docs/manual_tests/module2_push_smoke_test.md`

---

## Task 1: 共享 HTTP Client（带 Bearer 鉴权）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/common/http_client.py`
- Create: `/Users/mameican/Desktop/server/tests/common/test_http_client.py`

**产出:** `new_http_client(base_url, api_key, timeout) -> httpx.Client`，返回的 Client 预置：
- `base_url` 去尾 `/`
- Header `Authorization: Bearer {api_key}`
- Header `Content-Type: application/json`
- `timeout` 秒超时

- [ ] **Step 1: 先写失败测试**

```python
"""src.common.http_client 测试"""
from __future__ import annotations

import httpx

from src.common.http_client import new_http_client


def test_client_has_bearer_auth_header():
    client = new_http_client("https://api.example.com", "KEY123", timeout=10)
    assert client.headers["Authorization"] == "Bearer KEY123"
    assert client.headers["Content-Type"] == "application/json"


def test_client_strips_trailing_slash_from_base_url():
    client = new_http_client("https://api.example.com/", "K", timeout=10)
    assert str(client.base_url).rstrip("/") == "https://api.example.com"


def test_client_timeout_is_set():
    client = new_http_client("https://api.example.com", "K", timeout=15)
    assert client.timeout.connect == 15
    assert client.timeout.read == 15


def test_client_sends_auth_on_real_request():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("Authorization")
        captured["path"] = req.url.path
        return httpx.Response(200, json={"code": 0, "message": "ok", "data": {}})

    transport = httpx.MockTransport(handler)
    client = new_http_client("https://api.example.com", "KEY123", timeout=10,
                             transport=transport)
    resp = client.post("/market-data", json={"ok": True})
    assert resp.status_code == 200
    assert captured["auth"] == "Bearer KEY123"
    assert captured["path"] == "/market-data"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
source /Users/mameican/Desktop/server/venv/bin/activate
pytest tests/common/test_http_client.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `src/common/http_client.py`**

```python
"""共享 HTTP Client：httpx 同步 Client，带 Bearer 鉴权。"""
from __future__ import annotations

import httpx


def new_http_client(
    base_url: str,
    api_key: str,
    timeout: int,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """构造预置鉴权与超时的 httpx Client。

    Args:
        transport: 测试时传入 httpx.MockTransport；生产环境传 None。
    """
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
        transport=transport,
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/common/test_http_client.py -v
```

预期：4 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
cd /Users/mameican/Desktop/server
git add src/common/http_client.py tests/common/test_http_client.py
git commit -m "feat(common): add httpx client factory with Bearer auth"
```

---

## Task 2: 共享企业微信通知

**Files:**
- Create: `/Users/mameican/Desktop/server/src/common/notify.py`
- Create: `/Users/mameican/Desktop/server/tests/common/test_notify.py`

**产出:** `notify_wecom(webhook, message, level) -> bool`，成功返回 True，失败返回 False 但不抛异常（通知失败不该阻塞主流程）。

**企业微信 webhook 协议（简化版）：**
```
POST {webhook_url}
Body: {"msgtype": "text", "text": {"content": "..."}}
```

`level` 仅影响消息前缀：`alert` 加 `[报警]`，`info` 无前缀。

- [ ] **Step 1: 先写失败测试**

```python
"""src.common.notify 测试"""
from __future__ import annotations

import httpx

from src.common.notify import notify_wecom


def test_notify_success_returns_true():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    transport = httpx.MockTransport(handler)

    ok = notify_wecom(
        webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xx",
        message="行情推送成功",
        level="info",
        transport=transport,
    )

    assert ok is True
    assert captured["body"]["msgtype"] == "text"
    assert captured["body"]["text"]["content"] == "行情推送成功"


def test_notify_alert_level_prefixes_message():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={"errcode": 0})

    transport = httpx.MockTransport(handler)
    notify_wecom("https://x", "行情推送失败", level="alert", transport=transport)
    assert captured["body"]["text"]["content"].startswith("[报警]")


def test_notify_http_error_returns_false_no_raise():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    transport = httpx.MockTransport(handler)

    ok = notify_wecom("https://x", "hi", level="info", transport=transport)
    assert ok is False


def test_notify_network_exception_returns_false_no_raise():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    transport = httpx.MockTransport(handler)

    ok = notify_wecom("https://x", "hi", level="info", transport=transport)
    assert ok is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/common/test_notify.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `src/common/notify.py`**

```python
"""企业微信 webhook 推送。失败静默（仅记日志）。"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def notify_wecom(
    webhook: str,
    message: str,
    level: str = "info",
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """向企业微信机器人 webhook 推送文本消息。

    Args:
        level: "info"（提示）或 "alert"（报警，消息前加 [报警] 前缀）

    Returns: True=推送成功，False=失败。不抛异常。
    """
    content = f"[报警] {message}" if level == "alert" else message
    body = {"msgtype": "text", "text": {"content": content}}

    try:
        with httpx.Client(timeout=10, transport=transport) as client:
            resp = client.post(webhook, json=body)
            if resp.status_code != 200:
                logger.warning("wecom 推送 HTTP %s: %s", resp.status_code, resp.text)
                return False
            data = resp.json()
            if data.get("errcode", 0) != 0:
                logger.warning("wecom 推送失败 errcode=%s errmsg=%s",
                               data.get("errcode"), data.get("errmsg"))
                return False
            return True
    except httpx.HTTPError as e:
        logger.warning("wecom 推送异常: %s", e)
        return False
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/common/test_notify.py -v
```

预期：4 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/common/notify.py tests/common/test_notify.py
git commit -m "feat(common): add wecom webhook notifier with silent failure"
```

---

## Task 3: Payload 构造器（parquet → API JSON）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/market_data_push/__init__.py`
- Create: `/Users/mameican/Desktop/server/src/market_data_push/payload.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_push/__init__.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_push/test_payload.py`

**API 合约（参考《API接口文档（纯股）》接口一）:**

```json
{
  "trade_date": "20260422",
  "stocks": [
    {
      "symbol": "600519.SH",
      "open": 1520.0,
      "high": 1548.0,
      "low": 1515.0,
      "close": 1540.0,
      "volume": 12345678,
      "amount": 19012345678.0,
      "turnover_rate": 0.0032,
      "is_suspended": false
    }
  ]
}
```

**产出:** `build_market_data_payload(parquet_path, trade_date) -> dict`

- [ ] **Step 1: 写两个空 `__init__.py`**

```python
# 包标记
```

写到：
- `/Users/mameican/Desktop/server/src/market_data_push/__init__.py`
- `/Users/mameican/Desktop/server/tests/market_data_push/__init__.py`

- [ ] **Step 2: 先写失败测试**

```python
"""market_data_push.payload 测试"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.market_data_push.payload import build_market_data_payload


def _write_parquet(path: Path, rows: list[dict]) -> Path:
    df = pd.DataFrame(rows)
    df["volume"] = df["volume"].astype("int64")
    df["is_suspended"] = df["is_suspended"].astype("bool")
    out = path / "20260422.parquet"
    df.to_parquet(out, index=False)
    return out


def test_build_payload_basic(tmp_path: Path):
    pq = _write_parquet(tmp_path, [
        dict(symbol="600519.SH", trade_date="20260422",
             open=1520.0, high=1548.0, low=1515.0, close=1540.0,
             volume=12345678, amount=19012345678.0,
             turnover_rate=0.0032, is_suspended=False),
        dict(symbol="000001.SZ", trade_date="20260422",
             open=10.0, high=10.5, low=9.8, close=10.2,
             volume=100000, amount=1020000.0,
             turnover_rate=0.01, is_suspended=False),
    ])

    payload = build_market_data_payload(pq, trade_date="20260422")

    assert payload["trade_date"] == "20260422"
    assert isinstance(payload["stocks"], list)
    assert len(payload["stocks"]) == 2

    s0 = payload["stocks"][0]
    assert set(s0.keys()) == {
        "symbol", "open", "high", "low", "close",
        "volume", "amount", "turnover_rate", "is_suspended",
    }
    assert isinstance(s0["volume"], int)
    assert isinstance(s0["amount"], float)
    assert isinstance(s0["is_suspended"], bool)
    assert s0["symbol"] == "600519.SH"


def test_build_payload_rejects_mismatched_trade_date(tmp_path: Path):
    pq = _write_parquet(tmp_path, [
        dict(symbol="600519.SH", trade_date="20260422",
             open=1.0, high=1.0, low=1.0, close=1.0,
             volume=100, amount=100.0,
             turnover_rate=0.001, is_suspended=False),
    ])

    with pytest.raises(ValueError, match="trade_date"):
        build_market_data_payload(pq, trade_date="20260421")


def test_build_payload_empty_rows_raises(tmp_path: Path):
    df = pd.DataFrame(columns=[
        "symbol", "trade_date", "open", "high", "low", "close",
        "volume", "amount", "turnover_rate", "is_suspended",
    ])
    out = tmp_path / "20260422.parquet"
    df.to_parquet(out, index=False)

    with pytest.raises(ValueError, match="空"):
        build_market_data_payload(out, trade_date="20260422")
```

- [ ] **Step 3: 跑测试确认失败**

```bash
pytest tests/market_data_push/test_payload.py -v
```

预期：ImportError。

- [ ] **Step 4: 实现 `src/market_data_push/payload.py`**

```python
"""将模块一输出的 parquet 构造为 POST /market-data JSON。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def build_market_data_payload(
    parquet_path: Path | str,
    trade_date: str,
) -> dict[str, Any]:
    """读取 parquet，构造符合 POST /market-data 的 JSON 字典。

    Raises:
        FileNotFoundError: parquet 不存在
        ValueError: trade_date 与 parquet 内容不一致 或 parquet 为空
    """
    parquet_path = Path(parquet_path)
    if not parquet_path.exists():
        raise FileNotFoundError(f"parquet 不存在: {parquet_path}")

    df = pd.read_parquet(parquet_path)
    if df.empty:
        raise ValueError("parquet 空，无数据可推送")

    # 校验所有行的 trade_date 一致且等于期望值
    dates = df["trade_date"].unique()
    if set(dates) != {trade_date}:
        raise ValueError(
            f"parquet 的 trade_date 与期望不一致：期望 {trade_date}，实际 {sorted(dates)}"
        )

    stocks: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        stocks.append({
            "symbol": str(row.symbol),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": int(row.volume),
            "amount": float(row.amount),
            "turnover_rate": float(row.turnover_rate),
            "is_suspended": bool(row.is_suspended),
        })

    logger.info("构造 payload: %d 条 stocks，trade_date=%s", len(stocks), trade_date)
    return {"trade_date": trade_date, "stocks": stocks}
```

- [ ] **Step 5: 跑测试确认通过**

```bash
pytest tests/market_data_push/test_payload.py -v
```

预期：3 个测试 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/market_data_push/__init__.py src/market_data_push/payload.py tests/market_data_push/__init__.py tests/market_data_push/test_payload.py
git commit -m "feat(md-push): add payload builder from parquet to API JSON"
```

---

## Task 4: Pusher 与重试逻辑

**Files:**
- Create: `/Users/mameican/Desktop/server/src/market_data_push/pusher.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_push/test_pusher.py`

**产出:** `push_market_data(payload, http_client, max_retries=3, backoff=2) -> PushResult`

`PushResult` 是 dataclass：
- `ok: bool`
- `attempts: int`
- `response_data: dict | None` — 服务器 `data` 字段
- `error: str | None` — 最后一次失败的简短描述

**重试策略:**
- 网络异常 / HTTP 5xx / `code` 非 0：重试
- HTTP 4xx（含 1001/1002 鉴权/参数错误）：不重试，立即失败
- 每次重试间隔 `backoff * attempt` 秒（简单线性退避）
- `received_count=0` 视为失败

- [ ] **Step 1: 先写失败测试**

```python
"""pusher 测试（用 httpx.MockTransport 模拟服务器）"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.common.http_client import new_http_client
from src.market_data_push.pusher import push_market_data


def _mk_client(responses: list, base_url: str = "https://srv") -> httpx.Client:
    """responses 是一个 callable 列表，按顺序返回。"""
    it = iter(responses)

    def handler(req: httpx.Request) -> httpx.Response:
        fn = next(it)
        return fn(req)

    return new_http_client(base_url, "KEY", timeout=10, transport=httpx.MockTransport(handler))


def _ok_response(received: int = 100) -> httpx.Response:
    return httpx.Response(200, json={
        "code": 0, "message": "ok",
        "data": {"trade_date": "20260422", "received_count": received,
                 "strategy_triggered": True},
    })


def test_push_happy_path_first_attempt():
    client = _mk_client([lambda req: _ok_response(3)])
    payload = {"trade_date": "20260422", "stocks": [{}, {}, {}]}

    result = push_market_data(payload, client, max_retries=3, backoff=0)

    assert result.ok is True
    assert result.attempts == 1
    assert result.response_data["received_count"] == 3
    assert result.error is None


def test_push_retries_on_5xx(monkeypatch):
    # 前两次 500，第三次 200
    client = _mk_client([
        lambda req: httpx.Response(500, text="bad gateway"),
        lambda req: httpx.Response(500, text="bad gateway"),
        lambda req: _ok_response(1),
    ])
    payload = {"trade_date": "20260422", "stocks": [{}]}

    # 关掉 sleep 加速测试
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data(payload, client, max_retries=3, backoff=0)

    assert result.ok is True
    assert result.attempts == 3


def test_push_does_not_retry_on_4xx(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(401, json={"code": 1001, "message": "auth"}),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is False
    assert result.attempts == 1
    assert "1001" in (result.error or "") or "401" in (result.error or "")


def test_push_retries_on_non_zero_code(monkeypatch):
    """服务器 200 但 code 非 0（非 4xx 语义的失败）也重试。"""
    client = _mk_client([
        lambda req: httpx.Response(200, json={"code": 5000, "message": "server err"}),
        lambda req: httpx.Response(200, json={"code": 5000, "message": "server err"}),
        lambda req: _ok_response(1),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is True
    assert result.attempts == 3


def test_push_exhausts_retries_then_fails(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
        lambda req: httpx.Response(500),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is False
    assert result.attempts == 3
    assert result.error is not None


def test_push_network_exception_is_retried(monkeypatch):
    attempts = []

    def handler(req):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("no route")
        return _ok_response(1)

    client = new_http_client("https://srv", "K", timeout=10,
                             transport=httpx.MockTransport(handler))
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is True
    assert result.attempts == 3


def test_push_received_count_zero_is_failure(monkeypatch):
    client = _mk_client([
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"received_count": 0, "trade_date": "20260422"}}),
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"received_count": 0, "trade_date": "20260422"}}),
        lambda req: httpx.Response(200, json={
            "code": 0, "data": {"received_count": 0, "trade_date": "20260422"}}),
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = push_market_data({"trade_date": "20260422", "stocks": [{}]},
                              client, max_retries=3, backoff=0)

    assert result.ok is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/market_data_push/test_pusher.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `src/market_data_push/pusher.py`**

```python
"""行情推送与重试。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ENDPOINT = "/market-data"


@dataclass
class PushResult:
    ok: bool
    attempts: int
    response_data: dict[str, Any] | None
    error: str | None


def push_market_data(
    payload: dict[str, Any],
    http_client: httpx.Client,
    max_retries: int = 3,
    backoff: int = 2,
) -> PushResult:
    """推送行情，网络异常 / 5xx / code!=0 重试，4xx 立即失败。"""
    last_err: str | None = None

    for attempt in range(1, max_retries + 1):
        logger.info("POST %s 第 %d/%d 次尝试", _ENDPOINT, attempt, max_retries)
        try:
            resp = http_client.post(_ENDPOINT, json=payload)
        except httpx.HTTPError as e:
            last_err = f"network error: {e}"
            logger.warning("第 %d 次网络异常: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        if 400 <= resp.status_code < 500:
            try:
                body = resp.json()
                last_err = f"HTTP {resp.status_code} code={body.get('code')} message={body.get('message')}"
            except Exception:  # noqa: BLE001
                last_err = f"HTTP {resp.status_code} (body not JSON)"
            logger.error("4xx 不重试：%s", last_err)
            return PushResult(ok=False, attempts=attempt,
                              response_data=None, error=last_err)

        if resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code}"
            logger.warning("第 %d 次 5xx: %s", attempt, last_err)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        # 2xx
        try:
            body = resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = f"response not JSON: {e}"
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        code = body.get("code")
        if code != 0:
            last_err = f"biz code={code} message={body.get('message')}"
            logger.warning("第 %d 次业务失败: %s", attempt, last_err)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        data = body.get("data") or {}
        received = int(data.get("received_count", 0))
        if received <= 0:
            last_err = "received_count=0（服务器未入库任何数据）"
            logger.warning("第 %d 次 received_count=0", attempt)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            continue

        logger.info("推送成功：received_count=%d, strategy_triggered=%s",
                    received, data.get("strategy_triggered"))
        return PushResult(ok=True, attempts=attempt,
                          response_data=data, error=None)

    return PushResult(ok=False, attempts=max_retries,
                      response_data=None, error=last_err)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/market_data_push/test_pusher.py -v
```

预期：7 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/market_data_push/pusher.py tests/market_data_push/test_pusher.py
git commit -m "feat(md-push): add pusher with retry (5xx/network/code!=0) and 4xx fast-fail"
```

---

## Task 5: CLI 编排（读 parquet → push → 通知）

**Files:**
- Create: `/Users/mameican/Desktop/server/src/market_data_push/__main__.py`
- Create: `/Users/mameican/Desktop/server/tests/market_data_push/test_cli.py`

**用法:**

```bash
python -m src.market_data_push --date 20260422 --config config/settings.yaml
```

**退出码:**
- `0` 推送成功
- `1` 配置/参数错误
- `2` parquet 读取/校验失败
- `3` 推送失败（重试耗尽，已发报警）

**行为:**
- 读取 `{data_root}/market_data/{date}.parquet` 构造 payload
- POST 推送；成功发 info 级微信通知，失败发 alert 级报警

- [ ] **Step 1: 先写失败测试**

```python
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd
import pytest


def _write_cfg(tmp: Path, data_root: str) -> Path:
    p = tmp / "settings.yaml"
    p.write_text(f"""
qmt:
  data_dir: "/tmp/fake"
  account_id: "ACC"
server:
  base_url: "https://srv.example.com"
  api_key: "KEY"
  timeout: 10
paths:
  data_root: "{data_root}"
  log_dir: "{data_root}/logs"
  sqlite_path: "{data_root}/trading.db"
notify:
  wecom_webhook: "https://wecom.example.com/hook"
market_data:
  sector_name: "沪深A股"
""", encoding="utf-8")
    return p


def _write_parquet(data_root: Path, date: str, n: int = 2) -> Path:
    mdir = data_root / "market_data"
    mdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(n):
        rows.append(dict(
            symbol=f"{600000 + i}.SH", trade_date=date,
            open=10.0, high=10.5, low=9.8, close=10.2,
            volume=1000, amount=10000.0,
            turnover_rate=0.01, is_suspended=False,
        ))
    df = pd.DataFrame(rows)
    df["volume"] = df["volume"].astype("int64")
    df["is_suspended"] = df["is_suspended"].astype("bool")
    out = mdir / f"{date}.parquet"
    df.to_parquet(out, index=False)
    return out


def test_cli_happy_path(tmp_path: Path, monkeypatch):
    from src.market_data_push import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_parquet(data_root, "20260422", n=3)

    fake_server_calls = []

    def server_handler(req: httpx.Request) -> httpx.Response:
        fake_server_calls.append(str(req.url))
        return httpx.Response(200, json={
            "code": 0, "data": {"trade_date": "20260422",
                                "received_count": 3,
                                "strategy_triggered": True}})

    wecom_calls = []

    def wecom_handler(req: httpx.Request) -> httpx.Response:
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    # 让 __main__ 使用带 MockTransport 的 client / wecom
    monkeypatch.setattr(
        cli_mod, "_new_server_client",
        lambda cfg: cli_mod.new_http_client(
            cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
            transport=httpx.MockTransport(server_handler)),
    )
    monkeypatch.setattr(
        cli_mod, "_notify",
        lambda webhook, msg, level: cli_mod.notify_wecom(
            webhook, msg, level, transport=httpx.MockTransport(wecom_handler)),
    )

    exit_code = cli_mod.main(["--date", "20260422", "--config", str(cfg)])

    assert exit_code == 0
    assert len(fake_server_calls) == 1
    assert any("/market-data" in u for u in fake_server_calls)
    assert len(wecom_calls) == 1
    assert "成功" in wecom_calls[0]["text"]["content"]


def test_cli_missing_parquet(tmp_path: Path, monkeypatch):
    from src.market_data_push import __main__ as cli_mod

    cfg = _write_cfg(tmp_path, str(tmp_path / "data"))
    # 不写 parquet

    exit_code = cli_mod.main(["--date", "20260422", "--config", str(cfg)])
    assert exit_code == 2


def test_cli_push_failed_triggers_alert(tmp_path: Path, monkeypatch):
    from src.market_data_push import __main__ as cli_mod

    data_root = tmp_path / "data"
    data_root.mkdir()
    cfg = _write_cfg(tmp_path, str(data_root))
    _write_parquet(data_root, "20260422", n=2)

    def server_handler(req):
        return httpx.Response(500, text="boom")

    wecom_calls = []

    def wecom_handler(req):
        import json
        wecom_calls.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr(
        cli_mod, "_new_server_client",
        lambda cfg: cli_mod.new_http_client(
            cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
            transport=httpx.MockTransport(server_handler)),
    )
    monkeypatch.setattr(
        cli_mod, "_notify",
        lambda webhook, msg, level: cli_mod.notify_wecom(
            webhook, msg, level, transport=httpx.MockTransport(wecom_handler)),
    )
    monkeypatch.setattr("time.sleep", lambda s: None)

    exit_code = cli_mod.main(["--date", "20260422", "--config", str(cfg)])

    assert exit_code == 3
    assert len(wecom_calls) == 1
    assert wecom_calls[0]["text"]["content"].startswith("[报警]")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/market_data_push/test_cli.py -v
```

预期：ImportError。

- [ ] **Step 3: 实现 `src/market_data_push/__main__.py`**

```python
"""CLI：python -m src.market_data_push --date YYYYMMDD --config path"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.common.config import Config, load_config
from src.common.http_client import new_http_client
from src.common.logging_setup import setup_logging
from src.common.notify import notify_wecom
from src.market_data_push.payload import build_market_data_payload
from src.market_data_push.pusher import push_market_data


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.market_data_push",
        description="读取 parquet 并推送到服务器 /market-data。",
    )
    p.add_argument("--date", required=True, help="交易日 YYYYMMDD")
    p.add_argument("--config", required=True, help="settings.yaml 路径")
    return p


def _new_server_client(cfg: Config):
    """可被测试 monkeypatch 替换以注入 MockTransport。"""
    return new_http_client(
        cfg.server.base_url, cfg.server.api_key, cfg.server.timeout,
    )


def _notify(webhook: str, message: str, level: str) -> bool:
    """可被测试 monkeypatch 替换。"""
    return notify_wecom(webhook, message, level)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    logger = setup_logging(cfg.paths.log_dir, "market_data_push")
    logger.info("开始推送 trade_date=%s", args.date)

    parquet_path = Path(cfg.paths.data_root) / "market_data" / f"{args.date}.parquet"
    try:
        payload = build_market_data_payload(parquet_path, trade_date=args.date)
    except (FileNotFoundError, ValueError) as e:
        logger.error("parquet 校验失败: %s", e)
        _notify(cfg.notify.wecom_webhook,
                f"行情推送前校验失败 ({args.date}): {e}", level="alert")
        return 2

    with _new_server_client(cfg) as client:
        result = push_market_data(payload, client, max_retries=3, backoff=2)

    if result.ok:
        received = (result.response_data or {}).get("received_count")
        logger.info("推送完成：received_count=%s", received)
        _notify(cfg.notify.wecom_webhook,
                f"行情推送成功 ({args.date})：{received} 条", level="info")
        return 0

    logger.error("推送最终失败：%s", result.error)
    _notify(cfg.notify.wecom_webhook,
            f"行情推送失败 ({args.date})：{result.error}（已重试 {result.attempts} 次）",
            level="alert")
    return 3


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/market_data_push/test_cli.py -v
```

预期：3 个测试 PASS。

- [ ] **Step 5: 全回归**

```bash
pytest -v
```

预期：Plan A 和 Plan B 所有测试全绿。

- [ ] **Step 6: Commit**

```bash
git add src/market_data_push/__main__.py tests/market_data_push/test_cli.py
git commit -m "feat(md-push): add CLI orchestrating parquet read→push→notify"
```

---

## Task 6: Windows 集成冒烟测试文档

**Files:**
- Create: `/Users/mameican/Desktop/server/docs/manual_tests/module2_push_smoke_test.md`

- [ ] **Step 1: 写文档**

内容：

```markdown
# 模块二 Windows 集成冒烟测试

**前置条件:**
1. 模块一已跑通，`data\market_data\20260422.parquet` 存在
2. 服务器 `/market-data` 接口已就绪（搭档告知）
3. `config/settings.yaml` 的 `server.base_url` 和 `server.api_key` 已填正确
4. `config/settings.yaml` 的 `notify.wecom_webhook` 已填企业微信机器人 URL

**执行步骤（Windows PowerShell）:**

```powershell
cd C:\parttime\qmt模拟盘pipeline\server
C:\parttime\qmt数据推送\venv\Scripts\activate
python -m src.market_data_push --date 20260422 --config config\settings.yaml
```

**验收清单:**

- [ ] 退出码为 0
- [ ] 日志显示 `POST /market-data 第 1/3 次尝试` 和 `推送成功：received_count=...`
- [ ] 企业微信群收到"行情推送成功 (20260422)：N 条"提示
- [ ] 服务器侧确认收到数据（与搭档核对 received_count 一致）

**重试 & 报警验证（故意断网）:**

1. 断开机器网络，或把 `server.base_url` 改成 `https://nonexistent.example.com`
2. 重跑命令
3. 预期：日志显示 3 次网络异常；企业微信收到"[报警] 行情推送失败..."；退出码 3

**4xx 快速失败验证:**

1. 把 `server.api_key` 改成错误值
2. 重跑命令
3. 预期：日志显示 1 次 4xx，不重试；企业微信收到 alert；退出码 3

**幂等验证:**

1. 成功推送后立即再推一次同一日期
2. 预期：服务器返回 `code=2001`（日期重复）时视为非 0 business code，会重试 3 次，最终发 alert——这是 **预期行为**，服务器方可改为 `code=0, received_count=<原值>` 视作幂等成功
```

- [ ] **Step 2: Commit**

```bash
git add docs/manual_tests/module2_push_smoke_test.md
git commit -m "docs: add smoke test checklist for module 2 (push)"
```

---

## 收尾清单

- [ ] 所有 Task commit 完成
- [ ] `pytest -v` 全绿（覆盖 Plan A + Plan B）
- [ ] Windows 集成冒烟测试通过
- [ ] 与搭档确认服务器能收到 `POST /market-data` 数据
- [ ] 记录一次成功推送的 trace（日志 + 企业微信截图）供日后对账

---

## 后续计划

- 模块三（信号查询与制单）：`docs/superpowers/plans/2026-04-22-module3-signal-query-and-order-prep.md`
- 模块四（竞价下单）：`docs/superpowers/plans/2026-04-22-module4-auction-order-submission.md`
- 模块五+六（成交回报）：`docs/superpowers/plans/2026-04-22-module5-6-trade-result-reporting.md`
