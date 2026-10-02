"""Live-QMT market-data backups, isolated from the canonical strategy feed."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends

from app.auth import AuthContext, verify_api_key
from app.exceptions import APIError, ErrorCode
from app.schemas.common import APIResponse
from app.schemas.live_qmt_backup import LiveQMTBackupRequest
from app.settings import Settings, get_settings

router = APIRouter(prefix="/live-qmt-backups")


@router.post("/market-data", response_model=APIResponse[dict])
async def upload_live_qmt_backup(
    req: LiveQMTBackupRequest,
    auth: AuthContext = Depends(verify_api_key),
    settings: Settings = Depends(get_settings),
):
    if auth.execution_domain != "live" or not auth.allows_account(req.source_id):
        raise APIError(ErrorCode.AUTH_FAILED, "live QMT backup source 未授权", 403)
    payload = req.model_dump(mode="json")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    directory = Path(settings.parquet_root) / "live_qmt_backups" / req.trade_date / req.source_id
    directory.mkdir(parents=True, exist_ok=True)
    data_path = directory / "payload.json"
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("sha256") != digest:
            raise APIError(ErrorCode.DUPLICATE_DATE, "同一实盘备份日期/source 内容不一致")
        return APIResponse(code=0, message="already backed up", data=existing)
    data_path.write_bytes(canonical + b"\n")
    manifest = {
        "trade_date": req.trade_date, "source_id": req.source_id, "sha256": digest,
        "received": {"stocks": len(req.stocks), "etfs": len(req.etfs), "indexes": len(req.indexes)},
        "purpose": "diagnostic_backup_only",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return APIResponse(code=0, message="backed up", data=manifest)
