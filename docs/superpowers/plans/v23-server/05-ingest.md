# Plan 05: Ingest 服务 — POST /market-data 真实业务

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 把 `/market-data` 从 stub 升级为真实业务：接收 client 推送 → schema 转换（is_suspended bool → suspendFlag int8、加 trade_date）→ ParquetStore append 入库 → 返回新增条数 + 重复日期处理。

**Architecture:**
- 新增 `app/services/ingest.py::IngestService`，纯业务类（无 HTTP 依赖），接受 `MarketDataRequest`，返回 `MarketDataResponseData` 或 raise `APIError`
- `app/api/market_data.py` 改用 `Depends(get_ingest_service)` 注入服务
- 服务依赖通过 FastAPI Depends 链：`Settings → ParquetStore → IngestService`
- 重复日期语义：**全部重复 → 2001；部分重复 → 0 + 准确的 received 计数**
- 空数据语义：**三类都为空 → 2002**

**Files:**
- `v2.3/server/app/services/__init__.py` (NEW, `# 包标记`)
- `v2.3/server/app/services/ingest.py` (NEW)
- `v2.3/server/app/dependencies.py` (NEW, 集中放共享 Depends 工厂)
- `v2.3/server/app/api/market_data.py` (MODIFY, 接入 IngestService)
- `v2.3/server/tests/unit/test_ingest_service.py` (NEW)
- `v2.3/server/tests/unit/test_api_market_data.py` (MODIFY, 加 e2e 用例)

---

## Task 1: IngestService + 单测（不涉及 HTTP）

**Files:**
- Create: `v2.3/server/app/services/__init__.py` (`# 包标记`)
- Create: `v2.3/server/app/services/ingest.py`
- Create: `v2.3/server/tests/unit/test_ingest_service.py`

### Step 1: 写 8 个测试

