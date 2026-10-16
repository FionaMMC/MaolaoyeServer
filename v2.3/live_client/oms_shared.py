"""Account-scoped OMS execution. Every strategy spends only its own capacity."""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal, ROUND_CEILING
import math

from live_client.gateway import QMT_STOCK_BUY, QMT_STOCK_SELL, validate_limit_order
from live_client.oms_journal import JournalConflict, plan_content_sha256


def order_cost(quantity, price, spec=None):
    spec = spec or {}
    rate = spec.get("buy_reserve_rate")
    if rate is None:
        rate = spec.get("commission_rate", .001)
    notional = Decimal(str(price)) * quantity
    fee = max(Decimal(str(spec.get("min_commission", 5))), notional * Decimal(str(rate)))
    fee += notional * Decimal(str(spec.get("transfer_fee_rate", 0)))
    return float((notional + fee).quantize(Decimal(".01"), rounding=ROUND_CEILING))


def require_guard(agent, item, quantity, *, allowed_symbols, spec=None, policy=None):
    """One entrypoint for both scheduled and manual broker submissions."""
    cfg = agent.cfg
    validate_limit_order(item["side"], quantity, item["limit_price"])
    if item["symbol"] not in allowed_symbols:
        raise ValueError("symbol is outside the approved execution grant")
    price = float(item["limit_price"])
    if spec:
        tick = float(spec["price_tick"])
        if tick <= 0 or not math.isfinite(tick) or abs(price / tick - round(price / tick)) > 1e-6:
            raise ValueError("price is not on the instrument tick")
        for key, predicate in (("up_limit", lambda v: price > v + 1e-8),
                               ("down_limit", lambda v: price < v - 1e-8)):
            if spec.get(key) is not None and predicate(float(spec[key])):
                raise ValueError("price is outside the instrument price band")
        if item["side"] == "BUY":
            lot = int(spec["buy_lot"])
            if lot <= 0 or quantity % lot or quantity < int(spec.get("min_buy_quantity", lot)):
                raise ValueError("buy quantity is outside the instrument lot rules")
    if cfg is None:  # injected synthetic agents; production always supplies the config
        return
    cfg.require_submission_enabled()
    if agent.shared and cfg.mode == "live":
        from live_client.oms_agent import CHINA
        rules = agent.gateway.execution_rules(item["symbol"])
        if not spec or abs(float(rules["price_tick"]) - float(spec["price_tick"])) > 1e-10:
            raise ValueError("current broker price tick differs from approved instrument")
        if not float(rules["down_limit"]) <= price <= float(rules["up_limit"]):
            raise ValueError("limit is outside current broker price bands")
        stamp = rules.get("source_time")
        source = datetime.fromisoformat(stamp) if stamp else None
        if source is None or source.utcoffset() is None or source.astimezone(CHINA).date() != agent.clock().astimezone(CHINA).date():
            raise ValueError("current-day broker rules could not be verified")
    today = agent.clock().strftime("%Y%m%d")
    rows = [r for r in agent.journal.submissions() if r["trade_date"] == today and r["state"] != "REJECTED"]
    max_count = cfg.auto_max_daily_orders if cfg.risk_mode == "auto" else cfg.max_daily_orders
    if len(rows) >= max_count:
        raise ValueError("daily order count limit")
    notional = quantity * price
    totals = {side: sum(r["quantity"] * r["limit_price"] for r in rows if r["side"] == side)
              for side in ("BUY", "SELL")}
    limits = ((cfg.max_single_order_notional, notional),
              (cfg.max_daily_buy_notional, totals["BUY"] + (notional if item["side"] == "BUY" else 0)),
              (cfg.max_daily_sell_notional, totals["SELL"] + (notional if item["side"] == "SELL" else 0)),
              (cfg.max_daily_turnover_notional, sum(totals.values()) + notional))
    if any(limit > 0 and value > limit + 1e-8 for limit, value in limits):
        raise ValueError("configured notional risk limit")
    references = (policy or {}).get("reference_prices", {})
    reference = references.get(item["symbol"])
    if reference is not None and cfg.effective_price_offset_bps > 0:
        if float(reference) <= 0 or abs(price / float(reference) - 1) * 10000 > cfg.effective_price_offset_bps + 1e-6:
            raise ValueError("configured price-offset risk limit")


