# Plan 14b: 外部数据上传端点 — POST /admin/upload-data

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** 让策略生成方（你 Mac / 搭档 ML pipeline）能把策略私有数据（V20H 的 pred_csi1000 / v12_exp_hs300 / stock_close / index_csi1000）上传到 server，存到对应 plugin 的 data/ 目录。完成后 V20H adapter 在 server 上就能读到数据。

**Architecture:**
- 用 FastAPI `UploadFile` (multipart/form-data) 接收文件
- 路由：`POST /admin/upload-data?strategy=<name>&filename=<file.parquet>`
- 安全：白名单机制 — 每个 Strategy 子类**必须声明** `data_dir`（数据目录路径，绝对）+ `data_files`（允许的文件名列表）
- 上传时校验：文件名在白名单内、扩展名 `.parquet`、内容 pyarrow 能解析
- 配套 `GET /admin/data-status?strategy=<name>` 查看已上传文件的 size + mtime

**Files:**
- `v2.3/server/app/strategy/base.py` (MODIFY，给 Strategy 加 `data_dir` + `data_files` ClassVar)
- `v2.3/server/plugins/v20h_adapter.py` (MODIFY，声明 V20H 自己的 data_dir + data_files)
- `v2.3/server/app/api/admin.py` (MODIFY，加 upload + status 两个端点)
- `v2.3/server/app/services/data_upload.py` (NEW，上传逻辑 + 校验)
- `v2.3/server/app/dependencies.py` (MODIFY，加 service factory)
- `v2.3/server/tests/unit/test_data_upload.py` (NEW)
- `v2.3/server/tests/unit/test_api_admin.py` (MODIFY，加 upload + status 测试)

---

## Task 1: 给 Strategy 基类加白名单字段

`app/strategy/base.py` 改造：

```python
from pathlib import Path
from typing import ClassVar

class Strategy(abc.ABC):
    name: str = ""
    data_dir: ClassVar[Path | None] = None      # 新加
    data_files: ClassVar[list[str]] = []         # 新加：允许上传的文件白名单

    @abc.abstractmethod
    def run(self, ctx: Context, trade_date: int) -> list[RawSignal]:
        ...
```

`plugins/v20h_adapter.py` 顶部加：

```python
class V20HAdapter(Strategy):
    name = "v20h_v1_3"
    data_dir = _V20H_DIR / "data"
    data_files = [
        "pred_csi1000.parquet",
        "v12_exp_hs300.parquet",
        "stock_close.parquet",
        "stock_returns.parquet",
        "index_csi1000.parquet",
    ]
    # ... 其他方法不变
```

`plugins/_example_buy_threshold.py` 也加（哪怕空列表）：

```python
class BuyOnDipExample(Strategy):
    name = "buy_on_dip_example"
    data_dir = None        # 不需要外部数据
    data_files = []
    # ...
```

---

## Task 2: DataUploadService

`app/services/data_upload.py`:

```python
"""外部数据上传服务：把策略私有数据写入对应 plugin 的 data/ 目录。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Type

import pyarrow.parquet as pq

from app.exceptions import APIError, ErrorCode
from app.strategy.base import Strategy

logger = logging.getLogger(__name__)


class DataUploadService:
    """上传 + 校验 + 持久化策略私有数据文件。"""

    def __init__(self, registry: dict[str, Type[Strategy]]):
        self.registry = registry

    def upload(
        self, strategy_name: str, filename: str, body: bytes,
    ) -> dict:
        """保存上传的 bytes 到 plugin 的 data/。校验失败抛 APIError。"""
        strategy_cls = self._lookup(strategy_name)

        if strategy_cls.data_dir is None:
            raise APIError(
                ErrorCode.BAD_REQUEST,
                f"strategy '{strategy_name}' 不接受外部数据上传（data_dir=None）",
            )

        if filename not in strategy_cls.data_files:
            raise APIError(
                ErrorCode.BAD_REQUEST,
                f"filename '{filename}' 不在 strategy '{strategy_name}' 的"
                f" data_files 白名单 {strategy_cls.data_files}",
            )

        if not filename.endswith(".parquet"):
            raise APIError(
                ErrorCode.BAD_REQUEST,
                "仅接受 .parquet 文件",
            )

        # 防御性检查：parquet 真的能解析吗
        import io
        try:
            pq.read_metadata(io.BytesIO(body))
        except Exception as e:  # noqa: BLE001
            raise APIError(
                ErrorCode.BAD_REQUEST,
                f"parquet 解析失败: {e}",
            )

        target_dir = Path(strategy_cls.data_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename
        target_path.write_bytes(body)

        logger.info(
            "data_uploaded strategy=%s filename=%s size=%d path=%s",
            strategy_name, filename, len(body), target_path,
        )

        return {
            "strategy": strategy_name,
            "filename": filename,
            "bytes": len(body),
            "path": str(target_path),
            "saved_at": _now_iso(),
        }

    def status(self, strategy_name: str) -> dict:
        """返回该策略 data_dir 下所有白名单文件的 size + mtime。"""
        strategy_cls = self._lookup(strategy_name)

        if strategy_cls.data_dir is None:
            return {"strategy": strategy_name, "files": []}

        files = []
        for fn in strategy_cls.data_files:
            p = Path(strategy_cls.data_dir) / fn
            if p.exists():
                files.append({
                    "filename": fn,
                    "bytes": p.stat().st_size,
                    "mtime": datetime.fromtimestamp(
                        p.stat().st_mtime, tz=timezone.utc,
                    ).astimezone().isoformat(timespec="seconds"),
                    "exists": True,
                })
            else:
                files.append({
                    "filename": fn, "bytes": 0, "mtime": None, "exists": False,
                })
        return {"strategy": strategy_name, "files": files}

    def _lookup(self, strategy_name: str) -> Type[Strategy]:
        if strategy_name not in self.registry:
            raise APIError(
                ErrorCode.BAD_REQUEST,
                f"strategy '{strategy_name}' 未注册",
            )
        return self.registry[strategy_name]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
```

---

## Task 3: dependencies + admin endpoints

`app/dependencies.py` 末尾追加：

```python
from app.services.data_upload import DataUploadService


def get_data_upload_service(
    settings: Settings = Depends(get_settings),
) -> DataUploadService:
    registry = _strategy_registry(str(settings.plugins_dir))
    return DataUploadService(registry=registry)
```

`app/api/admin.py` 加两个端点（保留原 run-pipeline）:

```python
"""Admin 端点：人工触发 + 数据管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.auth import verify_api_key
from app.dependencies import get_data_upload_service, get_strategy_pipeline
from app.scheduler.pipeline import StrategyPipeline
from app.schemas.common import APIResponse
from app.services.data_upload import DataUploadService

router = APIRouter(prefix="/admin")


@router.post(
    "/run-pipeline",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def run_pipeline_now(
    trade_date: int = Query(ge=20000101, le=99991231),
    pipeline: StrategyPipeline = Depends(get_strategy_pipeline),
):
    summary = pipeline.run(trade_date)
    return APIResponse[dict](code=0, message="ok", data=summary)


@router.post(
    "/upload-data",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def upload_strategy_data(
    strategy: str = Query(min_length=1, max_length=100),
    filename: str = Query(min_length=1, max_length=200),
    file: UploadFile = File(...),
    service: DataUploadService = Depends(get_data_upload_service),
):
    """上传策略私有数据。需要 strategy 已声明 data_dir + data_files。"""
    body = await file.read()
    info = service.upload(strategy_name=strategy, filename=filename, body=body)
    return APIResponse[dict](code=0, message="ok", data=info)


@router.get(
    "/data-status",
    response_model=APIResponse[dict],
    dependencies=[Depends(verify_api_key)],
)
async def data_status(
    strategy: str = Query(min_length=1, max_length=100),
    service: DataUploadService = Depends(get_data_upload_service),
):
    info = service.status(strategy_name=strategy)
    return APIResponse[dict](code=0, message="ok", data=info)
```