```python
"""tests/unit/test_ingest_service.py"""
from pathlib import Path

import pandas as pd
import pytest

from app.exceptions import APIError, ErrorCode
from app.schemas.market_data import (
    ETFBar, IndexBar, MarketDataRequest, StockBar,
)
from app.services.ingest import IngestService
from app.storage.parquet import ParquetStore


def _stock(symbol="600519.SH", suspended=False, **overrides):
    base = dict(symbol=symbol, open=10.0, high=11.0, low=9.0, close=10.5,
                volume=1000, amount=10500.0, is_suspended=suspended)
    base.update(overrides)
    return StockBar(**base)


def _index(symbol="000300.SH", **overrides):
    base = dict(symbol=symbol, open=3800.0, high=3850.0, low=3790.0,
                close=3820.0, volume=0, amount=0.0)
    base.update(overrides)
    return IndexBar(**base)


def _etf(symbol="510300.SH", suspended=False, **overrides):
    base = dict(symbol=symbol, open=3.8, high=3.85, low=3.79, close=3.82,
                volume=100000, amount=380000.0, is_suspended=suspended)
    base.update(overrides)
    return ETFBar(**base)


def _svc(tmp_path: Path) -> IngestService:
    return IngestService(parquet_store=ParquetStore(root=tmp_path))


def test_ingest_writes_stocks_to_parquet(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(trade_date="20260430", stocks=[_stock()],
                            indexes=[], etfs=[])
    resp = svc.ingest(req)
    assert resp.received.stocks == 1

    df = pd.read_parquet(tmp_path / "market" / "daily" / "stocks" / "600519.SH.parquet")
    assert list(df["trade_date"]) == [20260430]
    assert df["close"].iloc[0] == 10.5


def test_ingest_translates_is_suspended_to_int(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(
        trade_date="20260430",
        stocks=[_stock(symbol="A.SH", suspended=True),
                _stock(symbol="B.SH", suspended=False)],
        indexes=[], etfs=[],
    )
    svc.ingest(req)

    df_a = pd.read_parquet(tmp_path / "market" / "daily" / "stocks" / "A.SH.parquet")
    df_b = pd.read_parquet(tmp_path / "market" / "daily" / "stocks" / "B.SH.parquet")
    assert df_a["suspendFlag"].iloc[0] == 1
    assert df_b["suspendFlag"].iloc[0] == 0
    # 不应该残留 is_suspended 字段
    assert "is_suspended" not in df_a.columns


def test_ingest_indexes_no_suspendFlag(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(trade_date="20260430", stocks=[],
                            indexes=[_index()], etfs=[])
    svc.ingest(req)

    df = pd.read_parquet(tmp_path / "market" / "daily" / "indexes" / "000300.SH.parquet")
    assert "suspendFlag" not in df.columns
    assert df["close"].iloc[0] == 3820.0


def test_ingest_etfs_with_suspendFlag(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(trade_date="20260430", stocks=[],
                            indexes=[], etfs=[_etf(suspended=True)])
    svc.ingest(req)

    df = pd.read_parquet(tmp_path / "market" / "daily" / "etfs" / "510300.SH.parquet")
    assert df["suspendFlag"].iloc[0] == 1


def test_ingest_drops_turnover_rate(tmp_path: Path):
    """v2.3 设计：不存 turnover_rate（QMT 日线不可靠）。"""
    svc = _svc(tmp_path)
    s = _stock()
    s.turnover_rate = 0.05
    req = MarketDataRequest(trade_date="20260430", stocks=[s], indexes=[], etfs=[])
    svc.ingest(req)

    df = pd.read_parquet(tmp_path / "market" / "daily" / "stocks" / "600519.SH.parquet")
    assert "turnover_rate" not in df.columns


def test_ingest_empty_arrays_raises_2002(tmp_path: Path):
    svc = _svc(tmp_path)
    req = MarketDataRequest(trade_date="20260430", stocks=[], indexes=[], etfs=[])
    with pytest.raises(APIError) as ei:
        svc.ingest(req)
    assert ei.value.code == ErrorCode.EMPTY_DATA


def test_ingest_full_duplicate_raises_2001(tmp_path: Path):
    """同一日同一标的全部已存在 → 2001。"""
    svc = _svc(tmp_path)
    req1 = MarketDataRequest(trade_date="20260430", stocks=[_stock()],
                             indexes=[], etfs=[])
    svc.ingest(req1)
    # 重复推送相同内容
    with pytest.raises(APIError) as ei:
        svc.ingest(req1)
    assert ei.value.code == ErrorCode.DUPLICATE_DATE


def test_ingest_partial_duplicate_returns_correct_count(tmp_path: Path):
    """同一日：A 已存在，B 是新的 → 不抛异常，received.stocks=1。"""
    svc = _svc(tmp_path)
    svc.ingest(MarketDataRequest(
        trade_date="20260430", stocks=[_stock(symbol="A.SH")],
        indexes=[], etfs=[],
    ))
    resp = svc.ingest(MarketDataRequest(
        trade_date="20260430",
        stocks=[_stock(symbol="A.SH"), _stock(symbol="B.SH")],
        indexes=[], etfs=[],
    ))
    assert resp.received.stocks == 1   # 只 B 是新的
    assert resp.strategy_triggered is True


def test_ingest_strategy_triggered_true_on_success(tmp_path: Path):
    svc = _svc(tmp_path)
    resp = svc.ingest(MarketDataRequest(
        trade_date="20260430", stocks=[_stock()], indexes=[], etfs=[],
    ))
    assert resp.strategy_triggered is True
```

### Step 2: 实现 `app/services/__init__.py`

```python
# 包标记
```

### Step 3: 实现 `app/services/ingest.py`

