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