def validate_grant(agent, plan, now, phase):
    if plan.get("schema_version") != 2 or plan_content_sha256(plan) != plan["plan_sha256"]:
        raise JournalConflict("shared execution requires a verified v2 plan")
    if plan["account_alias"] != agent.account_alias or plan["account_fingerprint"] != agent.cfg.expected_account_sha256:
        raise JournalConflict("plan account identity mismatch")
    if plan["phase"] != phase or plan["trade_date"] != now.strftime("%Y%m%d"):
        raise JournalConflict("plan session identity mismatch")
    if phase in ("BUY", "SELL") and any(o["side"] != phase for o in plan["orders"]):
        raise JournalConflict("order side does not match its session")
    for field in ("valid_until", "lease_expires_at"):
        expiry = datetime.fromisoformat(plan[field])
        if expiry.utcoffset() is None or now > expiry:
            raise JournalConflict(f"expired execution grant: {field}")
    if not plan.get("instance_id"):
        raise JournalConflict("plan is missing strategy identity")
    allowed = set(plan["allowed_symbols"])
    if any(o["symbol"] not in allowed or o["symbol"] not in plan["instrument_specs"] for o in plan["orders"]):
        raise JournalConflict("plan order is missing approved instrument metadata")
    for field in ("strategy_positions", "strategy_sellable"):
        values = plan[field]
        if any(type(v) is not int or v < 0 for v in values.values()):
            raise JournalConflict("invalid strategy quantities")
    if not math.isfinite(float(plan["strategy_cash"])) or float(plan["strategy_cash"]) < 0:
        raise JournalConflict("invalid strategy cash")


def broker_matches(order, item, quantity, account_id=None):
    return (order.stock_code == item["symbol"]
            and int(order.order_type) == (QMT_STOCK_BUY if item["side"] == "BUY" else QMT_STOCK_SELL)
            and int(order.order_volume) == quantity
            and abs(float(order.price) - float(item["limit_price"])) < 1e-7
            and (account_id is None or order.account_id == account_id))


