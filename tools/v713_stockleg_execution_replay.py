#!/usr/bin/env python3
"""Remote-only V713 stock-leg execution replay (design item E3). Research only, never an order router.

Replays V713_STOCKLEG_V1 on the frozen monthly TOP50 targets and V713 states with daily raw
bars: closing-auction sells, next-day opening-auction buys, limit-up and limit-down locks,
suspensions, a daily-volume cap, QMT dividend and bonus-share records and the confirmed fees. Lot targets,
limits, sell and buy plans and the session schedule come from the production planner
(app.oms.planner), so the replay and the live executor compute the same orders.
Needs v2.3/server on PYTHONPATH. Inputs stay on qmt.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import importlib.util
import json
import math
from pathlib import Path

from app.oms.instruments import estimate_fees, floor_buy_quantity
from app.oms.planner import (get_instrument, lot_target, plan_buy_session, plan_sell_session,
                             policy_from_dict, session_schedule)
from app.oms.v713_stock_leg import expand_policy

AUX = "AUX_HYDRA"
TOLERANCE = 1e-9
ROOT = Path("/opt/qmt-server/private/research-runs")
SOURCE = ROOT / "v713-combination-validation-20261016"
PRIOR = ROOT / "v713-scale-mapping-20261019-01"


@dataclass(frozen=True)
class Bar:
    open: float
    close: float
    volume: float  # lots of 100 shares
    suspended: bool = False
    up_limit: float | None = None
    down_limit: float | None = None


@dataclass
class Market:
    calendar: list
    bars: dict     # (symbol, date) -> Bar at raw prices
    actions: dict  # (symbol, ex-date) -> (cash dividend per share, bonus shares per share)
    st: set        # (symbol, date) under ST price limits

    def mark(self, symbol: str, date: str) -> float | None:
        """Last traded raw close on or before ``date``."""
        if getattr(self, "_marks", None) is None:
            closes = defaultdict(list)
            for (code, day), bar in self.bars.items():
                if not bar.suspended and bar.volume > 0 and bar.close > 0:
                    closes[code].append((day, bar.close))
            self._marks = {code: ([d for d, _ in sorted(v)], [c for _, c in sorted(v)]) for code, v in closes.items()}
        dates, values = self._marks.get(symbol, ([], []))
        i = bisect_right(dates, date)
        return values[i - 1] if i else None


def limit_pct(symbol: str, date: str, st: bool) -> float:
    if st:
        return .05
    if symbol.startswith("688") or (symbol.startswith("30") and symbol.endswith(".SZ") and date >= "20200824"):
        return .20
    return .10


def price_limits(reference: float | None, pct: float) -> tuple[float | None, float | None]:
    """Up and down limit prices, rounded half up to the cent as the exchanges publish them."""
    if reference is None or reference <= 0:
        return None, None
    base, step = Decimal(str(reference)), Decimal("0.01")
    up = (base * (1 + Decimal(str(pct)))).quantize(step, rounding=ROUND_HALF_UP)
    down = (base * (1 - Decimal(str(pct)))).quantize(step, rounding=ROUND_HALF_UP)
    return float(up), float(down)


def auction_fill(bar: Bar | None, side: str, limit: float) -> tuple[float | None, str | None]:
    """Buys fill at the opening auction, sells at the closing auction, never at a locked limit."""
    if bar is None or bar.suspended or bar.volume <= 0:
        return None, "SUSPENDED"
    if side == "BUY":
        price = bar.open
        if bar.up_limit is not None and price >= bar.up_limit - TOLERANCE:
            return None, "LIMIT_UP"
        return (price, None) if price <= limit + TOLERANCE else (None, "PRICE")
    price = bar.close
    if bar.down_limit is not None and price <= bar.down_limit + TOLERANCE:
        return None, "LIMIT_DOWN"
    return (price, None) if price >= limit - TOLERANCE else (None, "PRICE")


def apply_corporate_action(quantity: int, interest: float, bonus: float) -> tuple[int, float]:
    """Shares and cash after an ex-date: pre-tax cash dividend, floored bonus and capitalisation shares."""
    return quantity + int(math.floor(quantity * bonus + 1e-9)), quantity * interest


def ex_reference(prev_close: float, interest: float, bonus: float, allot_num: float, allot_price: float) -> float:
    """Exchange ex-rights reference price used for the ex-date price limits."""
    return (prev_close - interest + allot_price * allot_num) / (1 + bonus + allot_num)


def run_window(periods, *, end, capital, template, market, participation=.01, rolling_anchor=False) -> dict:
    """One continuous stock-leg ledger from cash over consecutive monthly V713 targets.

    A policy with ``anchor = previous_close`` (V713_STOCKLEG_V2) prices every session off the
    previous close, as the live executor does; ``rolling_anchor`` forces that for any policy.
    """
    calendar = [d for d in market.calendar if periods[0]["signal"] <= d <= end]
    signals = {p["signal"]: p for p in periods}
    positions = {}
    state = {"cash": float(capital), "fees": 0.0}
    cycles, events, nav_rows = [], [], []
    pending, previous_sleeve, today_bought = None, None, defaultdict(int)

    def stock_value(date):
        return sum(q * market.mark(s, date) for s, q in positions.items() if q)

    def note(reasons, date, symbol, side, reason, quantity):
        reasons[symbol, side][reason] += quantity
        events.append(dict(date=date, symbol=symbol, side=side, reason=reason, quantity=quantity))
        if reason == "SELL_LOT" and symbol.startswith("688"):
            pending["star_sell_lot"] += 1

    def book(date, symbol, side, quantity, price, spec):
        gross = quantity * price
        fee = estimate_fees(spec, gross, side)
        sign = 1 if side == "BUY" else -1
        positions[symbol] = positions.get(symbol, 0) + sign * quantity
        state["cash"] -= sign * gross + fee
        state["fees"] += fee
        pending["fees"] += fee
        pending["orders"] += 1
        pending["bought" if side == "BUY" else "sold"][symbol] += quantity
        if side == "BUY":
            today_bought[symbol] += quantity
        if not positions[symbol]:
            del positions[symbol]
        assert state["cash"] >= -1e-6 and positions.get(symbol, 0) >= 0

    def buy_phase(date, previous):
        reasons = defaultdict(lambda: defaultdict(float))
        if (rolling_anchor or pending["policy"].anchor == "previous_close") and pending["buy_anchor"] is not None:
            pending["buy_anchor"].update(closing_anchors(previous, [s for s, q in pending["frozen"].items() if q > 0]))
        orders, deferrals = plan_buy_session(target=pending["frozen"], positions=positions, cash=state["cash"],
                                             buy_anchor=pending["buy_anchor"] or {}, policy=pending["policy"])
        for d in deferrals:
            note(reasons, date, d.symbol, "BUY", d.reason, d.quantity)
        for order in orders:
            symbol, bar = order.symbol, market.bars.get((order.symbol, date))
            price, why = auction_fill(bar, "BUY", order.limit_price)
            if price is None:
                note(reasons, date, symbol, "BUY", why, order.quantity)
                continue
            spec = get_instrument(symbol, pending["policy"])
            quantity = floor_buy_quantity(min(order.quantity, int(bar.volume * 100 * participation)), spec)
            why = "CAPACITY" if quantity < order.quantity else None
            while quantity > 0 and quantity * price + estimate_fees(spec, quantity * price, "BUY") > state["cash"] + 1e-6:
                quantity = floor_buy_quantity(quantity - spec.buy_lot, spec)
                why = why or "CASH"
            if quantity:
                book(date, symbol, "BUY", quantity, price, spec)
            if quantity < order.quantity:
                note(reasons, date, symbol, "BUY", why, order.quantity - quantity)
        for symbol, target in pending["frozen"].items():
            if target > positions.get(symbol, 0) or (symbol, "BUY") in reasons:
                pending["reasons"][symbol, "BUY"] = reasons[symbol, "BUY"]
        if pending["first_buy_done"] is None:
            pending["first_buy_done"] = dict(pending["bought"])

    def sell_phase(date, attempt, previous):
        reasons = defaultdict(lambda: defaultdict(float))
        sellable = {s: q - today_bought.get(s, 0) for s, q in positions.items()}
        anchors = pending["anchor"]
        if rolling_anchor or pending["policy"].anchor == "previous_close":
            anchors = {s: market.mark(s, previous) or a for s, a in anchors.items()}
        orders, deferrals = plan_sell_session(target=pending["frozen"], positions=positions, sellable=sellable,
                                              sell_anchor=anchors, attempt=attempt, policy=pending["policy"])
        for d in deferrals:
            note(reasons, date, d.symbol, "SELL", d.reason, d.quantity)
        for order in orders:
            symbol, bar = order.symbol, market.bars.get((order.symbol, date))
            price, why = auction_fill(bar, "SELL", order.limit_price)
            if price is None:
                note(reasons, date, symbol, "SELL", why, order.quantity)
                continue
            spec = get_instrument(symbol, pending["policy"])
            held = positions[symbol]
            quantity = min(order.quantity, int(bar.volume * 100 * participation))
            if quantity < held:
                quantity = quantity // spec.sell_lot * spec.sell_lot
                if quantity < spec.min_sell_quantity:
                    quantity = 0
            if quantity:
                book(date, symbol, "SELL", quantity, price, spec)
            if quantity < order.quantity:
                note(reasons, date, symbol, "SELL", "CAPACITY", order.quantity - quantity)
        for symbol, target in pending["frozen"].items():
            if positions.get(symbol, 0) > target or (symbol, "SELL") in reasons:
                pending["reasons"][symbol, "SELL"] = reasons[symbol, "SELL"]
        if pending["first_sell_done"] is None:
            pending["first_sell_done"] = dict(pending["sold"])

    def closing_anchors(date, symbols):
        anchors = {}
        for symbol in symbols:
            bar = market.bars.get((symbol, date))
            if bar is not None and not bar.suspended and bar.volume > 0 and bar.close > 0:
                anchors[symbol] = bar.close
        return anchors

    def finish(date):
        nav = state["cash"] + stock_value(date)
        frozen, anchor, start = pending["frozen"], pending["anchor"], pending["start_positions"]
        need_buy = {s: max(0, q - start.get(s, 0)) for s, q in frozen.items()}
        need_sell = {s: max(0, start.get(s, 0) - q) for s, q in frozen.items()}

        def completion(done, need):
            intended = sum(n * anchor[s] for s, n in need.items())
            return (sum(min((done or {}).get(s, 0), n) * anchor[s] for s, n in need.items()) / intended
                    if intended else None)

        causes = defaultdict(float)
        for symbol, target in frozen.items():
            remaining = target - positions.get(symbol, 0)
            if not remaining:
                continue
            side = "BUY" if remaining > 0 else "SELL"
            share = abs(remaining) * market.mark(symbol, date) / nav
            reasons = pending["reasons"].get((symbol, side)) or {}
            total = sum(reasons.values())
            if not total:
                causes[side + "_NO_SESSION"] += share
            for reason, quantity in reasons.items():
                causes[side + "_" + reason] += share * quantity / total
        cycles.append(dict(
            signal=pending["signal"], end=date, kind=pending["kind"], sleeve=pending["sleeve"],
            nav_signal=pending["nav_signal"], nav_end=nav, lot_gap=pending["lot_gap"],
            exec_underweight=sum(max(0, q - positions.get(s, 0)) * market.mark(s, date) for s, q in frozen.items()) / nav,
            exec_overweight=sum(max(0, positions.get(s, 0) - q) * market.mark(s, date) for s, q in frozen.items()) / nav,
            first_buy_completion=completion(pending["first_buy_done"], need_buy),
            first_sell_completion=completion(pending["first_sell_done"], need_sell),
            buy_completion=completion(pending["bought"], need_buy),
            sell_completion=completion(pending["sold"], need_sell),
            fees=pending["fees"], orders=pending["orders"], residual_stock_weight=stock_value(date) / nav,
            star_sell_lot_deferrals=pending["star_sell_lot"], causes=dict(causes)))

    def publish(date, period):
        nonlocal pending, previous_sleeve
        held = sorted(s for s, q in positions.items() if q)
        sleeve = period["sleeve"]
        kind = ("AUX_LIQUIDATE" if sleeve == AUX else "AUX_EXIT" if previous_sleeve == AUX
                else "BUILD" if not held else "REBALANCE")
        previous_sleeve = sleeve
        if sleeve == AUX and not held:
            cycles.append(dict(signal=date, end=date, kind="AUX_CASH", sleeve=sleeve,
                               nav_signal=state["cash"], nav_end=state["cash"], residual_stock_weight=0.0))
            return
        codes = [] if sleeve == AUX else list(period["codes"])
        policy = policy_from_dict(expand_policy(template, set(codes) | set(held)))
        marks = {s: market.mark(s, date) for s in set(codes) | set(held)}
        missing = sorted(s for s, p in marks.items() if p is None)
        if missing:
            raise ValueError(f"no close on or before {date} for {missing}")
        nav = state["cash"] + stock_value(date)
        weights = {s: 1 / len(codes) for s in codes}
        frozen = lot_target(nav, weights, marks, policy) if weights else {}
        for symbol in held:
            frozen.setdefault(symbol, 0)
        later = [d for d in market.calendar if d > date]
        pending = dict(signal=date, sleeve=sleeve, kind=kind, policy=policy, frozen=frozen,
                       anchor={s: marks[s] for s in frozen}, buy_anchor=None,
                       schedule=session_schedule(market.calendar, date, policy.window, earliest_sell=later[0]),
                       start_positions=dict(positions), bought=defaultdict(int), sold=defaultdict(int),
                       first_buy_done=None, first_sell_done=None, reasons={}, fees=0.0, orders=0,
                       star_sell_lot=0, nav_signal=nav,
                       lot_gap=sum(max(0., (1 - policy.reserve) * w - frozen[s] * marks[s] / nav)
                                   for s, w in weights.items()))

    previous = None
    for date in calendar:
        today_bought = defaultdict(int)
        # Holders at the previous close receive the ex-date entitlement before the day trades.
        for symbol in sorted(positions):
            action = market.actions.get((symbol, date))
            if action is None:
                continue
            quantity, cash = apply_corporate_action(positions[symbol], *action)
            events.append(dict(date=date, symbol=symbol, side="ACTION", reason="CORPORATE_ACTION",
                               quantity=quantity - positions[symbol], cash=cash))
            positions[symbol] = quantity
            state["cash"] += cash
        if pending is not None:
            sold_today = False
            for session in sorted((x for x in pending["schedule"] if x.trade_date == date),
                                  key=lambda x: x.phase != "BUY"):
                if session.phase == "BUY":
                    buy_phase(date, previous)
                else:
                    sell_phase(date, session.sell_attempt, previous)
                    sold_today = True
            wanted = [s for s, q in pending["frozen"].items() if q > 0]
            if pending["buy_anchor"] is None and sold_today:
                pending["buy_anchor"] = closing_anchors(date, wanted)
            elif pending["buy_anchor"] is not None:
                pending["buy_anchor"].update(closing_anchors(date, [s for s in wanted if s not in pending["buy_anchor"]]))
            if date == pending["schedule"][-1].trade_date:
                finish(date)
                pending = None
        if date in signals and date != end:
            if pending is not None:
                finish(date)
                pending = None
            publish(date, signals[date])
        nav = state["cash"] + stock_value(date)
        assert math.isfinite(nav) and nav > 0 and state["cash"] >= -1e-6
        nav_rows.append(dict(date=date, nav=nav, cash=state["cash"], stock_weight=stock_value(date) / nav))
        previous = date
    if pending is not None:
        finish(calendar[-1])
    return {"cycles": cycles, "events": events, "nav": nav_rows, "fees": state["fees"]}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_market(g, names, data_root: Path, start: str, corporate_actions: bool) -> tuple[Market, dict]:
    """Daily bars of the target names with limit prices, ST flags and, optionally, QMT dividend records.

    The market/daily/stocks files are forward-adjusted as of their download (closes before
    a later ex-date carry more than two decimals), so their prices already contain every
    dividend and split. Applying QMT dividend records on top would count them twice;
    ``corporate_actions`` is for genuinely unadjusted bars only.
    """
    import pandas as pd

    dates = [str(d.date()).replace("-", "") for d in g["all_dates"]]
    calendar = [d for d in dates if d >= start]
    index = {code: i for i, code in enumerate(g["codes"])}
    first = dates.index(calendar[0])
    bars, actions, st = {}, {}, set()
    counts = Counter()
    for symbol in sorted(names):
        for day, flag in zip(calendar, g["st_mask"][index[symbol], first:]):
            if bool(flag):
                st.add((symbol, day))
        rights = {}
        path = data_root / "fundamentals" / "dividends" / f"{symbol}.parquet"
        if corporate_actions and path.exists():
            for row in pd.read_parquet(path).itertuples(index=False):
                day = str(int(row.trade_date))
                if day < calendar[0]:
                    continue
                interest, bonus = float(row.interest or 0), float(row.stockBonus or 0) + float(row.stockGift or 0)
                allot_num, allot_price = float(row.allotNum or 0), float(row.allotPrice or 0)
                actions[symbol, day] = (interest, bonus)
                rights[day] = (interest, bonus, allot_num, allot_price)
                counts["dividend_records"] += 1
                counts["rights_issues_not_subscribed"] += allot_num > 0
        frame = pd.read_parquet(data_root / "market" / "daily" / "stocks" / f"{symbol}.parquet",
                                columns=["trade_date", "open", "close", "volume", "suspendFlag"])
        frame = frame.assign(trade_date=frame.trade_date.astype(str)).drop_duplicates("trade_date", keep="last")
        frame = frame[frame.trade_date >= calendar[0]].sort_values("trade_date")
        previous_close = None
        for row in frame.itertuples(index=False):
            day = row.trade_date
            reference = previous_close
            if previous_close is not None and day in rights:
                reference = ex_reference(previous_close, *rights[day])
            up, down = price_limits(reference, limit_pct(symbol, day, (symbol, day) in st))
            suspended = bool(row.suspendFlag) or not (row.volume > 0)
            bars[symbol, day] = Bar(float(row.open), float(row.close), float(row.volume), suspended, up, down)
            if not suspended and row.close > 0:
                previous_close = float(row.close)
    return Market(calendar, bars, actions, st), {k: int(v) for k, v in counts.items()}


def startup_check(out: Path) -> dict:
    if not str(out).startswith(str(ROOT) + "/") or out == ROOT:
        raise SystemExit("Run only from a new directory under " + str(ROOT))
    if (out / "execution_replay_summary.json").exists():
        raise SystemExit("Outputs already exist; use a new run directory")
    archived = json.loads((SOURCE / "stock" / "stock_summary.json").read_text())
    prior = json.loads((PRIOR / "scale_summary.json").read_text())
    checks = {
        "sibling stock model is the archived script": sha(out / "v713_combination_validation_stock.py") == archived["script_sha256"],
        "period inputs match the Oct-19 manifest": sha(SOURCE / "stock" / "period_inputs.json") == prior["source_manifest"][str(SOURCE / "stock" / "period_inputs.json")],
        "monthly panel matches the archived artifact": any(Path(a["path"]).name == "monthly_panel.parquet" and sha(Path(a["path"])) == a["sha256"] for a in archived["remote_artifacts"]),
    }
    if not all(checks.values()):
        raise SystemExit("Startup check failed: " + json.dumps(checks))
    return checks


def summarize(cycles, windows, recent_from) -> dict:
    import numpy as np
    import pandas as pd

    def stats(series):
        a = pd.Series(series, dtype=float).dropna()
        if a.empty:
            return None
        return {"n": int(len(a)), "min": float(a.min()), "p05": float(a.quantile(.05)), "median": float(a.median()),
                "mean": float(a.mean()), "p95": float(a.quantile(.95)), "max": float(a.max())}

    frame = pd.DataFrame(cycles)
    out = {"cycles": [], "recent_cycles": [], "windows": []}
    for key, group in (("cycles", frame), ("recent_cycles", frame[frame.signal >= recent_from])):
        out[key] = cycle_rows(group, stats)
    w = pd.DataFrame(windows)
    for capital, c in w.groupby("capital"):
        out["windows"].append({"capital": int(capital), "windows": int(len(c)),
                               "relative_nav_vs_theory": stats(c.relative_nav_vs_theory),
                               "fees": stats(c.fees), "net_return": stats(c.net_return)})
    return out


def cycle_rows(frame, stats) -> list:
    import pandas as pd

    rows = []
    for (capital, kind), c in frame.groupby(["capital", "kind"]):
        traded = c[c.kind != "AUX_CASH"]
        causes = pd.DataFrame(list(traded.causes)).fillna(0.) if len(traded) else pd.DataFrame()
        rows.append({
            "capital": int(capital), "kind": kind, "cycles": int(len(c)),
            "distinct_signal_months": int(c.signal.nunique()),
            "exec_underweight": stats(traded.get("exec_underweight", [])),
            "cycles_exec_underweight_above_2pct": int((traded.get("exec_underweight", pd.Series(dtype=float)) > .02).sum()),
            "exec_overweight": stats(traded.get("exec_overweight", [])),
            "lot_gap": stats(traded.get("lot_gap", [])),
            "first_buy_completion": stats(traded.get("first_buy_completion", [])),
            "buy_completion": stats(traded.get("buy_completion", [])),
            "first_sell_completion": stats(traded.get("first_sell_completion", [])),
            "sell_completion": stats(traded.get("sell_completion", [])),
            "fees": stats(traded.get("fees", [])),
            "fees_bps_of_nav": stats((traded.fees / traded.nav_signal * 1e4) if len(traded) else []),
            "orders": stats(traded.get("orders", [])),
            "residual_stock_weight": stats(c.residual_stock_weight),
            "cycles_residual_above_1pct": int((c.residual_stock_weight > .01).sum()) if kind == "AUX_LIQUIDATE" else None,
            "star_sell_lot_deferrals": int(traded.get("star_sell_lot_deferrals", pd.Series(dtype=float)).fillna(0).sum()),
            "unfinished_share_of_nav_mean_by_cause": {k: float(v) for k, v in causes.mean().items()} if len(causes) else {}})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--source", required=True)
    args = ap.parse_args()
    out = Path(__file__).resolve().parent
    checks = startup_check(out)
    protocol = json.loads((out / "protocol.json").read_text(encoding="utf-8"))
    template = json.loads(Path(protocol["policy_template"]).read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("archived_stock_model", out / "v713_combination_validation_stock.py")
    stock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stock)
    import pandas as pd

    _, g, _, _ = stock.prepare(argparse.Namespace(source=args.source, data=args.data, end="2026-06-30"), out)
    inputs = json.loads((SOURCE / "stock" / "period_inputs.json").read_text())
    panel = pd.read_parquet(SOURCE / "stock" / "monthly_panel.parquet").set_index("start")
    periods = [{"signal": p["start"].replace("-", ""), "end": p["date"].replace("-", ""), "codes": p["codes"],
                "sleeve": str(panel.loc[p["start"], "sleeve"]), "returns": p["returns"]} for p in inputs]
    names = sorted({c for p in periods for c in p["codes"]})
    market, action_counts = load_market(g, names, Path(args.data), periods[0]["signal"],
                                        protocol["corporate_actions"] == "qmt_dividend_records")
    qa = [{"name": k, "passed": v} for k, v in checks.items()]
    qa.append({"name": "every target name has daily bars", "passed": all(any((s, d) in market.bars for d in market.calendar[:5000]) for s in names)})
    # A post-hoc sensitivity grid names its variants in the protocol; the frozen policy is the default.
    variants = protocol.get("variants") or [{"name": template["policy_id"], "overrides": {}}]
    cycles, windows, events = [], [], Counter()
    for variant in variants:
        variant_template = {**template, **variant["overrides"]}
        for capital in protocol["capitals"]:
            for k in range(len(periods) - 11):
                window = periods[k:k + 12]
                result = run_window(window, end=window[-1]["end"], capital=capital, template=variant_template,
                                    market=market, participation=protocol["participation"],
                                    rolling_anchor=variant.get("anchor") == "rolling")
                theory = 1.0
                for p in window:
                    theory *= 1 + (0.0 if p["sleeve"] == AUX else sum(p["returns"]) / len(p["returns"]))
                factor = result["nav"][-1]["nav"] / capital
                windows.append({"variant": variant["name"], "capital": capital, "start": window[0]["signal"],
                                "end": window[-1]["end"], "net_return": factor - 1,
                                "relative_nav_vs_theory": factor / theory - 1, "fees": result["fees"]})
                for i, c in enumerate(result["cycles"]):
                    cycles.append({**c, "variant": variant["name"], "capital": capital,
                                   "window_start": window[0]["signal"], "cycle_index": i})
                if variant is variants[0]:
                    events.update((e["side"], e["reason"]) for e in result["events"])
    first_variant = {**template, **variants[0]["overrides"]}
    again = run_window(periods[:12], end=periods[11]["end"], capital=protocol["capitals"][0], template=first_variant,
                       market=market, participation=protocol["participation"])
    first = [c for c in cycles if c["variant"] == variants[0]["name"] and c["capital"] == protocol["capitals"][0]
             and c["window_start"] == periods[0]["signal"]]
    qa.append({"name": "replay is deterministic", "passed": [c["nav_end"] for c in again["cycles"]] == [c["nav_end"] for c in first]})
    by_variant = {v["name"]: summarize([c for c in cycles if c["variant"] == v["name"]],
                                       [w for w in windows if w["variant"] == v["name"]], protocol["recent_from"])
                  for v in variants}
    summary = {"experiment_id": protocol["experiment_id"],
               "status": "COMPLETED_DAILY_AUCTION_REPLAY_NOT_BROKER_FILLS", **by_variant[variants[0]["name"]],
               "variants": {v["name"]: {"overrides": v["overrides"], **by_variant[v["name"]]} for v in variants}
               if len(variants) > 1 else {},
               "events_by_side_reason": {f"{side}:{reason}": n for (side, reason), n in sorted(events.items())},
               "corporate_action_records": action_counts,
               "qa": qa, "qa_all_passed": all(q["passed"] for q in qa), "limits": protocol["limits"],
               "production_modified": False, "orders_submitted": 0}
    pd.DataFrame(cycles).assign(causes=lambda f: f.causes.map(json.dumps)).to_parquet(out / "cycles_private.parquet", index=False)
    pd.DataFrame(windows).to_parquet(out / "windows_private.parquet", index=False)
    (out / "execution_replay_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    assert summary["qa_all_passed"], [q for q in qa if not q["passed"]]
    print(json.dumps({"qa": len(qa), "cycles": len(cycles), "windows": len(windows)}))


if __name__ == "__main__":
    main()