```python
"""行情入库服务：MarketDataRequest → Parquet。"""
from __future__ import annotations

import pandas as pd

from app.exceptions import APIError, ErrorCode
from app.schemas.market_data import (
    ETFBar, IndexBar, MarketDataReceived, MarketDataRequest, MarketDataResponseData, StockBar,
)
from app.storage.parquet import Category, ParquetStore


_OHLCV_FIELDS_WITH_SUSPEND = ["open", "high", "low", "close", "volume", "amount", "suspendFlag"]
_OHLCV_FIELDS_INDEX = ["open", "high", "low", "close", "volume", "amount"]


class IngestService:
    """接收 client 推送的当日行情，按标的写入 Parquet 仓库。

    schema 转换:
      stocks/etfs:  bool is_suspended → int8 suspendFlag (0/1)
      indexes:      不含 suspendFlag
      所有:         drop symbol（文件名编码），加 trade_date（int32，从 req 顶层）
      所有:         drop turnover_rate（v2.3 设计不存）
    """

    def __init__(self, parquet_store: ParquetStore):
        self.store = parquet_store

    def ingest(self, req: MarketDataRequest) -> MarketDataResponseData:
        if not req.stocks and not req.indexes and not req.etfs:
            raise APIError(
                ErrorCode.EMPTY_DATA,
                f"trade_date={req.trade_date} 三类数组均为空",
            )

        trade_date = int(req.trade_date)

        new_stocks = self._write_each("stocks", trade_date, req.stocks, with_suspend=True)
        new_etfs = self._write_each("etfs", trade_date, req.etfs, with_suspend=True)
        new_indexes = self._write_each("indexes", trade_date, req.indexes, with_suspend=False)

        total_new = new_stocks + new_etfs + new_indexes
        total_input = len(req.stocks) + len(req.etfs) + len(req.indexes)

        if total_input > 0 and total_new == 0:
            raise APIError(
                ErrorCode.DUPLICATE_DATE,
                f"trade_date={req.trade_date} 已全部入库（重复推送）",
            )

        return MarketDataResponseData(
            trade_date=req.trade_date,
            received=MarketDataReceived(
                stocks=new_stocks, indexes=new_indexes, etfs=new_etfs,
            ),
            strategy_triggered=True,  # plan 06+ 真实触发策略 runner；现在是 hook 占位
        )

    # ── 内部 ────────────────────────────────────────────────────────────
    def _write_each(
        self,
        category: Category,
        trade_date: int,
        bars: list[StockBar | ETFBar | IndexBar],
        with_suspend: bool,
    ) -> int:
        total_new = 0
        for bar in bars:
            df = self._bar_to_df(bar, trade_date, with_suspend=with_suspend)
            total_new += self.store.append(category, bar.symbol, df)
        return total_new

    @staticmethod
    def _bar_to_df(
        bar: StockBar | ETFBar | IndexBar,
        trade_date: int,
        with_suspend: bool,
    ) -> pd.DataFrame:
        row = {
            "trade_date": trade_date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "amount": bar.amount,
        }
        if with_suspend:
            # type narrow: only StockBar / ETFBar 有 is_suspended
            row["suspendFlag"] = 1 if getattr(bar, "is_suspended", False) else 0
        return pd.DataFrame([row])
```

### Step 4: 跑测试

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest tests/unit/test_ingest_service.py -v   # 9 PASS
```

---

## Task 2: 用 IngestService 替换 stub + 端到端测试

**Files:**
- Create: `v2.3/server/app/dependencies.py`
- Modify: `v2.3/server/app/api/market_data.py`
- Modify: `v2.3/server/tests/unit/test_api_market_data.py`

### Step 1: 实现 `app/dependencies.py`

```python
"""共享 Depends 工厂（避免在路由文件里重复构造服务）。"""
from __future__ import annotations

from fastapi import Depends

from app.services.ingest import IngestService
from app.settings import Settings, get_settings
from app.storage.parquet import ParquetStore


def get_parquet_store(settings: Settings = Depends(get_settings)) -> ParquetStore:
    return ParquetStore(root=settings.parquet_root)


def get_ingest_service(
    store: ParquetStore = Depends(get_parquet_store),
) -> IngestService:
    return IngestService(parquet_store=store)