---

## Task 4: 单测

`tests/unit/test_data_upload.py`:

```python
"""DataUploadService 单元测试"""
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.exceptions import APIError, ErrorCode
from app.services.data_upload import DataUploadService
from app.strategy.base import Strategy


def _make_parquet_bytes(rows: list[dict]) -> bytes:
    import io
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_parquet(buf, engine="pyarrow", index=False)
    return buf.getvalue()


def _registry_with_test_strategy(tmp_path: Path) -> dict:
    class TestStrat(Strategy):
        name = "test_strat"
        data_dir = tmp_path / "test_strat_data"
        data_files = ["pred.parquet", "v12.parquet"]
        def run(self, ctx, trade_date):
            return []

    class NoDataStrat(Strategy):
        name = "no_data"
        data_dir = None
        data_files = []
        def run(self, ctx, trade_date):
            return []

    return {"test_strat": TestStrat, "no_data": NoDataStrat}


def test_upload_happy_path(tmp_path: Path):
    svc = DataUploadService(registry=_registry_with_test_strategy(tmp_path))
    body = _make_parquet_bytes([{"x": 1, "y": 2}])

    info = svc.upload("test_strat", "pred.parquet", body)

    assert info["strategy"] == "test_strat"
    assert info["filename"] == "pred.parquet"
    assert info["bytes"] == len(body)
    assert "saved_at" in info

    saved = Path(info["path"])
    assert saved.exists()
    df = pd.read_parquet(saved)
    assert list(df.columns) == ["x", "y"]


def test_upload_unknown_strategy_raises(tmp_path: Path):
    svc = DataUploadService(registry=_registry_with_test_strategy(tmp_path))
    with pytest.raises(APIError) as ei:
        svc.upload("ghost", "any.parquet", b"")
    assert ei.value.code == ErrorCode.BAD_REQUEST
    assert "未注册" in ei.value.message


def test_upload_filename_not_in_whitelist(tmp_path: Path):
    svc = DataUploadService(registry=_registry_with_test_strategy(tmp_path))
    with pytest.raises(APIError) as ei:
        svc.upload("test_strat", "notallowed.parquet",
                   _make_parquet_bytes([{"a": 1}]))
    assert ei.value.code == ErrorCode.BAD_REQUEST
    assert "白名单" in ei.value.message


def test_upload_strategy_without_data_dir(tmp_path: Path):
    svc = DataUploadService(registry=_registry_with_test_strategy(tmp_path))
    with pytest.raises(APIError) as ei:
        svc.upload("no_data", "any.parquet", b"")
    assert "不接受外部数据上传" in ei.value.message


def test_upload_non_parquet_extension(tmp_path: Path):
    svc = DataUploadService(registry=_registry_with_test_strategy(tmp_path))
    # 即便是 parquet 内容，扩展名不对也拒绝
    body = _make_parquet_bytes([{"x": 1}])
    # 把 filename 加进白名单（用 .csv 扩展名）
    svc.registry["test_strat"].data_files = ["bad.csv"]
    with pytest.raises(APIError, match="parquet"):
        svc.upload("test_strat", "bad.csv", body)


def test_upload_invalid_parquet_body(tmp_path: Path):
    svc = DataUploadService(registry=_registry_with_test_strategy(tmp_path))
    with pytest.raises(APIError, match="parquet 解析失败"):
        svc.upload("test_strat", "pred.parquet", b"this is not a parquet file")


def test_upload_overwrites_existing(tmp_path: Path):
    svc = DataUploadService(registry=_registry_with_test_strategy(tmp_path))
    body1 = _make_parquet_bytes([{"x": 1}])
    body2 = _make_parquet_bytes([{"x": 999}])
    svc.upload("test_strat", "pred.parquet", body1)
    svc.upload("test_strat", "pred.parquet", body2)

    saved = Path(svc.registry["test_strat"].data_dir) / "pred.parquet"
    df = pd.read_parquet(saved)
    assert df["x"].iloc[0] == 999  # 覆盖成功


def test_status_lists_files(tmp_path: Path):
    svc = DataUploadService(registry=_registry_with_test_strategy(tmp_path))
    body = _make_parquet_bytes([{"a": 1}])
    svc.upload("test_strat", "pred.parquet", body)
    # v12 没上传

    s = svc.status("test_strat")
    assert s["strategy"] == "test_strat"
    files = {f["filename"]: f for f in s["files"]}
    assert files["pred.parquet"]["exists"] is True
    assert files["pred.parquet"]["bytes"] == len(body)
    assert files["v12.parquet"]["exists"] is False
    assert files["v12.parquet"]["bytes"] == 0


def test_status_strategy_without_data_dir(tmp_path: Path):
    svc = DataUploadService(registry=_registry_with_test_strategy(tmp_path))
    s = svc.status("no_data")
    assert s["files"] == []
```

