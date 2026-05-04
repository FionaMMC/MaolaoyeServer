# Plan 02: Parquet 存储层 (`app/storage/parquet.py`)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Server 端 Parquet 行情仓库的读写抽象。每个 (category, symbol) 一个文件，按 `trade_date` 主键去重 + 升序排列。是 ingest（plan 05）和 strategy framework（plan 06）的共同底层依赖。

**Architecture:** 单一职责类 `ParquetStore`。布局 `{root}/market/daily/{stocks|indexes|etfs}/{symbol}.parquet`。append 用"读旧 + 合并 + 去重 + 写回"模式（小文件够用，pyarrow 也无 incremental append 标准支持）。schema 由调用方保证，存储层不做字段校验（保持通用、避免与业务耦合）。

**Tech Stack:** pyarrow + pandas

**Files:**
- `v2.3/server/app/storage/__init__.py` (NEW, 包标记)
- `v2.3/server/app/storage/parquet.py` (NEW)
- `v2.3/server/tests/unit/test_parquet.py` (NEW)

---

## Task 1: ParquetStore + 8 单测

**Files:**
- Create: `v2.3/server/app/storage/__init__.py` (`# 包标记`)
- Create: `v2.3/server/app/storage/parquet.py`
- Create: `v2.3/server/tests/unit/test_parquet.py`

### Step 1: 写测试 (TDD)

`tests/unit/test_parquet.py`:

```python
"""tests/unit/test_parquet.py — ParquetStore 单元测试"""
from pathlib import Path

import pandas as pd
import pytest

from app.storage.parquet import ParquetStore


def _row(trade_date: int, close: float = 10.0) -> dict:
    return {
        "trade_date": trade_date,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.3,
        "close": close,
        "volume": 1000,
        "amount": close * 1000,
        "suspendFlag": 0,
    }


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_append_creates_new_file(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    n = store.append("stocks", "600519.SH", _df([_row(20260428)]))
    assert n == 1
    p = tmp_path / "market" / "daily" / "stocks" / "600519.SH.parquet"
    assert p.exists()


def test_append_dedupes_existing_dates(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH", _df([_row(20260428, close=10.0)]))
    # 同一日期再写：应被去重
    n = store.append("stocks", "600519.SH",
                     _df([_row(20260428, close=99.0), _row(20260429)]))
    assert n == 1   # 只新增 20260429
    df = store.read("stocks", "600519.SH")
    assert len(df) == 2
    # 旧日期内容保留（不被新值覆盖；先到先得）
    assert df.set_index("trade_date").loc[20260428, "close"] == 10.0


def test_append_keeps_data_sorted_by_trade_date(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    # 乱序写入
    store.append("stocks", "600519.SH",
                 _df([_row(20260430), _row(20260428), _row(20260429)]))
    df = store.read("stocks", "600519.SH")
    assert df["trade_date"].tolist() == [20260428, 20260429, 20260430]


def test_read_full(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH",
                 _df([_row(20260428), _row(20260429), _row(20260430)]))
    df = store.read("stocks", "600519.SH")
    assert len(df) == 3
    assert set(df.columns) >= {"trade_date", "close"}


def test_read_date_range(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH",
                 _df([_row(d) for d in (20260427, 20260428, 20260429, 20260430)]))
    df = store.read("stocks", "600519.SH", start_date=20260428, end_date=20260429)
    assert df["trade_date"].tolist() == [20260428, 20260429]


def test_read_nonexistent_symbol_returns_empty(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    df = store.read("stocks", "NOT_EXIST.SH")
    assert df.empty
    assert isinstance(df, pd.DataFrame)


def test_has_date(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH", _df([_row(20260428)]))
    assert store.has_date("stocks", "600519.SH", 20260428) is True
    assert store.has_date("stocks", "600519.SH", 20260429) is False
    assert store.has_date("stocks", "NOT_EXIST.SH", 20260428) is False


def test_list_symbols_per_category(tmp_path: Path):
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH", _df([_row(20260428)]))
    store.append("stocks", "000001.SZ", _df([_row(20260428)]))
    store.append("indexes", "000300.SH", _df([_row(20260428)]))

    assert sorted(store.list_symbols("stocks")) == ["000001.SZ", "600519.SH"]
    assert store.list_symbols("indexes") == ["000300.SH"]
    assert store.list_symbols("etfs") == []


def test_categories_isolated(tmp_path: Path):
    """同名 symbol 在不同 category 下互不影响。"""
    store = ParquetStore(root=tmp_path)
    store.append("stocks", "600519.SH", _df([_row(20260428, close=1500.0)]))
    store.append("etfs", "600519.SH", _df([_row(20260428, close=2.5)]))
    s = store.read("stocks", "600519.SH")
    e = store.read("etfs", "600519.SH")
    assert s["close"].iloc[0] == 1500.0
    assert e["close"].iloc[0] == 2.5
```

