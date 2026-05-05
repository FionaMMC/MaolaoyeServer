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