`tests/unit/test_api_admin.py` 末尾追加：

```python
def test_upload_data_endpoint_happy_path(client, settings_for_test, tmp_path):
    """e2e: 上传 → status 能看到。"""
    import io
    import pandas as pd

    # 先用一个真实存在的 strategy_name (默认插件自带 buy_on_dip_example，但它 data_dir=None)
    # 改用上面 registry 的处理方式 — 这里我们 mock 一个能接收数据的 plugin

    # 因为实际 plugins 加载依赖 settings.plugins_dir，client fixture 已经把它指到 tmp，
    # 所以暂时不会有真 plugin 注册。我们插一个 fake plugin 文件进去：
    plugin_file = settings_for_test.plugins_dir / "_test_data_plugin.py"
    plugin_file.write_text('''
from pathlib import Path
from app.strategy.base import Strategy

class TestUploadPlugin(Strategy):
    name = "test_upload_plugin"
    data_dir = Path(__file__).parent / "_test_data"
    data_files = ["sample.parquet"]
    def run(self, ctx, trade_date):
        return []
''')

    # 重置 registry 缓存
    from app.dependencies import _strategy_registry
    _strategy_registry.cache_clear()

    df = pd.DataFrame([{"x": 1, "y": 2}])
    buf = io.BytesIO()
    df.to_parquet(buf, engine="pyarrow", index=False)
    buf.seek(0)

    r = client.post(
        "/admin/upload-data?strategy=test_upload_plugin&filename=sample.parquet",
        headers=_AUTH,
        files={"file": ("sample.parquet", buf.getvalue(), "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["bytes"] > 0


def test_upload_data_unknown_strategy(client, settings_for_test):
    import io
    r = client.post(
        "/admin/upload-data?strategy=ghost&filename=x.parquet",
        headers=_AUTH,
        files={"file": ("x.parquet", b"data", "application/octet-stream")},
    )
    body = r.json()
    assert body["code"] == 1002
    assert "未注册" in body["message"]


def test_upload_data_no_auth(client):
    import io
    r = client.post(
        "/admin/upload-data?strategy=any&filename=x.parquet",
        files={"file": ("x.parquet", b"data", "application/octet-stream")},
    )
    assert r.status_code == 401


def test_data_status_endpoint(client, settings_for_test):
    plugin_file = settings_for_test.plugins_dir / "_test_status_plugin.py"
    plugin_file.write_text('''
from pathlib import Path
from app.strategy.base import Strategy

class StatusPlugin(Strategy):
    name = "status_plugin"
    data_dir = Path(__file__).parent / "_status_data"
    data_files = ["a.parquet", "b.parquet"]
    def run(self, ctx, trade_date):
        return []
''')

    from app.dependencies import _strategy_registry
    _strategy_registry.cache_clear()

    r = client.get(
        "/admin/data-status?strategy=status_plugin",
        headers=_AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    files = body["data"]["files"]
    assert {f["filename"] for f in files} == {"a.parquet", "b.parquet"}
    assert all(f["exists"] is False for f in files)   # 啥都没上传过
```

