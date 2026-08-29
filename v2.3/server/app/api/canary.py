"""Isolated one-lot server canary staging; never creates a Hydra target."""
import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.auth import AuthContext, verify_api_key
from app.dependencies import get_session_factory
from app.exceptions import APIError, ErrorCode
from app.models import Order
from app.schemas.canary import CanaryStageRequest
from app.schemas.common import APIResponse
from app.settings import Settings, get_settings

router = APIRouter(prefix="/hydra/canary")


@router.post("/stage")
def stage(req: CanaryStageRequest, auth: AuthContext = Depends(verify_api_key),
          settings: Settings = Depends(get_settings), sf=Depends(get_session_factory)):
    if not settings.live_canary_staging_enabled:
        raise APIError(ErrorCode.STRATEGY_PENDING, "server canary staging gate closed", http_status=423)
    if auth.execution_domain != "live" or not auth.allows_account(req.account_alias):
        raise APIError(ErrorCode.AUTH_FAILED, "canary live domain/account denied", http_status=403)
    if req.quantity != 100 or req.limit_price * req.quantity > 2000:
        raise APIError(ErrorCode.BAD_REQUEST, "canary must be one lot and <= CNY 2000")
    if abs(req.limit_price / req.reference_price - 1) * 10000 > 50.01:
        raise APIError(ErrorCode.BAD_REQUEST, "canary price offset exceeds 50bps")
    run_id = "canary_" + req.plan_sha256[:24]
    batch_payload = {"rebalance_id": run_id, "attempt_number": 1, "trade_date": req.trade_date,
                     "orders": [{"symbol": req.symbol, "direction": "BUY", "quantity": 100,
                                  "reference_price": round(req.reference_price, 6), "limit_price": round(req.limit_price, 3)}]}
    batch_sha = hashlib.sha256(json.dumps(batch_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    order_id = "hco_" + batch_sha[:32]
    with sf() as session:
        row = session.get(Order, order_id)
        if row is None:
            existing = session.execute(select(Order.order_id).where(Order.execution_domain == "live", Order.qmt_account_alias == req.account_alias, Order.valid_date == req.trade_date, Order.target_id.like("server_canary:%"))).first()
            if existing:
                raise APIError(ErrorCode.BAD_REQUEST, "a server canary already exists for this account/day")
            session.add(Order(order_id=order_id, execution_domain="live", qmt_account_alias=req.account_alias,
                target_id="server_canary:" + run_id, rebalance_id=run_id, attempt_id=run_id, attempt_number=1,
                batch_id="hb_" + batch_sha, batch_sha256=batch_sha, target_hash=req.plan_sha256,
                execution_reference_price=req.reference_price, account_group=req.account_alias, symbol=req.symbol,
                direction="BUY", quantity=100, limit_price=req.limit_price, valid_date=req.trade_date,
                status="PENDING", created_at=datetime.now(timezone.utc).isoformat()))
            session.commit()
    return APIResponse(code=0, message="ok", data={"order_id": order_id, "batch_sha256": batch_sha})
