"""Real-data system replay: server core + agent + simulated QMT vs the C3 research replay.

Run only inside a research directory on the qmt server (raw data never leaves it):

    PYTHONPATH=<release>/v2.3:<release>/v2.3/server python oms_system_replay.py --since 2024-10-01

Corporate actions are excluded on both sides (the simulator has no dividend or split
model); both sides see identical raw prices, so any difference is an execution-path bug.
Writes system_replay.json with per-cycle comparisons and the overall verdict.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path("/opt/qmt-server/private/research-runs/etf-final-20260928/reports")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2024-10-01")
    parser.add_argument("--capital", type=float, default=200000.)
    parser.add_argument("--output", default="system_replay.json")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT / "execution_experiment_20260927"))
    import backtest as source                                   # frozen research loader
    server_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(server_root / "tests" / "integration"))
    import test_oms_system_replay as harness                    # same harness as the CI test
    import policy_replay
    from app.oms.planner import Policy, session_schedule

    weights, daily, close, _, _ = source.load_inputs()
    cal_all = [d.strftime("%Y%m%d") for d in close.index]
    keep = []
    for d in weights.loc[args.since:].index:
        try:
            sched = session_schedule(cal_all, d.strftime("%Y%m%d"), 5)
        except ValueError:
            continue
        keep.append(d) if sched else None
    weights = weights.loc[keep]
    symbols = list(weights.columns)
    bars = {(d.strftime("%Y%m%d"), s): {"open": float(b["open"]), "high": float(b["high"]), "low": float(b["low"]),
                                        "close": float(b["close"]), "volume": float(b["volume"]),
                                        "suspendFlag": int(b.get("suspendFlag", 0) or 0)}
            for (d, s), b in daily.items() if d >= weights.index.min()}

    c3 = policy_replay.Policy("C3", lot="nearest", size_factor=1.001, buy_bps=Policy().buy_bps, touch=True,
                              sell_schedule=(50., 200.))
    _, hist, cycles, fills, events = policy_replay.run(weights, daily, close, [], c3, capital=args.capital,
                                                       slip_bps=5., participation=.01)

    with tempfile.TemporaryDirectory() as tmp:
        system = harness.System(Path(tmp), bars, symbols=symbols, capital=args.capital, transport="direct")
        signals = {d.strftime("%Y%m%d"): ({s: float(w) for s, w in weights.loc[d].items()},
                                          {s: float(close.loc[d, s]) for s in symbols}) for d in weights.index}
        days = [d for d in cal_all if d >= min(signals)]
        last_signal = max(signals)
        last_day = session_schedule(cal_all, last_signal, 3)[-1].trade_date
        for day in days:
            if day > last_day:
                break
            system.run_day(day, signals.get(day))
        sides = {23: "BUY", 24: "SELL"}
        system_fills = Counter((t["date"], t["stock_code"], sides[t["order_type"]], int(t["traded_volume"]),
                                round(float(t["traded_price"]), 3)) for t in system.trades)
        replay_fills = Counter((f.date.replace("-", ""), f.symbol, f.direction, int(f.quantity), round(float(f.price), 3))
                               for f in fills.itertuples())
        positions, cash = system.ledger()
        broker = {s: p["volume"] for s, p in system.exchange.positions().items() if p["volume"]}
        out = {
            "since": args.since, "signals": sorted(signals), "symbols": symbols, "capital": args.capital,
            "replay_fills": sum(replay_fills.values()), "system_fills": sum(system_fills.values()),
            "fills_identical": system_fills == replay_fills,
            "only_in_replay": sorted(map(list, (replay_fills - system_fills).elements()))[:20],
            "only_in_system": sorted(map(list, (system_fills - replay_fills).elements()))[:20],
            "ledger_equals_broker": positions == broker,
            "cash_system": cash, "cash_replay": float(hist.cash.iloc[-1]),
            "cash_abs_diff": abs(cash - float(hist.cash.iloc[-1])),
            "replay_unfilled_reasons": {f"{k[0]}:{k[1]}": int(v) for k, v in
                                        Counter(zip(events.side, events.reason)).items()},
        }
        out["verdict"] = "IDENTICAL" if (out["fills_identical"] and out["ledger_equals_broker"]
                                         and out["cash_abs_diff"] < 1e-6) else "DIFFERENT"
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in ("verdict", "replay_fills", "system_fills", "cash_abs_diff",
                                          "ledger_equals_broker")}))
    return 0 if out["verdict"] == "IDENTICAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