```

### Step 2: 改 `app/api/market_data.py`（替换原 stub 实现）

```python
"""POST /market-data — 真实业务：通过 IngestService 写 Parquet。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.dependencies import get_ingest_service
from app.schemas.common import APIResponse
from app.schemas.market_data import MarketDataRequest, MarketDataResponseData
from app.services.ingest import IngestService

router = APIRouter()


@router.post(
    "/market-data",
    response_model=APIResponse[MarketDataResponseData],
    dependencies=[Depends(verify_api_key)],
)
async def push_market_data(
    req: MarketDataRequest,
    ingest: IngestService = Depends(get_ingest_service),
):
    """接收 client 推送的当日行情，写入 Parquet 仓库。"""
    data = ingest.ingest(req)
    return APIResponse[MarketDataResponseData](
        code=0,
        message="ok",
        data=data,
    )
```

### Step 3: 加 e2e 用例 — 在 `tests/unit/test_api_market_data.py` 末尾追加

把现有 4 个测试中 `test_post_market_data_happy_path` 加强：再加 2 个 e2e 用例：

```python
# 在文件末尾追加（保持现有 4 个测试不动）

import pandas as pd


def test_post_market_data_writes_parquet(client, settings_for_test):
    """e2e: 端到端发请求 → 检查 parquet 文件实际写出。"""
    r = client.post("/market-data", headers=_AUTH, json={
        "trade_date": "20260430",
        "stocks": [{
            "symbol": "600519.SH",
            "open": 1500.0, "high": 1520.0, "low": 1490.0, "close": 1510.0,
            "volume": 1234, "amount": 1865000.0,
            "is_suspended": False,
        }],
        "indexes": [{
            "symbol": "000300.SH",
            "open": 3800.0, "high": 3850.0, "low": 3790.0, "close": 3820.0,
        }],
        "etfs": [],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["received"] == {"stocks": 1, "indexes": 1, "etfs": 0}

    # 验证文件落盘
    p_stock = settings_for_test.parquet_root / "market" / "daily" / "stocks" / "600519.SH.parquet"
    p_index = settings_for_test.parquet_root / "market" / "daily" / "indexes" / "000300.SH.parquet"
    assert p_stock.exists()
    assert p_index.exists()
    df = pd.read_parquet(p_stock)
    assert df["close"].iloc[0] == 1510.0
    assert df["suspendFlag"].iloc[0] == 0


def test_post_market_data_duplicate_returns_2001(client):
    payload = {
        "trade_date": "20260430",
        "stocks": [{
            "symbol": "A.SH", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
            "volume": 1, "amount": 1.0, "is_suspended": False,
        }],
        "indexes": [], "etfs": [],
    }
    r1 = client.post("/market-data", headers=_AUTH, json=payload)
    assert r1.json()["code"] == 0
    r2 = client.post("/market-data", headers=_AUTH, json=payload)
    body = r2.json()
    assert body["code"] == 2001
```

但还要修改原来的 `test_post_market_data_empty_arrays_ok` —— 之前 stub 接受空，现在真实业务会返回 `code=2002`。把它改名、改断言：

把这行：
```python
def test_post_market_data_empty_arrays_ok(client):
    r = client.post("/market-data", headers=_AUTH,
                    json={"trade_date": "20260430", "stocks": [],
                          "indexes": [], "etfs": []})
    assert r.json()["code"] == 0
```

改为：
```python
def test_post_market_data_empty_arrays_returns_2002(client):
    r = client.post("/market-data", headers=_AUTH,
                    json={"trade_date": "20260430", "stocks": [],
                          "indexes": [], "etfs": []})
    body = r.json()
    assert body["code"] == 2002   # EMPTY_DATA
```

### Step 4: 跑测试

```bash
pytest -v   # 期望 50: 39 prior - 0（test_post_empty_arrays_ok 改语义） + 9 ingest_service + 2 endpoint e2e
```

实际：
- 39 个原测试中 `test_post_market_data_empty_arrays_ok` 改名/语义换 → 仍是 1 个测试
- +9 ingest_service 单测
- +2 endpoint e2e 测试
- 总: 39 + 9 + 2 = 50

### Step 5: Commit（拆 2 个 commit 更清晰）

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/services/ \
        v2.3/server/tests/unit/test_ingest_service.py
git commit -m "feat(server): add IngestService writing market data to Parquet"

git add v2.3/server/app/dependencies.py \
        v2.3/server/app/api/market_data.py \
        v2.3/server/tests/unit/test_api_market_data.py
git commit -m "feat(server): wire /market-data endpoint to IngestService (Plan 05)"
```

---

## 收尾

- [ ] `pytest -v` → 50 PASS
- [ ] `python -m app.main` 起服务后，curl POST /market-data 真实写出 parquet 文件
- [ ] 2 commit

---

## 后续 plan

Plan 06: strategy framework + plugin loader（构造 `Context` + 扫 plugins/ 加载策略）
