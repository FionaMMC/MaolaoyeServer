"""Read the two already-valued live legs and atomically publish a synthetic report.

No valuation, schema, account, target or order writes. SQLite is opened mode=ro
and query_only. A missing dependency publishes an explicit WAITING document.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models import InstanceState, PerfSnapshot
from app.oms.models import V713StateRecord
from app.services.v713_synthetic import (SyntheticEvidenceUnavailable, load_inputs,
                                         load_synthetic_policy, run_synthetic)

CHINA = ZoneInfo("Asia/Shanghai")


def readonly_sessions(db_url):
    url = make_url(db_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("synthetic EOD requires an existing file-backed SQLite database")
    path = Path(url.database).resolve(strict=True)

    def connect():
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection

    engine = create_engine("sqlite://", creator=connect)
    return engine, sessionmaker(engine, expire_on_commit=False)


def expected_session(now, calendar_path=None):
    """Use a covered exchange calendar, never a weekday approximation."""
    today = now.astimezone(CHINA).strftime("%Y%m%d")
    if calendar_path is not None:
        raw = Path(calendar_path).read_bytes()
        evidence = json.loads(raw)
        if not evidence.get("source") or not (evidence["coverage_start"] <= today <= evidence["coverage_end"]):
            raise ValueError("calendar provenance or coverage is missing for today")
        dates = evidence["trading_dates"]
        receipt = {"source": evidence["source"], "sha256": hashlib.sha256(raw).hexdigest()}
    else:
        from scripts.v713_cycle import exchange_dates
        dates = exchange_dates(today)
        receipt = {"source": "Tushare SSE trade_cal", "requested_for": today}
    if not dates or any(not isinstance(day, str) or len(day) != 8 or
                        datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") != day for day in dates):
        raise ValueError("invalid exchange calendar dates")
    latest = [day for day in dates if day < today or (day == today and now.astimezone(CHINA).hour >= 16)]
    if not latest:
        raise ValueError("calendar has no completed trading session")
    return max(latest), receipt


def report(policy, sf, expected_date):
    with sf() as session:
        missing = [instance for instance in (policy.stock_instance, policy.hydra_instance)
                   if session.get(InstanceState, instance) is None]
        if missing:
            return "WAITING_INSTANCE", {"missing_instances": missing}, None
        for instance in (policy.stock_instance, policy.hydra_instance):
            state = session.get(InstanceState, instance)
            if state.execution_domain != "live" or state.ledger_mode != "attributed":
                return "WAITING_INSTANCE_CONFIGURATION", {"instance_id": instance}, None
        if session.scalar(select(V713StateRecord.signal_date).where(
                V713StateRecord.instance_id == policy.stock_instance).limit(1)) is None:
            return "WAITING_START", {"reason": "no_published_stock_leg_monthly_state"}, None
        missing = [instance for instance in (policy.stock_instance, policy.hydra_instance)
                   if session.scalar(select(PerfSnapshot.date).where(
                       PerfSnapshot.instance_id == instance, PerfSnapshot.execution_domain == "live",
                       PerfSnapshot.date == expected_date)) is None]
        if missing:
            return "WAITING_LEG_NAV", {"missing_nav_instances": missing}, None
    try:
        inputs = load_inputs(sf, policy)
        inputs["expected_date"] = expected_date
        inputs["dates"] = [day for day in inputs["dates"] if day <= expected_date]
        lines = {f"{bps:g}": run_synthetic(policy, bps=bps, **inputs) for bps in policy.cost_bps}
    except SyntheticEvidenceUnavailable as exc:
        return "WAITING_ORDER_EVIDENCE", {"missing_order_evidence": exc.order_ids}, None
    except ValueError as exc:
        return "WAITING_INPUT_EVIDENCE", {"reason": str(exc)}, None
    return lines[f"{policy.primary_bps:g}"]["status"], {}, lines


def main(argv=None, *, now=None, session_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--calendar", type=Path, default=os.environ.get("QMT_V713_SYNTHETIC_CALENDAR"),
                        help="Optional private calendar evidence JSON; otherwise query the existing exchange calendar API")
    args = parser.parse_args(argv)
    from app.settings import get_settings
    settings = get_settings()
    output = args.output or settings.v713_synthetic_output
    if output is None:
        parser.error("--output or QMT_V713_SYNTHETIC_OUTPUT is required")
    policy = load_synthetic_policy(args.policy)
    now = now or datetime.now(CHINA)
    if now.tzinfo is None:
        raise ValueError("now must include timezone")
    expected, calendar, lines, detail, engine = None, None, None, {}, None
    try:
        expected, calendar = expected_session(now, args.calendar)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        status, detail = "WAITING_TRADING_CALENDAR", {"reason": type(exc).__name__}
    else:
        try:
            if session_factory is None:
                engine, session_factory = readonly_sessions(settings.db_url)
            status, detail, lines = report(policy, session_factory, expected)
        finally:
            if engine is not None:
                engine.dispose()
    lines = lines or {f"{bps:g}": {"status": status, "rows": [], "events": []} for bps in policy.cost_bps}
    primary = lines[f"{policy.primary_bps:g}"]
    document = {"policy_id": policy.policy_id, "label": policy.label, "stock_instance": policy.stock_instance,
                "hydra_instance": policy.hydra_instance, "base_value": policy.base_value,
                "primary_bps": policy.primary_bps, "status": status, "generated_at": now.isoformat(),
                "expected_date": expected, "as_of_date": primary["rows"][-1]["date"] if primary["rows"] else None,
                "calendar": calendar, "lines": lines, "detail": detail, "database_read_only": True}
    # The private output directory is provisioned explicitly during deployment.
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                prefix=output.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(json.dumps({"status": status, "expected_date": expected, "as_of_date": document["as_of_date"],
                      "output": str(output), "database_read_only": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