---

## Task 5: V20H adapter 加 data_dir + data_files

修改 `plugins/v20h_adapter.py` 的类定义：

```python
class V20HAdapter(Strategy):
    name = "v20h_v1_3"
    data_dir = _V20H_DIR / "data"
    data_files = [
        "pred_csi1000.parquet",
        "v12_exp_hs300.parquet",
        "stock_close.parquet",
        "stock_returns.parquet",
        "index_csi1000.parquet",
    ]
    # ... 后续方法不变
```

`plugins/_example_buy_threshold.py` 也补上：

```python
class BuyOnDipExample(Strategy):
    name = "buy_on_dip_example"
    data_dir = None
    data_files = []
    # ...
```

---

## 验证 + Commit

```bash
cd /Users/mameican/Desktop/server/v2.3/server
source venv/bin/activate
pytest -v   # 期望 146 + 9 (data_upload) + 4 (admin endpoints) = 159 PASS
```

```bash
cd /Users/mameican/Desktop/server
git add v2.3/server/app/strategy/base.py \
        v2.3/server/app/services/data_upload.py \
        v2.3/server/app/api/admin.py \
        v2.3/server/app/dependencies.py \
        v2.3/server/plugins/v20h_adapter.py \
        v2.3/server/plugins/_example_buy_threshold.py \
        v2.3/server/tests/unit/test_data_upload.py \
        v2.3/server/tests/unit/test_api_admin.py
git commit -m "feat(server): add /admin/upload-data + /admin/data-status (Plan 14b)"
```

---

## 上传脚本（给你 Mac 用）

部署后让你方便地批量上传 V20H 那 47 MB 数据，写一个一次性 helper：

`v2.3/server/scripts/upload_v20h_data.sh`：

```bash
#!/usr/bin/env bash
# 把 V20H 历史数据从 Mac 上传到 server。
# 用法: ./scripts/upload_v20h_data.sh <BASE_URL> <API_KEY> <LOCAL_V20H_DATA_DIR>
set -euo pipefail

BASE="${1:-http://120.26.138.82:8000}"
KEY="${2:-pipeline-v23-shared-secret-2026}"
LOCAL_DIR="${3:-/Users/mameican/Desktop/server/v2.3/server/plugins/v20h/data}"

for fn in pred_csi1000.parquet v12_exp_hs300.parquet stock_close.parquet \
          stock_returns.parquet index_csi1000.parquet; do
  if [ -f "$LOCAL_DIR/$fn" ]; then
    size=$(du -h "$LOCAL_DIR/$fn" | cut -f1)
    echo "▶ uploading $fn ($size)..."
    curl -X POST "$BASE/admin/upload-data?strategy=v20h_v1_3&filename=$fn" \
      -H "Authorization: Bearer $KEY" \
      -F "file=@$LOCAL_DIR/$fn" \
      --progress-bar
    echo ""
  else
    echo "⚠️  $fn not found, skipping"
  fi
done

echo "▶ status:"
curl -s -H "Authorization: Bearer $KEY" \
  "$BASE/admin/data-status?strategy=v20h_v1_3" | python3 -m json.tool
```

注意：`pred_csi1000.parquet` 26 MB，需要确认 nginx 没限制 client_max_body_size（uvicorn 默认无限制）。

---

## 收尾

- [ ] 159+ pytest PASS
- [ ] 1 commit
- [ ] `scripts/upload_v20h_data.sh` 准备好（只是脚本本身，实际上传等部署完）

---

## 后续 plan

Plan 14c：把 V20H adapter 切到实盘，emit 真的 RawSignal[]
