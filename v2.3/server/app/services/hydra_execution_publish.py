"""Publish raw bars and a calendar only; never strategy weights or capital."""

import hashlib
import io
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select

from app.models import HydraExecutionPublication
from app.schemas.hydra_data import HydraDataManifest
from app.services.ledger_transaction import begin_ledger_transaction
from app.exceptions import APIError, ErrorCode


def publish_execution(service, req):
    raw = pd.DataFrame(req.bars)
    if (
        "symbol" not in raw
        or set(raw.symbol) != service.allowed_symbols
        or len(raw) != len(service.allowed_symbols)
    ):
        raise APIError(ErrorCode.BAD_REQUEST, "执行行情必须是 Hydra 白名单每标的一条当日不复权价格")
    raw = raw.sort_values("symbol").reset_index(drop=True)
    calendar = pd.DataFrame({"trade_date": sorted(set(req.calendar_dates))})
    calendar = calendar.loc[calendar.trade_date >= req.reference_date].reset_index(drop=True)
    now = datetime.now(timezone.utc).isoformat()
    hashes = []
    for stream, adjustment, frame in (
        ("hydra_execution_raw", "none", raw),
        ("hydra_trading_calendar", "calendar", calendar),
    ):
        output = io.BytesIO()
        frame.to_parquet(output, index=False)
        body = output.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        manifest = HydraDataManifest(
            stream=stream,
            source="qmt-live-execution",
            adjustment=adjustment,
            as_of_date=req.reference_date,
            fetched_at=now,
            producer_commit=req.producer_commit,
            file_sha256=digest,
            row_count=len(frame),
            symbol_count=len(raw) if adjustment == "none" else 0,
        )
        # Validate the actual bytes even on replay. Publication identity is
        # account-scoped separately, so equal public market bars can be shared.
        service.data_store._validate_frame(frame, manifest)
        if adjustment == "none":
            service._validate_execution_raw_universe(frame, manifest)
            service._require_as_of_coverage(
                frame, req.reference_date, service.allowed_symbols, stream
            )
        if (service.data_store.root / stream / digest / "manifest.json").exists():
            _, previous = service.data_store.load(stream, digest)
            if previous.as_of_date != req.reference_date:
                raise APIError(ErrorCode.BAD_REQUEST, "执行数据日期与现有不可变批次冲突")
        else:
            try:
                service.data_store.install(body, manifest)
            except ValueError:
                # Concurrent publication of identical market bytes can have
                # a different fetched_at. Reuse only a complete verified pair.
                _, previous = service.data_store.load(stream, digest)
                if previous.as_of_date != req.reference_date:
                    raise
        hashes.append(digest)
    publication_id = (
        "hep_"
        + hashlib.sha256(
            (req.account_alias + req.reference_date + "".join(hashes)).encode()
        ).hexdigest()
    )
    with service.session_factory() as session:
        begin_ledger_transaction(session, "live", req.account_alias)
        prior = session.get(HydraExecutionPublication, publication_id)
        if prior:
            return prior.response_payload
        result = dict(
            status="EXECUTION_DATA_PUBLISHED",
            publication_id=publication_id,
            reference_date=req.reference_date,
            execution_raw_sha256=hashes[0],
            execution_calendar_sha256=hashes[1],
        )
        session.add(
            HydraExecutionPublication(
                publication_id=publication_id,
                account_alias=req.account_alias,
                reference_date=req.reference_date,
                execution_raw_sha256=hashes[0],
                execution_calendar_sha256=hashes[1],
                producer_commit=req.producer_commit,
                observed_at=now,
                response_payload=result,
            )
        )
        session.commit()
    return result


def latest_execution_publication(service, account_alias, reference_date):
    with service.session_factory() as session:
        return session.scalar(
            select(HydraExecutionPublication)
            .where(
                HydraExecutionPublication.account_alias == account_alias,
                HydraExecutionPublication.reference_date == reference_date,
            )
            .order_by(
                HydraExecutionPublication.observed_at.desc(),
                HydraExecutionPublication.publication_id.desc(),
            )
            .limit(1)
        )
