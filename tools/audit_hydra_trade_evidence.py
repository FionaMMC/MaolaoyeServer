"""Offline audit of the redacted September 4/8 evidence, never a repair tool.

Run with v2.3/server/venv/bin/python. No env/config loading, broker connections,
production DB or HTTP. The replay DB is constructed in a temporary directory.
Its baseline and default fee policy are explicit counterfactual assumptions,
not a recovered production database or a broker settlement statement.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import itertools
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "v2.3/server"), str(REPO / "v2.3")]


def no_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
        raise RuntimeError("Offline evidence audit prohibits network access")


sys.addaudithook(no_network)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def read(root, name):
    return json.loads((root / name).read_text(encoding="utf-8-sig"))


def total(rows, key):
    return float(sum((Decimal(str(r[key])) for r in rows), Decimal(0)))


def manifest_audit(root):
    results = []
    for entry in read(root, "manifest.json")["files"]:
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Manifest path escapes evidence directory")
        body = path.read_bytes()
        kind = (
            "EXACT"
            if sha(body) == entry["sha256"] and len(body) == entry["size"]
            else "UNRESOLVED"
        )
        normalized = body.replace(b"\r\n", b"\n")
        crlf = normalized.replace(b"\n", b"\r\n")
        if (
            kind == "UNRESOLVED"
            and sha(crlf) == entry["sha256"]
            and len(crlf) == entry["size"]
        ):
            kind = "CRLF_NORMALIZATION_ONLY"
        # A PowerShell transcript had mixed line endings before Git export.
        # Bounded reconstruction diagnoses formatting; input files stay intact.
        parts = normalized.split(b"\n")
        missing_cr = len(crlf) - entry["size"]
        if kind == "UNRESOLVED" and 0 < missing_cr <= 2 and len(parts) <= 100:
            for indices in itertools.combinations(range(len(parts) - 1), missing_cr):
                candidate = (
                    b"".join(
                        p + (b"\n" if i in indices else b"\r\n")
                        for i, p in enumerate(parts[:-1])
                    )
                    + parts[-1]
                )
                if sha(candidate) == entry["sha256"]:
                    kind = "MIXED_LINE_ENDINGS_ONLY"
                    break
        results.append(
            dict(path=entry["path"], status=kind, git_export_sha256=sha(body))
        )
    return dict(counts=dict(Counter(r["status"] for r in results)), files=results)


def replay(orders, target, ledger, qmt, root):
    from sqlalchemy import select
    from app.db import make_engine, make_session_factory, init_db
    from app.models import (
        HydraTarget,
        HydraRebalance,
        HydraExecutionAttempt,
        InstanceState,
        Order,
        RawSignal,
        OrderSignalMap,
        Trade,
    )
    from app.schemas.hydra_relay import HydraAttemptCloseRequest
    from app.schemas.trade_result import TradeResult
    from app.services.hydra_data import HydraDataStore
    from app.services.hydra_relay import HydraRelayService, HydraRiskLimits
    from app.services.settlement import SettlementService
    from live_client.qmt_day_order_policy import day_order_expiration

    first = orders[0]
    initial_positions = dict(ledger["positions"])
    desired = dict(initial_positions)
    for order in orders:
        desired[order["symbol"]] = desired.get(order["symbol"], 0) + order["quantity"]
    with tempfile.TemporaryDirectory(prefix="hydra-evidence-replay-") as directory:
        engine = make_engine(f"sqlite:///{directory}/isolated.db")
        try:
            init_db(engine)
            sf = make_session_factory(engine)
            with sf() as session:
                session.add(
                    InstanceState(
                        instance_id=target["instance_id"],
                        execution_domain="live",
                        account_alias=target["account_alias"],
                        ledger_mode="attributed",
                        virtual_cash=ledger["virtual_cash"],
                        virtual_positions=initial_positions,
                        last_update="replay-baseline",
                        strategy_state={"reconciliation_status": "attributed_ledger"},
                        owned_symbols=sorted(desired),
                    )
                )
                session.add(
                    HydraTarget(
                        target_id=first["target_id"],
                        **{
                            k: target[k]
                            for k in (
                                "execution_domain",
                                "account_alias",
                                "strategy_version",
                                "publisher_source_commit",
                                "decision_date",
                                "as_of_date",
                                "execution_date",
                                "basket_sha256",
                                "research_input_hashes",
                                "input_hashes",
                                "cash_buffer_weight",
                            )
                        },
                        weights={w["code"]: w["weight"] for w in target["weights"]},
                        status="STAGED",
                        created_at="replay",
                    )
                )
                session.add(
                    HydraRebalance(
                        rebalance_id=first["rebalance_id"],
                        target_id=first["target_id"],
                        execution_domain="live",
                        account_alias=target["account_alias"],
                        baseline_cash=ledger["virtual_cash"],
                        baseline_positions=initial_positions,
                        target_shares=desired,
                        status="ACTIVE",
                        reconciliation_status="REPLAY_BASELINE",
                        created_at="replay",
                    )
                )
                session.add(
                    HydraExecutionAttempt(
                        attempt_id=first["attempt_id"],
                        rebalance_id=first["rebalance_id"],
                        execution_domain="live",
                        account_alias=target["account_alias"],
                        attempt_number=1,
                        trade_date=target["execution_date"],
                        residual_before={o["symbol"]: o["quantity"] for o in orders},
                        batch_id=first["batch_id"],
                        batch_sha256=first["batch_sha256"],
                        risk_snapshot={},
                        status="PENDING",
                        created_at="replay",
                    )
                )
                for o in orders:
                    session.add(Order(**o, status="PENDING", created_at="replay"))
                    session.add(
                        RawSignal(
                            signal_id=o["order_id"],
                            execution_domain="live",
                            instance_id=target["instance_id"],
                            symbol=o["symbol"],
                            direction=o["direction"],
                            quantity=o["quantity"],
                            reference_price=o["execution_reference_price"],
                            price_offset=0.005,
                            limit_price=o["limit_price"],
                            valid_date=o["valid_date"],
                            signal_time="replay",
                            precheck_status="PASS",
                        )
                    )
                    session.add(
                        OrderSignalMap(
                            order_id=o["order_id"],
                            signal_id=o["order_id"],
                            signal_quantity=o["quantity"],
                        )
                    )
                session.commit()

            broker_by_symbol = {
                r["symbol_code"]: r for r in qmt["orders_final_observation"]
            }
            results = []
            for o in orders:
                b = broker_by_symbol[o["symbol"].split(".")[0]]
                observed = datetime.strptime(
                    b["observed_at"], "%Y-%m-%d %H:%M:%S,%f"
                ).replace(tzinfo=timezone(timedelta(hours=8)))
                policy = day_order_expiration(
                    qmt_status=b["order_status_code"],
                    filled_quantity=b["traded_quantity"],
                    ordered_quantity=b["quantity"],
                    valid_date=o["valid_date"],
                    observed_at=observed,
                )
                if policy is None and not (
                    b["order_status_code"] == 56
                    and b["traded_quantity"] == b["quantity"]
                ):
                    raise ValueError(
                        "Evidence contains an unsupported terminal observation"
                    )
                results.append(
                    TradeResult(
                        order_id=o["order_id"],
                        filled_quantity=b["traded_quantity"],
                        filled_price=b["traded_average_price"],
                        status="EXPIRED_BY_POLICY" if policy else "FILLED",
                        symbol=o["symbol"],
                        direction=o["direction"],
                        qmt_order_id=str(b["qmt_order_id"]),
                        raw_qmt_status=b["order_status_code"],
                        status_observed_at=observed,
                        expiration_policy_id="QMT_DAY_ORDER_1500_V1"
                        if policy
                        else None,
                    )
                )
            settlement = SettlementService(sf)
            first_pass = settlement.settle(
                "20260904", results, "live", (target["account_alias"],)
            )

            def snapshot():
                with sf() as session:
                    state = session.get(InstanceState, target["instance_id"])
                    return dict(
                        cash=state.virtual_cash,
                        positions=state.virtual_positions,
                        trade_rows=len(session.scalars(select(Trade)).all()),
                    )

            after = snapshot()
            repeated = settlement.settle(
                "20260904", results, "live", (target["account_alias"],)
            )
            assert snapshot() == after, "Repeat settlement changed money or positions"
            assert (
                first_pass.matched_count == len(orders)
                and not first_pass.rejected_observations
            )
            service = HydraRelayService(
                sf,
                HydraDataStore(directory),
                allowed_symbols=set(desired),
                allowed_publisher_commits={target["publisher_source_commit"]},
                live_enabled=False,
                live_limits=HydraRiskLimits(9, 1e6, 1e6, 1e6, 2e6, 50),
            )
            # Close is checked against the isolated projection, NOT a claimed
            # whole-account historical snapshot, which this public pack lacks.
            close = service.close_attempt(
                HydraAttemptCloseRequest(
                    execution_domain="live",
                    account_alias=target["account_alias"],
                    attempt_id=first["attempt_id"],
                    actual_cash=after["cash"],
                    actual_positions=after["positions"],
                    reconciliation_evidence_sha256=sha(
                        (root / "qmt/historical_log_extraction.json").read_bytes()
                    ),
                )
            )
            return dict(
                scope="actual exported final observations through new settlement/expiry/close in isolated seeded DB",
                baseline_assumption="September 11 stale owned ledger is used as pre-fill baseline; no intervening owned cash flows/fees assumed",
                fee_policy="code defaults: 0.03%, minimum 5 per order; NOT verified broker fees",
                snapshot_after=after,
                first_pass=first_pass.model_dump(mode="json"),
                repeated_matched=repeated.matched_count,
                repeat_does_not_change_ledger=True,
                close=close.model_dump(mode="json"),
                no_order_generation_attempted=True,
            )
        finally:
            engine.dispose()


def audit(root):
    target = read(root, "target/target-request.json")
    local = read(root, "local/state_rows.json")
    orders = read(root, "server/orders.json")["body"]["data"]["orders"]
    qmt = read(root, "qmt/historical_log_extraction.json")
    manual = read(root, "qmt/manual-residual-20260908.json")
    ledger = read(root, "server/strategy-ledger-current.json")["body"]["data"]
    from app.schemas.hydra_relay import HydraTargetRequest

    HydraTargetRequest(**target)  # Includes independent basket hash check.
    canonical = [
        {
            "symbol": o["symbol"],
            "direction": o["direction"],
            "quantity": o["quantity"],
            "reference_price": o["execution_reference_price"],
            "limit_price": o["limit_price"],
        }
        for o in orders
    ]
    batch_hash = sha(
        json.dumps(
            dict(
                rebalance_id=orders[0]["rebalance_id"],
                attempt_number=1,
                trade_date="20260904",
                orders=sorted(canonical, key=lambda x: x["symbol"]),
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    assert batch_hash == local["order_batch_payload"]["batch_sha256"]
    order_map = {o["order_id"]: o for o in orders}
    assert len(order_map) == len(orders)
    assert {
        o["order_id"]: o for o in local["order_batch_payload"]["orders"]
    } == order_map
    submissions = {s["order_id"]: s for s in local["submissions"]}
    assert set(submissions) == set(order_map)
    broker = {str(b["qmt_order_id"]): b for b in qmt["orders_final_observation"]}
    fills = defaultdict(list)
    seen = set()
    for t in qmt["trades_deduplicated"]:
        key = (t["qmt_order_id"], t["trade_id"])
        assert key not in seen
        seen.add(key)
        assert abs(t["quantity"] * t["price"] - t["amount"]) < 0.005
        fills[str(t["qmt_order_id"])].append(t)
    manual_by_symbol = {r["symbol"]: r for r in manual["orders_final"]}
    manual_trades = defaultdict(list)
    for t in manual["trades_deduplicated"]:
        assert abs(t["quantity"] * t["price"] - t["amount"]) < 0.005
        manual_trades[t["qmt_order_id"]].append(t)
    nav = ledger["virtual_cash"] + sum(
        ledger["positions"].get(o["symbol"], 0) * o["execution_reference_price"]
        for o in orders
    )
    rows = []
    for o in sorted(orders, key=lambda x: x["symbol"]):
        assert o["direction"] == "BUY" and o["batch_sha256"] == batch_hash
        s = submissions[o["order_id"]]
        b = broker[s["local_order_id"]]
        assert b["symbol_code"] == o["symbol"].split(".")[0]
        assert b["quantity"] == o["quantity"] and b["limit_price"] == o["limit_price"]
        assert o["order_id"].startswith(b["strategy_name"].split("|")[0])
        assert o["target_hash"].startswith(b["strategy_name"].split("|")[1])
        assert o["target_hash"] == target["basket_sha256"]
        f = fills[s["local_order_id"]]
        assert sum(t["quantity"] for t in f) == b["traded_quantity"]
        assert abs(total(f, "amount") - b["traded_amount"]) < 0.005
        assert all(t["price"] <= o["limit_price"] + 1e-8 for t in f)
        offset = (o["limit_price"] / o["execution_reference_price"] - 1) * 10000
        assert offset <= 50 + 1e-6
        m = manual_by_symbol.get(b["symbol_code"])
        mq = m["traded_quantity"] if m else 0
        if m:
            mt = manual_trades[m["qmt_order_id"]]
            assert m["status_code"] == 56 and mq == m["quantity"]
            assert sum(t["quantity"] for t in mt) == mq
            assert abs(total(mt, "amount") - m["traded_amount"]) < 0.005
            assert all(t["price"] <= m["limit_price"] for t in mt)
        weight = next(
            w["weight"] for w in target["weights"] if w["code"] == o["symbol"]
        )
        projected = math.floor(
            nav * weight / (o["execution_reference_price"] * 1.005 * 1.001) / 100
        ) * 100 - ledger["positions"].get(o["symbol"], 0)
        expected_position = (
            ledger["positions"].get(o["symbol"], 0) + b["traded_quantity"] + mq
        )
        rows.append(
            dict(
                symbol=o["symbol"],
                ordered=o["quantity"],
                sept4_filled=b["traded_quantity"],
                sept4_amount=total(f, "amount"),
                sept4_trade_fragments=len(f),
                raw_status=b["order_status_code"],
                raw_observed_at=b["observed_at"],
                manual_filled=mq,
                manual_amount=m["traded_amount"] if m else 0,
                remaining_original_quantity=o["quantity"] - b["traded_quantity"] - mq,
                expected_owned_position_if_no_other_flows=expected_position,
                reported_owned_position=ledger["positions"].get(o["symbol"], 0),
                max_buy_offset_bps=offset,
                sizing_reproduced_with_assumed_baseline=(projected == o["quantity"]),
            )
        )
    replay_result = replay(orders, target, ledger, qmt, root)
    expected_residual = {
        r["symbol"]: r["ordered"] - r["sept4_filled"]
        for r in rows
        if r["ordered"] != r["sept4_filled"]
    }
    assert replay_result["close"]["residual_after"] == expected_residual
    return dict(
        evidence_commit="89964bd9ff598bf9037fd91dd3195c5d5d3367fb",
        code_commit=subprocess.check_output(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True
        ).strip(),
        network_disabled=True,
        manifest=manifest_audit(root),
        basket_hash_pass=True,
        batch_hash_pass=True,
        local_server_order_identity_pass=True,
        known_broker_mapping_count=len(rows),
        observations_are_redacted_log_extracts_not_independently_verified_originals=True,
        amounts=dict(
            sept4=total(qmt["trades_deduplicated"], "amount"),
            sept8=total(manual["trades_deduplicated"], "amount"),
        ),
        local_settlement_push_count=len(local["settlement_pushes"]),
        local_workflow_receipt_count=len(local["workflow_receipts"]),
        ledger_retrieved_at=read(root, "server/strategy-ledger-current.json")[
            "retrieved_at"
        ],
        ledger_last_update=ledger["last_update"],
        sizing_baseline_nav_assumed=nav,
        all_original_order_quantities_completed=all(
            r["remaining_original_quantity"] == 0 for r in rows
        ),
        no_sell_orders_in_evidence=True,
        orders=rows,
        isolated_replay=replay_result,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.evidence_dir.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "AUDIT_COMPLETED_NOT_PRODUCTION_ACCEPTANCE",
                "output": str(args.output),
                "amounts": result["amounts"],
                "manifest": result["manifest"]["counts"],
                "close": result["isolated_replay"]["close"],
                "all_original_order_quantities_completed": result[
                    "all_original_order_quantities_completed"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
