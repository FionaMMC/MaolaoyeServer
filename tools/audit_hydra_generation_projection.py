"""Historical generation probe using real target weights and order references.

With --frozen-batches, authenticates and loads all four original parquet files.
Without it this is NOT a four-stream replay: the public evidence omits those
files. ProjectedStore is explicitly a test double at the data-loading boundary.
It supplies observed raw closes, assumed model coverage/non-suspension/calendar,
and an empty action projection. It never authenticates those projections with
the original hashes and never writes them into a real HydraDataStore.

The actual stage_initial, risk, queue and settlement code runs unmodified in a
temporary database. No production configuration, network, QMT or writes outside
the requested local output. This is a diagnostic, not a repair/deployment tool.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

from audit_hydra_trade_evidence import REPO, read, sha  # installs network denial
from hydra_source_identity import source_identity

import pandas as pd
from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory
from app.models import HydraExecutionAttempt, InstanceState, Order
from app.schemas.hydra_relay import HydraAttemptCloseRequest, HydraTargetRequest
from app.schemas.trade_result import TradeResult
from app.services.hydra_data import HydraDataStore
from app.services.hydra_relay import HydraRelayService, HydraRiskLimits
from app.services.orders_queue import OrdersQueueService
from app.services.ownership import validate_no_owned_symbol_overlap
from app.services.settlement import SettlementService
from live_client.qmt_day_order_policy import day_order_expiration


class ProjectedStore:
    """Unverified coverage assumptions; raw close values come from real orders."""

    def __init__(self, target, historical):
        self.available = False
        self.target = target
        date = target["as_of_date"]
        coverage = pd.DataFrame(
            {"symbol": o["symbol"], "trade_date": date} for o in historical
        )
        raw = pd.DataFrame(
            {
                "symbol": o["symbol"],
                "trade_date": date,
                "close": o["execution_reference_price"],
                "suspendFlag": 0,  # assumed, not recovered from original raw bars
            }
            for o in historical
        )
        self.frames = {
            "hydra_model_hfq": coverage,  # no invented adjusted price history
            "hydra_execution_raw": raw,
            "hydra_corporate_actions": pd.DataFrame(columns=["symbol"]),
            "hydra_trading_calendar": pd.DataFrame(
                {"trade_date": [date, target["execution_date"]]}
            ),
        }

    def load(self, stream, digest):
        if not self.available:
            raise FileNotFoundError("PROBE: missing data before evening recovery")
        assert digest == self.target["input_hashes"][stream.removeprefix("hydra_")]
        return self.frames[stream].copy(), SimpleNamespace(
            as_of_date=self.target["as_of_date"],
            stream=stream,
            executable_symbols=None,
            research_only_symbols=None,
            file_sha256=digest,  # identity label ONLY, bytes NOT authenticated
        )


def run(root, frozen_batches=None, server_evidence=None):
    target = read(root, "target/target-request.json")
    historical = read(root, "server/orders.json")["body"]["data"]["orders"]
    ledger = read(root, "server/strategy-ledger-current.json")["body"]["data"]
    if server_evidence:
        evidence = json.loads(server_evidence.read_text())
        baseline = next(
            r
            for r in evidence["rebalances"]
            if r["rebalance_id"] == historical[0]["rebalance_id"]
        )
        ledger = {
            **ledger,
            "virtual_cash": baseline["baseline_cash"],
            "positions": json.loads(baseline["baseline_positions"]),
        }
    req = HydraTargetRequest.model_validate(target)
    symbols = {o["symbol"] for o in historical}
    weights = pd.read_parquet(root / "target/Hydra_latest.parquet")
    parquet_weights = dict(zip(weights["code"], weights["weight"]))
    assert all(abs(parquet_weights[w.code] - w.weight) < 1e-12 for w in req.weights)
    peer_id = "probe_kobold_not_a_deployed_strategy"
    peer_cash, peer_positions = 50_000.0, {"510300.SH": 500}
    physical = SimpleNamespace(
        actual_cash=19_148_943.85,
        actual_positions={"510300.SH": ledger["positions"]["510300.SH"] + 500},
    )  # physical portfolio is an explicit what-if, not a historical observation

    with tempfile.TemporaryDirectory(
        prefix="hydra-generation-projection-"
    ) as directory:
        engine = make_engine(f"sqlite:///{directory}/isolated.db")
        try:
            init_db(engine)
            sf = make_session_factory(engine)
            with sf() as session:
                for instance, cash, positions in (
                    (
                        target["instance_id"],
                        ledger["virtual_cash"],
                        ledger["positions"],
                    ),
                    (peer_id, peer_cash, peer_positions),
                ):
                    session.add(
                        InstanceState(
                            instance_id=instance,
                            execution_domain="live",
                            account_alias=target["account_alias"],
                            ledger_mode="attributed",
                            virtual_cash=cash,
                            virtual_positions=positions,
                            owned_symbols=sorted(symbols),
                            last_update="isolated-probe",
                            strategy_state={
                                "reconciliation_status": "attributed_ledger"
                            },
                        )
                    )
                validate_no_owned_symbol_overlap(session)
                session.commit()
            service = HydraRelayService(
                sf,
                HydraDataStore(directory),
                allowed_symbols=symbols,
                allowed_publisher_commits={target["publisher_source_commit"]},
                live_enabled=True,
                live_limits=HydraRiskLimits(
                    9,
                    0,
                    0,
                    0,
                    0,
                    0,
                    mode="auto",
                    auto_max_daily_orders=9,
                ),
            )
            try:
                service.stage_initial(req, physical_snapshot=physical)
            except FileNotFoundError as exc:
                authenticated_replay = {
                    "status": "MISSING_ORIGINAL_STREAMS",
                    "error": str(exc),
                }
            else:
                raise AssertionError(
                    "Empty store unexpectedly accepted original inputs"
                )

            if frozen_batches:
                from app.schemas.hydra_data import HydraDataManifest

                for name, digest in target["input_hashes"].items():
                    path = frozen_batches / ("hydra_" + name) / digest
                    manifest = HydraDataManifest.model_validate_json(
                        (path / "manifest.json").read_text()
                    )
                    service.data_store.install(
                        (path / "data.parquet").read_bytes(), manifest
                    )
                authenticated_replay = {
                    "status": "VERIFIED_ORIGINAL_STREAMS",
                    "hashes": target["input_hashes"],
                }
            else:
                projection = ProjectedStore(target, historical)
                service.data_store = projection
                try:
                    service.stage_initial(req, physical_snapshot=physical)
                except FileNotFoundError:
                    pass
                else:
                    raise AssertionError(
                        "Missing projected input must not create a batch"
                    )
                projection.available = True
            with sf() as session:
                assert len(session.scalars(select(Order)).all()) == 0
            first = service.stage_initial(req, physical_snapshot=physical)
            queue = OrdersQueueService(sf)
            generated = queue.list_pending(
                "20260904", "live", (target["account_alias"],)
            )
            assert len(generated) == 9
            original_by_symbol = {o["symbol"]: o for o in historical}
            differences = []
            for order in generated:
                original = original_by_symbol[order.symbol]
                for key in (
                    "symbol",
                    "direction",
                    "quantity",
                    "limit_price",
                    "execution_reference_price",
                    "valid_date",
                ):
                    if getattr(order, key) != original[key]:
                        differences.append(
                            {
                                "symbol": order.symbol,
                                "field": key,
                                "old": original[key],
                                "new": getattr(order, key),
                            }
                        )
            assert differences == [], differences
            second = service.stage_initial(req, physical_snapshot=physical)
            assert (
                second.idempotent_replay and first.batch_sha256 == second.batch_sha256
            )
            assert {o.order_id for o in generated} == {
                o.order_id
                for o in queue.list_pending(
                    "20260904", "live", (target["account_alias"],)
                )
            }
            with sf() as session:
                attempt = session.get(HydraExecutionAttempt, first.attempt_id)
                risk = dict(attempt.risk_snapshot)
                assert risk["available_cash"] == round(ledger["virtual_cash"], 6)
                assert risk["nav"] < 212_000
                assert len(session.scalars(select(Order)).all()) == 9
            # Real known 510300 fill split (400 + 2100), synthetic replay timing.
            # The second strategy is hypothetical; this tests ownership, not QMT.
            own_order = next(o for o in generated if o.symbol == "510300.SH")
            settlement = SettlementService(sf)
            for qty, status in ((400, "PARTIAL"), (2500, "FILLED")):
                result = settlement.settle(
                    "20260904",
                    [
                        TradeResult(
                            order_id=own_order.order_id,
                            filled_quantity=qty,
                            filled_price=4.644,
                            status=status,
                            symbol="510300.SH",
                            direction="BUY",
                        )
                    ],
                    "live",
                    (target["account_alias"],),
                )
                assert result.matched_count == 1 and not result.rejected_observations
            with sf() as session:
                peer = session.get(InstanceState, peer_id)
                hydra = session.get(InstanceState, target["instance_id"])
                assert (
                    peer.virtual_cash == peer_cash
                    and peer.virtual_positions == peer_positions
                )
                assert hydra.virtual_positions["510300.SH"] == 2600

            # Bind observed outcomes to the newly generated diagnostic orders
            # only in this isolated DB, by the already-verified symbol/quantity.
            # These are NOT broker-approved identities for production repair.
            qmt = read(root, "qmt/historical_log_extraction.json")
            broker = {b["symbol_code"]: b for b in qmt["orders_final_observation"]}
            outcomes = []
            for order in generated:
                b = broker[order.symbol.split(".")[0]]
                observed = datetime.strptime(
                    b["observed_at"], "%Y-%m-%d %H:%M:%S,%f"
                ).replace(tzinfo=timezone(timedelta(hours=8)))
                expired = day_order_expiration(
                    qmt_status=b["order_status_code"],
                    filled_quantity=b["traded_quantity"],
                    ordered_quantity=b["quantity"],
                    valid_date=order.valid_date,
                    observed_at=observed,
                )
                assert expired or b["order_status_code"] == 56
                outcomes.append(
                    TradeResult(
                        order_id=order.order_id,
                        filled_quantity=b["traded_quantity"],
                        filled_price=b["traded_average_price"],
                        status="EXPIRED_BY_POLICY" if expired else "FILLED",
                        symbol=order.symbol,
                        direction=order.direction,
                        qmt_order_id=str(b["qmt_order_id"]),
                        raw_qmt_status=b["order_status_code"],
                        status_observed_at=observed,
                        expiration_policy_id="QMT_DAY_ORDER_1500_V1"
                        if expired
                        else None,
                    )
                )
            final = settlement.settle(
                "20260904", outcomes, "live", (target["account_alias"],)
            )
            assert not final.rejected_observations and not final.unmatched_order_ids, (
                final.model_dump(mode="json")
            )

            def balances():
                with sf() as session:
                    return {
                        i: (
                            session.get(InstanceState, i).virtual_cash,
                            session.get(InstanceState, i).virtual_positions,
                        )
                        for i in (target["instance_id"], peer_id)
                    }

            after = balances()
            again = settlement.settle(
                "20260904", outcomes, "live", (target["account_alias"],)
            )
            assert again.matched_count == 0 and balances() == after
            assert after[peer_id] == (peer_cash, peer_positions)
            own_cash, own_positions = after[target["instance_id"]]
            total_positions = dict(own_positions)
            total_positions["510300.SH"] += 500
            close = service.close_attempt(
                HydraAttemptCloseRequest(
                    execution_domain="live",
                    account_alias=target["account_alias"],
                    attempt_id=first.attempt_id,
                    actual_cash=own_cash + peer_cash,
                    actual_positions=total_positions,
                    reconciliation_evidence_sha256=sha(
                        (root / "qmt/historical_log_extraction.json").read_bytes()
                    ),
                )
            )
            assert close.status == "RESIDUAL" and close.residual_after == {
                "518880.SH": 800,
                "513500.SH": 1200,
                "513100.SH": 1000,
            }

            return {
                "status": "FROZEN_GENERATION_PASS_NOT_PRODUCTION_ACCEPTANCE"
                if frozen_batches
                else "CONDITIONAL_GENERATION_PASS_NOT_MIGRATION_APPROVAL",
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "code_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
                ).strip(),
                "actual_worktree": source_identity(REPO),
                "evidence_commit": "89964bd9ff598bf9037fd91dd3195c5d5d3367fb",
                "target_file_sha256": sha(
                    (root / "target/Hydra_latest.parquet").read_bytes()
                ),
                "target_parquet_matches_request_weights": True,
                "authenticated_four_stream_replay": authenticated_replay,
                "research_weights_recomputed": False,
                "projected_store_replaces_authenticated_load": not bool(frozen_batches),
                "baseline_from_production_rebalance": bool(server_evidence),
                "assumptions": [
                    *(
                        []
                        if frozen_batches
                        else [
                            "Coverage metadata, no suspension, two-date calendar and action coverage are unverified projections."
                        ]
                    ),
                    *(
                        []
                        if server_evidence
                        else [
                            "Stale exported ledger cash/position is a presumed pre-fill baseline, not proven full historical state."
                        ]
                    ),
                    "Second attributed strategy and aggregate physical snapshot are synthetic ownership scenarios.",
                    "Fill split quantities are real; intermediate delivery timing and statuses are simulated.",
                    "Historical outcomes rebound to diagnostic IDs only in temporary DB; close uses calculated portfolio, not independent broker reconciliation.",
                ],
                "generated_order_count": len(generated),
                "semantic_differences": differences,
                "generated_orders": [
                    {
                        k: getattr(o, k)
                        for k in (
                            "symbol",
                            "direction",
                            "quantity",
                            "limit_price",
                            "execution_reference_price",
                            "valid_date",
                        )
                    }
                    for o in generated
                ],
                "risk_snapshot": risk,
                "missing_input_then_same_request_recovery": True,
                "rerun_after_fetch_preserves_generated_order_ids": True,
                "peer_same_etf_cash_and_positions_unchanged_after_partial_and_final": True,
                "generated_orders_then_historical_outcomes_then_close": close.model_dump(
                    mode="json"
                ),
                "duplicate_final_outcomes_do_not_change_ledger": True,
                "new_batch_sha256": first.batch_sha256,
                "historical_batch_sha256": historical[0]["batch_sha256"],
                "warning": "New policy-bound IDs are not a migration replacement for historical IDs. Do not submit these diagnostic orders.",
            }
        finally:
            engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frozen-batches", type=Path)
    parser.add_argument("--server-evidence", type=Path)
    args = parser.parse_args()
    result = run(
        args.evidence_root.resolve(), args.frozen_batches, args.server_evidence
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "orders": result["generated_order_count"],
                "differences": result["semantic_differences"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