def execute_bundle(agent, trade_date, phase, *, plans=None, dry_run=False):
    from live_client.oms_agent import CHINA, WINDOWS, MANUAL_WINDOW
    now = agent.clock().astimezone(CHINA)
    if agent.journal.hold_flag():
        return {"status": "HOLD_LOCAL", "plans": []}
    plans = agent.fetch_plans(trade_date, phase) if plans is None else plans
    if not plans:
        return {"status": "NO_PLAN", "plans": []}
    # A malformed grant aborts the bundle before any broker call.
    for plan in plans:
        validate_grant(agent, plan, now, phase)
    account = agent.gateway.account_snapshot()
    by_remark = {}
    for order in agent.gateway.day_orders():
        by_remark.setdefault((order.order_remark or "").strip(), []).append(order)
    submissions = {r["client_order_id"]: r for r in agent.journal.submissions()}
    physical_cash = float(account.available_cash)
    physical_sellable = dict(account.sellable_positions)
    terminal_proofs = {coid: proof for p in plans for coid, proof in p.get("server_terminal_orders", {}).items()}
    remaining_reservations = []
    for coid, row in submissions.items():
        if row["state"] == "REJECTED":
            continue
        matches = by_remark.get(coid, [])
        if len(matches) > 1 or (matches and not broker_matches(matches[0], row, row["quantity"], account.account_id)):
            raise JournalConflict("broker remark collision or mismatched order identity")
        if not matches:
            if coid in terminal_proofs:
                agent.journal.record_terminal_proof(coid, terminal_proofs[coid])
            if agent.journal.terminal_proof(coid):
                continue
            if row["trade_date"] != trade_date:
                if row["state"] in ("SUBMITTING", "UNKNOWN"):
                    return {"status": "UNRESOLVED_PRIOR_DAY", "plans": []}
                continue
            remaining = row["quantity"]
            filled = 0
            if row["side"] == "BUY":
                physical_cash -= row["reserve_cash"]
            else:
                physical_sellable[row["symbol"]] = max(0, physical_sellable.get(row["symbol"], 0) - remaining)
        else:
            broker = matches[0]
            filled = int(broker.traded_volume)
            remaining = max(0, row["quantity"] - int(broker.traded_volume)) if int(broker.order_status) in (48, 49, 50, 51, 52, 55) else 0
        remaining_reservations.append({**row, "remaining": remaining, "filled": filled})
    outputs = []
    for plan in sorted(plans, key=lambda p: p["instance_id"]):
        instance = plan["instance_id"]
        if agent.journal.hold_flag(instance):
            outputs.append({"instance_id": instance, "status": "HOLD_STRATEGY", "orders": []})
            continue
        if not dry_run and not plan["executable"]:
            outputs.append({"instance_id": instance, "status": "NOT_EXECUTABLE", "orders": []})
            continue
        policy = plan["policy"]
        submit_at = policy.get("submit_at", {}).get(phase)
        if not dry_run and submit_at and now.time() < time.fromisoformat(submit_at):
            agent.wait_until(time.fromisoformat(submit_at))
            now = agent.clock().astimezone(CHINA)
            validate_grant(agent, plan, now, phase)
        window = policy.get("execution_windows", {}).get(phase)
        start, end = [time.fromisoformat(t) for t in window] if window else (MANUAL_WINDOW if phase == "MANUAL" else WINDOWS[phase])
        if not dry_run and not start <= now.time() <= end:
            outputs.append({"instance_id": instance, "status": "OUTSIDE_WINDOW", "orders": []})
            continue
        server_reserved = set(plan.get("server_reserved_order_ids", []))
        accounted = plan.get("server_accounted_fill_quantities", {})
        local = []
        pending_buys = {}
        for row in remaining_reservations:
            if row["instance_id"] != instance:
                continue
            unaccounted = max(0, row["filled"] - int(accounted.get(row["client_order_id"], 0)))
            units = unaccounted + (0 if row["client_order_id"] in server_reserved else row["remaining"])
            local.append({**row, "reserve_units": units})
            if row["side"] == "BUY":
                pending_buys[row["symbol"]] = pending_buys.get(row["symbol"], 0) + row["remaining"] + unaccounted
        own_cash_reserve = sum(r["reserve_cash"] * r["reserve_units"] / r["quantity"] for r in local if r["side"] == "BUY")
        owned_cash = max(0., float(plan["strategy_cash"]) - own_cash_reserve)
        sellable = {s: min(q, int(plan["strategy_positions"].get(s, 0))) - sum(r["reserve_units"] for r in local if r["side"] == "SELL" and r["symbol"] == s)
                    for s, q in plan["strategy_sellable"].items()}
        results = []
        for item in plan["orders"]:
            coid, symbol, side = item["client_order_id"], item["symbol"], item["side"]
            intent = agent.journal.intent(coid)
            matches = by_remark.get(coid, [])
            if matches:
                actual = submissions.get(coid, item)
                if len(matches) != 1 or not broker_matches(matches[0], item, actual["quantity"], account.account_id):
                    raise JournalConflict("broker order identity mismatch")
                if intent is None or intent["state"] in ("SUBMITTING", "UNKNOWN"):
                    agent.journal.mark(coid, "ACKED", str(matches[0].order_id), "verified broker identity")
                results.append({"client_order_id": coid, "status": "ALREADY_AT_BROKER"})
                continue
            if intent is not None:
                results.append({"client_order_id": coid, "status": "SKIPPED_" + intent["state"]})
                continue
            spec = plan["instrument_specs"][symbol]
            quantity, limit = int(item["quantity"]), float(item["limit_price"])
            if side == "BUY":
                lot = int(spec["buy_lot"])
                if lot <= 0:
                    raise ValueError("invalid buy lot")
                held = int(plan["strategy_positions"].get(symbol, 0))
                room = max(0, int(plan["frozen_target"].get(symbol, 0)) - held - pending_buys.get(symbol, 0))
                quantity = min(quantity, room) // lot * lot
                while quantity > 0 and order_cost(quantity, limit, spec) > min(physical_cash, owned_cash) + 1e-8:
                    quantity -= lot
                if quantity < int(spec.get("min_buy_quantity", lot)):
                    quantity = 0
            else:
                available = max(0, min(int(sellable.get(symbol, 0)), int(physical_sellable.get(symbol, 0))))
                quantity = min(quantity, available)
                lot = int(spec.get("sell_lot", 100))
                if lot <= 0:
                    raise ValueError("invalid sell lot")
                if quantity % lot and not (spec.get("odd_lot_sell_all") and quantity == available):
                    quantity = quantity // lot * lot
            if quantity <= 0:
                results.append({"client_order_id": coid, "status": "SKIPPED_NO_ROOM"})
                continue
            try:
                require_guard(agent, item, quantity, allowed_symbols=set(plan["allowed_symbols"]), spec=spec, policy=policy)
            except (ValueError, RuntimeError) as exc:
                results.append({"client_order_id": coid, "status": "GUARD_REJECTED", "detail": str(exc)})
                continue
            cost = order_cost(quantity, limit, spec) if side == "BUY" else 0.
            if dry_run:
                status = "DRY_RUN"
            else:
                record = dict(item, instance_id=instance, trade_date=trade_date, quantity=quantity, reserve_cash=cost)
                agent.journal.mark(coid, "SUBMITTING", None, None, submission=record)
                outcome = agent.gateway.submit_limit(symbol=symbol, side=side, quantity=quantity, limit_price=limit, remark=coid)
                state = {"SUBMITTED": "ACKED", "REJECTED": "REJECTED"}.get(outcome.status, "UNKNOWN")
                agent.journal.mark(coid, state, outcome.local_order_id if state == "ACKED" else None, outcome.detail)
                status = outcome.status
            # Unknown calls reserve capacity across both subsequent orders and restarts.
            if status != "REJECTED":
                if side == "BUY":
                    physical_cash -= cost
                    owned_cash -= cost
                    pending_buys[symbol] = pending_buys.get(symbol, 0) + quantity
                else:
                    physical_sellable[symbol] = physical_sellable.get(symbol, 0) - quantity
                    sellable[symbol] = sellable.get(symbol, 0) - quantity
            results.append({"client_order_id": coid, "status": status, "quantity": quantity})
        outputs.append({"instance_id": instance, "status": "DRY_RUN" if dry_run else "EXECUTED", "orders": results})
    agent.flush_events()
    return {"status": "DRY_RUN" if dry_run else "EXECUTED", "plans": outputs}
