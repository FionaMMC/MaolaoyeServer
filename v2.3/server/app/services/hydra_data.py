"""Hydra 后复权模型价、原始执行价、公司行动的不可变内容仓库。"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
from pathlib import Path

import pandas as pd

from app.schemas.hydra_data import HydraDataInstallResult, HydraDataManifest

_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ)$")
_PRICE_COLUMNS = {
    "symbol", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "suspendFlag",
}
_ACTION_COLUMNS = {
    "symbol", "event_date", "event_type", "cash_per_share", "share_factor",
    "source_event_id",
}
_EXPECTED_ADJUSTMENT = {
    "hydra_model_hfq": "back",
    "hydra_execution_raw": "none",
    "hydra_corporate_actions": "corporate_actions",
    "hydra_trading_calendar": "calendar",
}


def _canonical_json(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


class HydraDataStore:
    """以 file SHA-256 为目录，禁止静默覆盖同一批数据。"""

    def __init__(self, root: Path | str):
        self.root = Path(root) / "hydra" / "batches"

    def install(
        self, body: bytes, manifest: HydraDataManifest,
    ) -> HydraDataInstallResult:
        actual_sha = hashlib.sha256(body).hexdigest()
        if actual_sha != manifest.file_sha256:
            raise ValueError("manifest file_sha256 与上传文件不一致")
        expected_adjustment = _EXPECTED_ADJUSTMENT[manifest.stream]
        if manifest.adjustment != expected_adjustment:
            raise ValueError(
                f"{manifest.stream} adjustment 必须是 {expected_adjustment}"
            )
        try:
            frame = pd.read_parquet(io.BytesIO(body))
        except Exception as exc:
            raise ValueError(f"Hydra 数据不是有效 parquet: {exc}") from exc
        self._validate_frame(frame, manifest)

        manifest_payload = manifest.model_dump(mode="json")
        manifest_sha = hashlib.sha256(_canonical_json(manifest_payload)).hexdigest()
        batch_dir = self.root / manifest.stream / manifest.file_sha256
        data_path = batch_dir / "data.parquet"
        manifest_path = batch_dir / "manifest.json"
        if batch_dir.exists():
            if not data_path.is_file() or not manifest_path.is_file():
                raise ValueError("已有 Hydra batch 目录不完整，拒绝覆盖")
            existing_body_sha = hashlib.sha256(data_path.read_bytes()).hexdigest()
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                existing_body_sha != manifest.file_sha256
                or existing_manifest != manifest_payload
            ):
                raise ValueError("同一内容地址已有不同内容，拒绝覆盖")
            return HydraDataInstallResult(
                stream=manifest.stream,
                file_sha256=manifest.file_sha256,
                manifest_sha256=manifest_sha,
                as_of_date=manifest.as_of_date,
                row_count=manifest.row_count,
                symbol_count=manifest.symbol_count,
                installed=False,
                batch_dir=str(batch_dir),
            )

        batch_dir.mkdir(parents=True, exist_ok=False)
        data_tmp = self._write_temp(batch_dir, body, ".parquet.tmp")
        manifest_tmp = self._write_temp(
            batch_dir, _canonical_json(manifest_payload) + b"\n", ".json.tmp",
        )
        try:
            os.replace(data_tmp, data_path)
            os.replace(manifest_tmp, manifest_path)
        finally:
            Path(data_tmp).unlink(missing_ok=True)
            Path(manifest_tmp).unlink(missing_ok=True)
        return HydraDataInstallResult(
            stream=manifest.stream,
            file_sha256=manifest.file_sha256,
            manifest_sha256=manifest_sha,
            as_of_date=manifest.as_of_date,
            row_count=manifest.row_count,
            symbol_count=manifest.symbol_count,
            installed=True,
            batch_dir=str(batch_dir),
        )

    def load(
        self, stream: str, file_sha256: str,
    ) -> tuple[pd.DataFrame, HydraDataManifest]:
        batch_dir = self.root / stream / file_sha256
        data_path = batch_dir / "data.parquet"
        manifest_path = batch_dir / "manifest.json"
        if not data_path.is_file() or not manifest_path.is_file():
            raise FileNotFoundError(f"Hydra data batch 不存在: {stream}/{file_sha256}")
        body = data_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != file_sha256:
            raise ValueError("Hydra data batch 文件 hash 校验失败")
        manifest = HydraDataManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        if manifest.stream != stream or manifest.file_sha256 != file_sha256:
            raise ValueError("Hydra data batch manifest 路径/内容不一致")
        frame = pd.read_parquet(io.BytesIO(body))
        self._validate_frame(frame, manifest)
        return frame, manifest

    @staticmethod
    def _write_temp(directory: Path, body: bytes, suffix: str) -> str:
        handle = tempfile.NamedTemporaryFile(
            dir=directory, prefix=".install-", suffix=suffix, delete=False,
        )
        try:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
            return handle.name
        finally:
            handle.close()

    @staticmethod
    def _validate_frame(frame: pd.DataFrame, manifest: HydraDataManifest) -> None:
        if len(frame) != manifest.row_count:
            raise ValueError("manifest row_count 与 parquet 不一致")
        if manifest.stream == "hydra_trading_calendar":
            if set(frame.columns) != {"trade_date"}:
                raise ValueError("trading-calendar 必须仅含 trade_date 列")
            if frame["trade_date"].duplicated().any():
                raise ValueError("trading-calendar 含重复交易日")
            dates = frame["trade_date"].astype(str).str.replace("-", "", regex=False)
            if not dates.str.fullmatch(r"\d{8}").all():
                raise ValueError("trading-calendar 日期必须是 YYYYMMDD")
            if manifest.as_of_date not in set(dates):
                raise ValueError("trading-calendar 不含 as_of_date")
            if manifest.symbol_count != 0:
                raise ValueError("trading-calendar symbol_count 必须为 0")
            return
        if manifest.stream == "hydra_corporate_actions":
            missing = sorted(_ACTION_COLUMNS - set(frame.columns))
            if missing:
                raise ValueError(f"corporate-actions 缺少列: {missing}")
            date_col = "event_date"
            if frame[["symbol", date_col, "event_type", "source_event_id"]].duplicated().any():
                raise ValueError("corporate-actions 存在重复事件")
            cash = pd.to_numeric(frame["cash_per_share"], errors="coerce")
            factors = pd.to_numeric(frame["share_factor"], errors="coerce")
            if not cash.map(lambda value: math.isfinite(value) and value >= 0).all():
                raise ValueError("cash_per_share 必须是有限非负数")
            if not factors.map(lambda value: math.isfinite(value) and value > 0).all():
                raise ValueError("share_factor 必须是有限正数")
        else:
            missing = sorted(_PRICE_COLUMNS - set(frame.columns))
            if missing:
                raise ValueError(f"price batch 缺少列: {missing}")
            date_col = "trade_date"
            if frame[["symbol", date_col]].duplicated().any():
                raise ValueError("price batch 存在重复 symbol/trade_date")
            suspended = pd.to_numeric(frame["suspendFlag"], errors="coerce")
            if not suspended.isin([0, 1]).all():
                raise ValueError("suspendFlag 必须是 0/1")
            numeric = {
                column: pd.to_numeric(frame[column], errors="coerce")
                for column in ("open", "high", "low", "close", "volume", "amount")
            }
            if not numeric["close"].map(
                lambda value: math.isfinite(value) and value > 0
            ).all():
                raise ValueError("close 必须是有限正数")
            active = suspended == 0
            for column in ("open", "high", "low"):
                values = numeric[column]
                clean = values.map(math.isfinite) & (
                    (active & (values > 0)) | (~active & (values >= 0))
                )
                if not clean.all():
                    raise ValueError(f"{column} 活跃日必须为正、停牌日不得为负")
            for column in ("volume", "amount"):
                values = numeric[column]
                if not (values.map(math.isfinite) & (values >= 0)).all():
                    raise ValueError(f"{column} 必须是有限非负数")
            if (
                (numeric["high"][active] < numeric["low"][active]).any()
                or (numeric["high"][active] < numeric["open"][active]).any()
                or (numeric["high"][active] < numeric["close"][active]).any()
                or (numeric["low"][active] > numeric["open"][active]).any()
                or (numeric["low"][active] > numeric["close"][active]).any()
            ):
                raise ValueError("活跃日 OHLC 高低价关系非法")

        symbols = frame["symbol"].astype(str)
        if not symbols.map(lambda value: bool(_CODE_RE.fullmatch(value))).all():
            raise ValueError("Hydra batch 包含非法 QMT ETF code")
        if symbols.nunique() != manifest.symbol_count:
            raise ValueError("manifest symbol_count 与 parquet 不一致")
        dates = frame[date_col].astype(str).str.replace("-", "", regex=False)
        if not dates.str.fullmatch(r"\d{8}").all():
            raise ValueError(f"{date_col} 必须是 YYYYMMDD")
        if manifest.stream == "hydra_corporate_actions":
            if not dates.empty and dates.max() > manifest.as_of_date:
                raise ValueError("corporate-actions 事件日期超过 coverage as_of_date")
        elif dates.max() != manifest.as_of_date:
            raise ValueError("manifest as_of_date 必须等于数据最大日期")