### Step 2: Run, confirm ImportError

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest tests/unit/test_parquet.py -v
```

### Step 3: Implement `app/storage/parquet.py`

```python
"""Parquet 行情仓库：按 (category, symbol) 一文件，trade_date 主键去重 + 升序。

布局:
  {root}/market/daily/stocks/{symbol}.parquet
  {root}/market/daily/indexes/{symbol}.parquet
  {root}/market/daily/etfs/{symbol}.parquet

写入策略：read-merge-dedupe-write（小文件够用，pyarrow 无原生 append-with-dedup）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import pandas as pd

Category = Literal["stocks", "indexes", "etfs"]
_PK = "trade_date"


class ParquetStore:
    """Parquet 行情仓库。schema 由调用方保证，store 不做字段校验。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    # ── 路径解析 ─────────────────────────────────────────────────────────
    def _file(self, category: Category, symbol: str) -> Path:
        return self.root / "market" / "daily" / category / f"{symbol}.parquet"

    def _category_dir(self, category: Category) -> Path:
        return self.root / "market" / "daily" / category

    # ── 写 ──────────────────────────────────────────────────────────────
    def append(self, category: Category, symbol: str, df: pd.DataFrame) -> int:
        """追加写入，按 trade_date 主键去重。返回实际新增行数。

        语义：已存在的 trade_date 保留旧值（先到先得），新 trade_date 追加。
        """
        if df.empty:
            return 0
        if _PK not in df.columns:
            raise ValueError(f"DataFrame 必须含 '{_PK}' 列")

        path = self._file(category, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            existing = pd.read_parquet(path)
            existing_dates = set(existing[_PK])
            new_rows = df[~df[_PK].isin(existing_dates)]
            if new_rows.empty:
                return 0
            merged = pd.concat([existing, new_rows], ignore_index=True)
        else:
            new_rows = df
            merged = df.copy()

        merged.sort_values(_PK, inplace=True, ignore_index=True)
        merged.to_parquet(path, engine="pyarrow", index=False)
        return len(new_rows)

    # ── 读 ──────────────────────────────────────────────────────────────
    def read(
        self,
        category: Category,
        symbol: str,
        start_date: int | None = None,
        end_date: int | None = None,
    ) -> pd.DataFrame:
        """读取单标的历史，可选日期范围（含端点）。

        缺失文件返回空 DataFrame（不抛异常）。
        """
        path = self._file(category, symbol)
        if not path.exists():
            return pd.DataFrame()

        df = pd.read_parquet(path)
        if start_date is not None:
            df = df[df[_PK] >= start_date]
        if end_date is not None:
            df = df[df[_PK] <= end_date]
        return df.reset_index(drop=True)

    def has_date(self, category: Category, symbol: str, trade_date: int) -> bool:
        path = self._file(category, symbol)
        if not path.exists():
            return False
        df = pd.read_parquet(path, columns=[_PK])
        return bool((df[_PK] == trade_date).any())

    def list_symbols(self, category: Category) -> list[str]:
        d = self._category_dir(category)
        if not d.exists():
            return []
        return [p.stem for p in d.glob("*.parquet")]
```

### Step 4: Run, confirm 9 PASS (18 total in suite)

```bash
pytest tests/unit/test_parquet.py -v
pytest -v   # full suite: 9 prior + 9 new = 18
```

### Step 5: Commit

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/storage/__init__.py \
        v2.3/server/app/storage/parquet.py \
        v2.3/server/tests/unit/test_parquet.py
git commit -m "feat(server): add ParquetStore for incremental OHLCV storage"
```

---

## 收尾

- [ ] `pytest -v` 全绿 (18 测试)
- [ ] 1 个 commit

---

## 后续 plan

Plan 03: SQLite + ORM models（business tables 用 SQLAlchemy 2.0）
